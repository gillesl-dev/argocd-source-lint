from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from argocd_source_lint.fsutil import discover_documents, is_within_budget, walk_tree
from argocd_source_lint.git_context import (
    is_local_repo_url,
    materialize_revision,
    revision_matches_checkout,
)
from argocd_source_lint.globs import match_doublestar, match_git_pathspec, match_path
from argocd_source_lint.loader import (
    INVALID_MANIFEST_ERRORS,
    build_application,
    invalid_reason,
)
from argocd_source_lint.models import Application, Finding, Severity

RULE_ID = "unresolvable-generator"


@dataclass(frozen=True, slots=True)
class GeneratorContext:
    """Per-ApplicationSet state passed unchanged through generator recursion."""

    repo_root: Path
    local_origin: str | None
    appset_name: str
    source_file: Path
    severity: Severity
    new_git_file_globbing: bool


# Classic ApplicationSet templating (`{{key}}`, valyala/fasttemplate).
# `spec.goTemplate: true` uses a different engine and is not supported.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.\-\[\]]+)\s*\}\}")

_yaml_safe = YAML(typ="safe")


def discover(
    repo_root: Path,
    local_origin: str | None,
    severity: Severity,
    *,
    new_git_file_globbing: bool = False,
) -> tuple[list[Application], list[Finding]]:
    """Expand every ApplicationSet into the Applications its generators produce.

    A generator that cannot be resolved from the local checkout yields one
    finding instead of a guess, like an external Application source.
    """
    applications: list[Application] = []
    findings: list[Finding] = []

    for manifest_path, doc in discover_documents(repo_root):
        if not _is_application_set(doc):
            continue
        expanded_apps, doc_findings = _expand(
            doc, manifest_path, repo_root, local_origin, severity, new_git_file_globbing
        )
        applications.extend(expanded_apps)
        findings.extend(doc_findings)

    return applications, findings


def _is_application_set(doc: dict[str, Any]) -> bool:
    return doc.get("kind") == "ApplicationSet" and str(doc.get("apiVersion", "")).startswith(
        "argoproj.io/"
    )


def _expand(
    doc: dict[str, Any],
    manifest_path: Path,
    repo_root: Path,
    local_origin: str | None,
    severity: Severity,
    new_git_file_globbing: bool,
) -> tuple[list[Application], list[Finding]]:
    applications, findings = _expand_spec(
        doc, manifest_path, repo_root, local_origin, severity, new_git_file_globbing
    )
    if findings:
        # Some Applications may be missing: orphan-source must not report the
        # files they would deploy as errors.
        paths = _possible_source_paths(doc.get("spec") or {}, local_origin)
        findings = [f.model_copy(update={"unexpanded_paths": paths}) for f in findings]
    return applications, findings


def _expand_spec(
    doc: dict[str, Any],
    manifest_path: Path,
    repo_root: Path,
    local_origin: str | None,
    severity: Severity,
    new_git_file_globbing: bool,
) -> tuple[list[Application], list[Finding]]:
    metadata = doc.get("metadata", {}) or {}
    spec = doc.get("spec", {}) or {}
    try:
        source_file = manifest_path.relative_to(repo_root)
    except ValueError:
        source_file = manifest_path
    ctx = GeneratorContext(
        repo_root=repo_root,
        local_origin=local_origin,
        appset_name=metadata.get("name", ""),
        source_file=source_file,
        severity=severity,
        new_git_file_globbing=new_git_file_globbing,
    )

    if spec.get("goTemplate"):
        return [], [
            _finding(
                ctx,
                "`goTemplate: true` (Go template rendering) is out of scope v1 — "
                "only the classic `{{key}}` substitution is supported.",
            )
        ]

    if spec.get("templatePatch"):
        return [], [
            _finding(
                ctx,
                "`templatePatch` is not supported, so the Applications this ApplicationSet "
                "generates are not checked.",
            )
        ]

    template = spec.get("template") or {}
    appset_namespace = metadata.get("namespace") or "argocd"
    applications: list[Application] = []
    findings: list[Finding] = []

    for generator in spec.get("generators") or []:
        if not isinstance(generator, dict):
            continue
        param_sets, generator_findings = _resolve_generator(generator, ctx)
        findings.extend(generator_findings)
        # A generator can carry its own `template`, merged over the spec's.
        effective_template = _merged(template, _generator_template(generator))
        for params in param_sets:
            unresolved = _unresolved_placeholders(
                _checked_fields(_substitute(effective_template, params))
            )
            if unresolved:
                findings.append(
                    _finding(
                        ctx,
                        f"template placeholder(s) {', '.join(sorted(unresolved))} get no value "
                        "from the generator, so the Applications it generates are not checked.",
                    )
                )
                break
            try:
                applications.append(
                    _build_generated_application(
                        effective_template, params, manifest_path, repo_root, appset_namespace
                    )
                )
            except INVALID_MANIFEST_ERRORS as exc:
                findings.append(
                    _finding(
                        ctx,
                        f"template renders an invalid Application ({invalid_reason(exc)}), "
                        "so the Applications it generates are not checked.",
                    )
                )
                break

    return applications, findings


def _build_generated_application(
    template: dict[str, Any],
    params: dict[str, str],
    manifest_path: Path,
    repo_root: Path,
    appset_namespace: str,
) -> Application:
    rendered = _substitute(template, params)
    # ArgoCD always overwrites a generated Application's namespace with the
    # ApplicationSet's own, ignoring the template's (GenerateApplications in
    # applicationset/controllers/template/template.go).
    metadata = {**(rendered.get("metadata") or {}), "namespace": appset_namespace}
    doc = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": metadata,
        "spec": rendered.get("spec") or {},
    }
    return build_application(doc, manifest_path, repo_root)


def _resolve_generator(
    generator: dict[str, Any], ctx: GeneratorContext
) -> tuple[list[dict[str, str]], list[Finding]]:
    if generator.get("selector"):
        return [], [
            _finding(
                ctx,
                "generator has a `selector` (label filter on the generated params) — "
                "this tool doesn't evaluate label selectors, out of scope v1; every "
                "combination is left unexpanded rather than guessed at.",
            )
        ]

    if "list" in generator:
        return _resolve_list(generator.get("list") or {}, ctx)
    if "git" in generator:
        return _resolve_git(generator.get("git") or {}, ctx)
    if "matrix" in generator:
        return _resolve_matrix(generator.get("matrix") or {}, ctx)
    if "merge" in generator:
        return _resolve_merge(generator.get("merge") or {}, ctx)

    kind = next(iter(generator), "unknown")
    return [], [
        _finding(
            ctx,
            f"`{kind}` generator requires live cluster/API access — out of scope v1, "
            "this tool only reads the local Git checkout.",
        )
    ]


def _resolve_list(
    list_generator: dict[str, Any], ctx: GeneratorContext
) -> tuple[list[dict[str, str]], list[Finding]]:
    elements = list(list_generator.get("elements") or [])
    elements_yaml = list_generator.get("elementsYaml")
    if elements_yaml:
        try:
            parsed = _yaml_safe.load(str(elements_yaml))
        except YAMLError:
            parsed = None
        if not isinstance(parsed, list) or not is_within_budget(parsed):
            return [], [_finding(ctx, "list generator's `elementsYaml` is not a YAML list.")]
        elements.extend(parsed)
    return [_flatten_params(element) for element in elements if isinstance(element, dict)], []


def _resolve_git(
    git_generator: dict[str, Any], ctx: GeneratorContext
) -> tuple[list[dict[str, str]], list[Finding]]:
    repo_url = git_generator.get("repoURL", "")
    if not is_local_repo_url(repo_url, ctx.local_origin):
        return [], [
            _finding(
                ctx,
                "git generator targets a different repo — out of scope v1, this tool "
                "only verifies sources in the repo it runs in, see DESIGN.md.",
            )
        ]

    # The generator's `revision` is independent of the generated Applications'
    # `targetRevision` (upstream Git generator docs), so a revision pinned away
    # from HEAD must enumerate that snapshot, not the working tree. Same
    # mechanism as `coverage._resolved_source_root` (DESIGN.md).
    revision = git_generator.get("revision") or "HEAD"
    matches_checkout = revision_matches_checkout(ctx.repo_root, revision)
    if matches_checkout is None:
        # An unresolvable revision must never be treated as matching HEAD.
        return [], [
            _finding(
                ctx,
                f"git generator's revision `{revision}` missing from the local "
                "checkout — unable to tell which directories/files it would "
                "discover. Add `fetch-depth: 0` or fetch the branch in question "
                "in CI.",
                severity=Severity.UNVERIFIABLE,
            )
        ]

    base_root = ctx.repo_root
    if matches_checkout is False:
        snapshot_root = materialize_revision(ctx.repo_root, revision)
        if snapshot_root is None:
            return [], [
                _finding(
                    ctx,
                    f"git generator's revision `{revision}` could not be extracted "
                    "from the local checkout.",
                    severity=Severity.UNVERIFIABLE,
                )
            ]
        base_root = snapshot_root

    # With both set, Argo CD uses `directories` and ignores `files`.
    directories = git_generator.get("directories") or []
    if directories:
        return _resolve_git_directories(base_root, directories), []
    files = git_generator.get("files") or []
    return _resolve_git_files(base_root, files, ctx.new_git_file_globbing), []


def _resolve_git_directories(repo_root: Path, entries: list[Any]) -> list[dict[str, str]]:
    all_dirs = _list_local_directories(repo_root)
    included: set[str] = set()
    excluded: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pattern = str(entry.get("path", ""))
        matches = {d for d in all_dirs if match_path(pattern, d)}
        if entry.get("exclude"):
            excluded |= matches
        else:
            included |= matches

    return [_directory_params(d) for d in sorted(included - excluded)]


def _resolve_git_files(
    repo_root: Path, entries: list[Any], new_git_file_globbing: bool
) -> list[dict[str, str]]:
    # Argo CD's `LsFiles` in util/git/client.go: `git ls-files` by default,
    # doublestar with the ApplicationSet controller's new globbing flag.
    match = match_doublestar if new_git_file_globbing else match_git_pathspec
    all_files = _list_local_files(repo_root)
    matched: set[str] = set()
    excluded: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pattern = str(entry.get("path", ""))
        matches = {f for f in all_files if match(pattern, f)}
        if entry.get("exclude"):
            excluded |= matches
        else:
            matched |= matches
    matched -= excluded

    param_sets: list[dict[str, str]] = []
    for rel_path in sorted(matched):
        content = _load_params_file(repo_root / rel_path)
        if content is None:
            continue
        params = _flatten_params(content)
        # As in Argo CD, `path` is the directory holding the file, not the file.
        params.update(_directory_params(posixpath.dirname(rel_path)))
        filename = posixpath.basename(rel_path)
        params["path.filename"] = filename
        params["path.filenameNormalized"] = _normalized(filename)
        param_sets.append(params)
    return param_sets


# Real matrices (environments x regions, clusters x apps) rarely reach the
# low hundreds. The YAML node budget does not bound this: two flat lists of
# 5,000 elements each pass it but combine into 25 million entries.
_MAX_MATRIX_COMBINATIONS = 10_000


def _resolve_matrix(
    matrix_generator: dict[str, Any], ctx: GeneratorContext
) -> tuple[list[dict[str, str]], list[Finding]]:
    children = matrix_generator.get("generators") or []

    if len(children) > 2:
        # The Argo CD controller errors on a matrix with more than two child
        # generators (see DESIGN.md), so it would generate no Application.
        return [], [
            _finding(
                ctx,
                "matrix generator has more than 2 child generators — ArgoCD only "
                "supports combining exactly two and errors out on more, so this tool "
                "doesn't guess at what it would generate either.",
            )
        ]

    findings: list[Finding] = []
    param_lists: list[list[dict[str, str]]] = []
    children = [child for child in children if isinstance(child, dict)]
    if len(children) < 2:
        return [], [
            _finding(
                ctx, "matrix generator has fewer than 2 child generators, so it is not expanded."
            )
        ]

    first, second = children
    first_params, first_findings = _resolve_generator(first, ctx)
    findings.extend(first_findings)
    if _unresolved_placeholders(second):
        # The second generator uses the first one's parameters (`{{path}}`), so it
        # is resolved once per entry of the first, as Argo CD does.
        combined_chained: list[dict[str, str]] = []
        for entry in first_params:
            params, child_findings = _resolve_generator(_substitute(second, entry), ctx)
            findings.extend(child_findings)
            combined_chained.extend({**entry, **other} for other in params)
            if len(combined_chained) > _MAX_MATRIX_COMBINATIONS:
                break
        else:
            return combined_chained, findings
        param_lists = [first_params, combined_chained]
    else:
        second_params, second_findings = _resolve_generator(second, ctx)
        findings.extend(second_findings)
        param_lists = [first_params, second_params]

    if any(not params for params in param_lists):
        return [], findings

    total_combinations = 1
    for params in param_lists:
        total_combinations *= len(params)
    if total_combinations > _MAX_MATRIX_COMBINATIONS:
        sizes = " x ".join(str(len(params)) for params in param_lists)
        return [], [
            *findings,
            _finding(
                ctx,
                f"matrix generator would produce {total_combinations} combinations "
                f"({sizes}) — over this tool's {_MAX_MATRIX_COMBINATIONS} safety limit, "
                "not computed rather than risking an expensive or unbounded cartesian "
                "product.",
            ),
        ]

    combined: list[dict[str, str]] = [{}]
    for params in param_lists:
        combined = [{**base, **entry} for base in combined for entry in params]
    return combined, findings


def _resolve_merge(
    merge_generator: dict[str, Any], ctx: GeneratorContext
) -> tuple[list[dict[str, str]], list[Finding]]:
    merge_keys = [str(key) for key in (merge_generator.get("mergeKeys") or [])]
    if not merge_keys:
        return [], [
            _finding(
                ctx,
                "merge generator has no `mergeKeys` — matching semantics are "
                "unspecified upstream, this tool doesn't guess at them.",
            )
        ]

    children = merge_generator.get("generators") or []
    findings: list[Finding] = []
    child_results: list[tuple[list[dict[str, str]], bool]] = []

    for child in children:
        if not isinstance(child, dict):
            continue
        params, child_findings = _resolve_generator(child, ctx)
        findings.extend(child_findings)
        child_results.append((params, bool(child_findings)))

    if not child_results:
        return [], findings

    base_params, base_unresolvable = child_results[0]
    if base_unresolvable:
        # Without base entries there is nothing for later generators to merge into.
        return [], findings

    # Base entries are kept even without a match (see DESIGN.md). A later
    # generator only overrides entries whose merge keys match, and its other
    # entries are discarded. An unresolvable later generator contributes no
    # override; its finding already reports the gap.
    merged = [dict(entry) for entry in base_params]
    by_key = {tuple(entry.get(key, "") for key in merge_keys): entry for entry in merged}

    for params, was_unresolvable in child_results[1:]:
        if was_unresolvable:
            continue
        for entry in params:
            target = by_key.get(tuple(entry.get(key, "") for key in merge_keys))
            if target is not None:
                target.update(entry)

    return merged, findings


def _list_local_directories(repo_root: Path) -> list[str]:
    # Argo CD skips hidden directories and everything under them
    # (`walkDirectoryTree` in reposerver/repository/utils.go).
    directories = (path.relative_to(repo_root).as_posix() for path in walk_tree(repo_root))
    return [
        directory
        for directory in directories
        if (repo_root / directory).is_dir()
        and not any(segment.startswith(".") for segment in directory.split("/"))
    ]


def _list_local_files(repo_root: Path) -> list[str]:
    return [
        path.relative_to(repo_root).as_posix() for path in walk_tree(repo_root) if path.is_file()
    ]


def _directory_params(rel_path: str) -> dict[str, str]:
    basename = rel_path.rsplit("/", 1)[-1]
    params = {
        "path": rel_path,
        "path.basename": basename,
        "path.basenameNormalized": _normalized(basename),
    }
    # `{{path[0]}}`, `{{path[1]}}`...: the path split into segments.
    for index, segment in enumerate(rel_path.split("/")):
        params[f"path[{index}]"] = segment
    return params


def _normalized(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", name).strip("-").lower() or "x"


def _load_params_file(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            content = _yaml_safe.load(f)  # valid JSON is also valid YAML
    except (YAMLError, UnicodeDecodeError, OSError):
        return None
    # Not loaded through fsutil.load_yaml_documents, so the alias-bomb budget
    # is checked here before _flatten_params calls str() on the content.
    return content if is_within_budget(content) else None


def _flatten_params(obj: Any, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten_params(value, dotted))
    elif isinstance(obj, list):
        if prefix:
            flat[prefix] = str(obj)
    elif prefix:
        flat[prefix] = "" if obj is None else str(obj)
    return flat


def _generator_template(generator: dict[str, Any]) -> dict[str, Any]:
    for body in generator.values():
        if isinstance(body, dict) and isinstance(body.get("template"), dict):
            return body["template"]
    return {}


def _merged(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _merged(base.get(key), value) if key in base else value
        return merged
    return override if override not in (None, {}) else base


def _checked_fields(rendered: dict[str, Any]) -> list[Any]:
    """The fields the rules read. Others, such as Helm values, may hold their
    own `{{ }}` syntax that is not an ApplicationSet placeholder."""
    spec = _mapping(rendered.get("spec"))
    sources = _template_sources(spec)
    keys = ("repoURL", "path", "targetRevision", "chart")
    return [
        _mapping(rendered.get("metadata")).get("name"),
        spec.get("destination"),
        *(source.get(key) for source in sources for key in keys),
    ]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _template_sources(spec: dict[str, Any]) -> list[dict[str, Any]]:
    sources = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    if not isinstance(sources, list):
        return []
    return [source for source in sources if isinstance(source, dict)]


def _possible_source_paths(spec: dict[str, Any], local_origin: str | None) -> tuple[str, ...]:
    """Folders the Applications of `spec` may deploy from, "" meaning anywhere.

    Taken from the template's source paths up to their first placeholder, or
    from the git generators' paths when a template path starts with one.
    """
    spec = _mapping(spec)
    generators = list(_all_generators(spec.get("generators")))
    templates = [_mapping(spec.get("template"))]
    templates += [_generator_template(generator) for generator in generators]
    generator_paths = {
        _literal_folder(str(entry.get("path", "")))
        for generator in generators
        for git in [_mapping(generator.get("git"))]
        if is_local_repo_url(str(git.get("repoURL", "")), local_origin)
        for key in ("directories", "files")
        for entry in git.get(key) or []
        if isinstance(entry, dict) and not entry.get("exclude")
    }
    paths: set[str] = set()
    for template in templates:
        for source in _template_sources(_mapping(template.get("spec"))):
            repo_url = str(source.get("repoURL", ""))
            path = source.get("path")
            if not isinstance(path, str):
                continue
            if "{{" not in repo_url and not is_local_repo_url(repo_url, local_origin):
                continue
            folder = _literal_folder(path)
            paths |= {folder} if folder else (generator_paths or {""})
    return tuple(sorted(paths))


def _all_generators(generators: Any) -> Iterator[dict[str, Any]]:
    for generator in generators if isinstance(generators, list) else []:
        if not isinstance(generator, dict):
            continue
        yield generator
        for combining in ("matrix", "merge"):
            yield from _all_generators(_mapping(generator.get(combining)).get("generators"))


def _literal_folder(pattern: str) -> str:
    """The folder of `pattern` before its first placeholder or glob character."""
    cuts = [index for index in (pattern.find(c) for c in "{*?[") if index != -1]
    if cuts:
        pattern = pattern[: min(cuts)].rpartition("/")[0]
    folder = posixpath.normpath(pattern.strip("/") or ".")
    return "" if folder == "." else folder


def _unresolved_placeholders(value: Any) -> set[str]:
    if isinstance(value, str):
        return {f"`{{{{{name}}}}}`" for name in _PLACEHOLDER_RE.findall(value)}
    if isinstance(value, dict):
        return set().union(*(_unresolved_placeholders(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_unresolved_placeholders(v) for v in value))
    return set()


def _substitute(value: Any, params: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER_RE.sub(lambda m: params.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {key: _substitute(v, params) for key, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, params) for v in value]
    return value


def _finding(ctx: GeneratorContext, message: str, *, severity: Severity | None = None) -> Finding:
    return Finding(
        rule_id=RULE_ID,
        severity=ctx.severity if severity is None else severity,
        application=ctx.appset_name,
        message=message,
        file=ctx.source_file,
    )
