from __future__ import annotations

import dataclasses
import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argocd_source_lint import appset_go_params, appset_params, gotemplate
from argocd_source_lint.appset_go_params import UnsupportedParamsError
from argocd_source_lint.appset_params import LimitError, ParamsError
from argocd_source_lint.fsutil import discover_documents, walk_tree
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
    # Tree listings by (kind, root): a chained matrix resolves its second git
    # generator once per entry of the first, which must not walk the tree each time.
    listings: dict[tuple[str, Path], list[str]] = field(
        default_factory=dict, compare=False, hash=False
    )
    revisions: set[str] = field(default_factory=set, compare=False, hash=False)
    # goTemplate: params keep their types, and matrix and merge use mergo.
    go_template: bool = False
    go_template_options: tuple[str, ...] = ()
    # The manifest's lines, to tell quoted scalars from plain ones (YAML 1.1).
    source_lines: tuple[str, ...] = ()
    # Set once check_manifest_elements has checked the list elements.
    elements_checked: bool = False
    # Shared by every string the ApplicationSet renders (gotemplate bounds).
    go_template_budget: gotemplate.Budget = field(
        default_factory=gotemplate.Budget, compare=False, hash=False
    )

    @property
    def rendering(self) -> appset_go_params.Rendering:
        return appset_go_params.Rendering(self.go_template_options, self.go_template_budget)


# An ApplicationSet comes from the repository, which may not be trusted: every
# expansion is bounded, and going over a bound is reported, not computed.
_MAX_GENERATED_APPLICATIONS = 10_000
_MAX_PARAMS_FILE_BYTES = 1024 * 1024
_MAX_PARAMS_BYTES_PER_GENERATOR = 16 * 1024 * 1024
_MAX_GIT_REVISIONS = 20


# Classic ApplicationSet templating (`{{key}}`, valyala/fasttemplate).
# `spec.goTemplate: true` uses a different engine and is not supported.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.\-\[\]]+)\s*\}\}")


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
        go_template=bool(spec.get("goTemplate")),
        go_template_options=tuple(str(option) for option in spec.get("goTemplateOptions") or ()),
        source_lines=_source_lines(manifest_path) if spec.get("goTemplate") else (),
    )

    if spec.get("templatePatch"):
        return [], [
            _finding(
                ctx,
                "`templatePatch` is not supported, so the Applications this ApplicationSet "
                "generates are not checked.",
            )
        ]

    try:
        return _expand_generators(spec, ctx, manifest_path, repo_root, metadata)
    except ParamsError as exc:
        # GenerateApplications returns the first generator error, and the
        # controller then stops before creating or updating any Application
        # (applicationset_controller.go, Reconcile).
        return [], [
            _finding(
                ctx,
                f"Argo CD can't expand this ApplicationSet: {exc}. The controller stops "
                "before creating or updating any of its Applications.",
            )
        ]
    except LimitError as exc:
        return [], [
            _finding(
                ctx,
                f"ApplicationSet not expanded: {exc}, over this tool's limit. The "
                "Applications it generates are not checked.",
            )
        ]
    except UnsupportedParamsError as exc:
        # Never a partial expansion: Applications rendered from the other
        # param sets could be ones Argo CD does not create.
        return [], [
            _finding(
                ctx,
                f"ApplicationSet not expanded, not supported by this tool: {exc}. The "
                "Applications it generates are not checked.",
            )
        ]


def _source_lines(manifest_path: Path) -> tuple[str, ...]:
    # Split on LF only, as ruamel counts lines.
    try:
        return tuple(manifest_path.read_text(encoding="utf-8").split("\n"))
    except (OSError, UnicodeDecodeError):
        return ()


def _expand_generators(
    spec: dict[str, Any],
    ctx: GeneratorContext,
    manifest_path: Path,
    repo_root: Path,
    metadata: dict[str, Any],
) -> tuple[list[Application], list[Finding]]:
    template = spec.get("template") or {}
    appset_namespace = metadata.get("namespace") or "argocd"
    applications: list[Application] = []
    findings: list[Finding] = []

    for generator in spec.get("generators") or []:
        if not isinstance(generator, dict):
            continue
        param_sets, generator_findings = _resolve_generator(generator, ctx)
        findings.extend(generator_findings)
        if len(applications) + len(param_sets) > _MAX_GENERATED_APPLICATIONS:
            raise LimitError(
                f"it would generate more than {_MAX_GENERATED_APPLICATIONS} Applications"
            )
        # A generator can carry its own `template`, merged over the spec's.
        effective_template = _merged(template, _generator_template(generator))
        for params in param_sets:
            if ctx.go_template:
                # RenderTemplateParams leaves the template as it is without params.
                rendered = (
                    appset_go_params.render_template(effective_template, params, ctx.rendering)
                    if params
                    else effective_template
                )
                try:
                    applications.append(
                        _application_from(rendered, manifest_path, repo_root, appset_namespace)
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
                continue
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
    params: dict[str, Any],
    manifest_path: Path,
    repo_root: Path,
    appset_namespace: str,
) -> Application:
    return _application_from(
        _substitute(template, params), manifest_path, repo_root, appset_namespace
    )


def _application_from(
    rendered: dict[str, Any], manifest_path: Path, repo_root: Path, appset_namespace: str
) -> Application:
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
) -> tuple[list[dict[str, Any]], list[Finding]]:
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
) -> tuple[list[dict[str, Any]], list[Finding]]:
    elements = list_generator.get("elements") or []
    if not isinstance(elements, list):
        raise ParamsError("the list generator's `elements` is not a list")
    if ctx.go_template:
        lines = None if ctx.elements_checked else ctx.source_lines
        return appset_go_params.list_param_sets(
            elements, list_generator.get("elementsYaml"), lines
        ), []
    return appset_params.list_param_sets(elements, list_generator.get("elementsYaml")), []


def _resolve_git(
    git_generator: dict[str, Any], ctx: GeneratorContext
) -> tuple[list[dict[str, Any]], list[Finding]]:
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
    revision = str(git_generator.get("revision") or "HEAD")
    # Each new revision costs Git commands, and a chained matrix can set one
    # per entry of its first child.
    ctx.revisions.add(revision)
    if len(ctx.revisions) > _MAX_GIT_REVISIONS:
        raise LimitError(f"its git generators use more than {_MAX_GIT_REVISIONS} revisions")
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

    prefix = str(git_generator.get("pathParamPrefix") or "")
    values = git_generator.get("values")
    if values is not None and not isinstance(values, dict):
        raise ParamsError("the git generator's `values` is not a map")

    # With both set, Argo CD uses `directories` and ignores `files`.
    directories = git_generator.get("directories") or []
    if directories:
        found = _resolve_git_directories(ctx, base_root, directories)
        if ctx.go_template:
            rendering = ctx.rendering
            return [
                appset_go_params.directory_params(directory, prefix, values, rendering)
                for directory in found
            ], []
        return [
            appset_params.directory_params(directory, prefix, values) for directory in found
        ], []
    files = git_generator.get("files") or []
    return _resolve_git_files(ctx, base_root, files, prefix, values), []


def _listing(ctx: GeneratorContext, kind: str, root: Path) -> list[str]:
    key = (kind, root)
    if key not in ctx.listings:
        lister = _list_local_directories if kind == "directories" else _list_local_files
        ctx.listings[key] = lister(root)
    return ctx.listings[key]


def _resolve_git_directories(
    ctx: GeneratorContext, repo_root: Path, entries: list[Any]
) -> list[str]:
    all_dirs = _listing(ctx, "directories", repo_root)
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

    return sorted(included - excluded)


def _resolve_git_files(
    ctx: GeneratorContext,
    repo_root: Path,
    entries: list[Any],
    prefix: str,
    values: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    # Argo CD's `LsFiles` in util/git/client.go: `git ls-files` by default,
    # doublestar with the ApplicationSet controller's new globbing flag.
    match = match_doublestar if ctx.new_git_file_globbing else match_git_pathspec
    all_files = _listing(ctx, "files", repo_root)
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

    param_sets: list[dict[str, Any]] = []
    bytes_read = 0
    for rel_path in sorted(matched):
        path = repo_root / rel_path
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > _MAX_PARAMS_FILE_BYTES:
            raise LimitError(f"`{rel_path}` is over {_MAX_PARAMS_FILE_BYTES // 1024} KiB")
        bytes_read += size
        if bytes_read > _MAX_PARAMS_BYTES_PER_GENERATOR:
            raise LimitError(
                f"the git files generator matches over "
                f"{_MAX_PARAMS_BYTES_PER_GENERATOR // (1024 * 1024)} MiB of files"
            )
        try:
            text = path.read_text(encoding="utf-8")
            if ctx.go_template:
                param_sets.extend(
                    appset_go_params.git_file_param_sets(
                        rel_path, text, prefix, values, ctx.rendering
                    )
                )
            else:
                param_sets.extend(appset_params.git_file_param_sets(rel_path, text, prefix, values))
        except (OSError, UnicodeDecodeError, ParamsError) as exc:
            raise ParamsError(f"`{rel_path}` of the git files generator: {exc}") from exc
    return param_sets


# Real matrices (environments x regions, clusters x apps) rarely reach the
# low hundreds. The YAML node budget does not bound this: two flat lists of
# 5,000 elements each pass it but combine into 25 million entries.
_MAX_MATRIX_COMBINATIONS = 10_000


def _resolve_matrix(
    matrix_generator: dict[str, Any], ctx: GeneratorContext
) -> tuple[list[dict[str, Any]], list[Finding]]:
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
    param_lists: list[list[dict[str, Any]]] = []
    children = [child for child in children if isinstance(child, dict)]
    if len(children) < 2:
        return [], [
            _finding(
                ctx, "matrix generator has fewer than 2 child generators, so it is not expanded."
            )
        ]

    first, second = children
    if ctx.go_template:
        return _resolve_go_matrix(first, second, ctx)
    first_params, first_findings = _resolve_generator(first, ctx)
    findings.extend(first_findings)
    if _unresolved_placeholders(second):
        # The second generator uses the first one's parameters (`{{path}}`), so it
        # is resolved once per entry of the first, as Argo CD does.
        if len(first_params) > _MAX_MATRIX_COMBINATIONS:
            raise LimitError(
                f"the matrix generator's first child has more than {_MAX_MATRIX_COMBINATIONS} "
                "entries to resolve its second child with"
            )
        combined_chained: list[dict[str, Any]] = []
        for entry in first_params:
            params, child_findings = _resolve_generator(_substitute(second, entry), ctx)
            # The same gap found for every entry is reported once.
            findings.extend(f for f in child_findings if f not in findings)
            combined_chained.extend(
                appset_params.combine_string_maps(entry, other) for other in params
            )
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

    first_list, second_list = param_lists
    combined = [
        appset_params.combine_string_maps(base, entry)
        for base in first_list
        for entry in second_list
    ]
    return combined, findings


def _resolve_go_matrix(
    first: dict[str, Any], second: dict[str, Any], ctx: GeneratorContext
) -> tuple[list[dict[str, Any]], list[Finding]]:
    """A matrix in goTemplate mode (MatrixGenerator.GenerateParams): the
    second child is rendered with each non-empty entry of the first, and each
    pair is merged with mergo, the first child's values winning."""
    first_params, findings = _resolve_generator(first, ctx)
    if len(first_params) > _MAX_MATRIX_COMBINATIONS:
        raise LimitError(
            f"the matrix generator's first child has more than {_MAX_MATRIX_COMBINATIONS} entries"
        )
    # Kubernetes reads the manifest once, before any rendering.
    appset_go_params.check_manifest_elements(second, ctx.source_lines)
    checked_ctx = dataclasses.replace(ctx, elements_checked=True)
    combined: list[dict[str, Any]] = []
    for entry in first_params:
        # Transform interpolates the child only with non-empty params.
        child = appset_go_params.render_generator(second, entry, ctx.rendering) if entry else second
        params, child_findings = _resolve_generator(child, checked_ctx)
        # The same gap found for every entry is reported once.
        findings.extend(f for f in child_findings if f not in findings)
        for other in params:
            merged = appset_go_params.mergo_merge(appset_go_params.mergo_merge({}, other), entry)
            combined.append(merged)
        if len(combined) > _MAX_MATRIX_COMBINATIONS:
            raise LimitError(
                f"the matrix generator produces more than {_MAX_MATRIX_COMBINATIONS} combinations"
            )
    return combined, findings


def _resolve_merge(
    merge_generator: dict[str, Any], ctx: GeneratorContext
) -> tuple[list[dict[str, Any]], list[Finding]]:
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
    children = [child for child in children if isinstance(child, dict)]
    if len(children) < 2:
        raise ParamsError("the merge generator has fewer than 2 child generators")
    findings: list[Finding] = []
    child_results: list[tuple[dict[str, dict[str, Any]], bool]] = []

    for child in children:
        params, child_findings = _resolve_generator(child, ctx)
        findings.extend(child_findings)
        child_results.append((_by_merge_key(params, merge_keys), bool(child_findings)))

    base_by_key, base_unresolvable = child_results[0]
    if base_unresolvable:
        # Without base entries there is nothing for later generators to merge into.
        return [], findings

    # Base entries are kept even without a match (see DESIGN.md). A later
    # generator only overrides entries whose merge keys match, and its other
    # entries are discarded. An unresolvable later generator contributes no
    # override; its finding already reports the gap.
    merged = {key: dict(entry) for key, entry in base_by_key.items()}
    for by_key, was_unresolvable in child_results[1:]:
        if was_unresolvable:
            continue
        for key, entry in by_key.items():
            if key in merged and ctx.go_template:
                merged[key] = appset_go_params.mergo_merge(merged[key], entry)
            elif key in merged:
                merged[key].update(entry)

    return list(merged.values()), findings


def _by_merge_key(
    param_sets: list[dict[str, Any]], merge_keys: list[str]
) -> dict[str, dict[str, Any]]:
    """Param sets by their merge key values, which must be unique within one
    child generator (getParamSetsByMergeKey in generators/merge.go)."""
    by_key: dict[str, dict[str, Any]] = {}
    for params in param_sets:
        key = appset_params.merge_key(params, merge_keys)
        if key in by_key:
            raise ParamsError(f"two entries of a merge generator child have the merge key {key}")
        by_key[key] = params
    return by_key


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


def _substitute(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str):
        # Only a string param fills a placeholder; Argo CD leaves the others
        # (an `elementsYaml` number or map) as they are.
        return _PLACEHOLDER_RE.sub(lambda m: _string_param(params, m), value)
    if isinstance(value, dict):
        return {key: _substitute(v, params) for key, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, params) for v in value]
    return value


def _string_param(params: dict[str, Any], match: re.Match[str]) -> str:
    value = params.get(match.group(1))
    return value if isinstance(value, str) else match.group(0)


def _finding(ctx: GeneratorContext, message: str, *, severity: Severity | None = None) -> Finding:
    return Finding(
        rule_id=RULE_ID,
        severity=ctx.severity if severity is None else severity,
        application=ctx.appset_name,
        message=message,
        file=ctx.source_file,
    )
