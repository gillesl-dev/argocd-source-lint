from __future__ import annotations

import re
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
from argocd_source_lint.globs import match_glob
from argocd_source_lint.loader import build_application
from argocd_source_lint.models import Application, Finding, Severity

RULE_ID = "unresolvable-generator"


@dataclass(frozen=True, slots=True)
class GeneratorContext:
    """Everything a generator resolver needs besides its own generator
    dict, threaded unchanged through every level of `_resolve_generator`'s
    recursion (`matrix`/`merge` calling back into it for each child);
    bundled here instead of six positional parameters repeated across
    every one of those functions."""

    repo_root: Path
    local_origin: str | None
    appset_name: str
    source_file: Path
    severity: Severity


# Classic ApplicationSet templating (`{{key}}`, valyala/fasttemplate).
# `spec.goTemplate: true` switches to Go template syntax instead, which is
# a different rendering engine entirely and out of scope v1 (see below).
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.\-]+)\s*\}\}")

_yaml_safe = YAML(typ="safe")


def discover(
    repo_root: Path, local_origin: str | None, severity: Severity
) -> tuple[list[Application], list[Finding]]:
    """Expands every `ApplicationSet` in the repo into the `Application`s
    its generators would produce, so the existing rules apply to them
    unchanged. A generator this tool can't resolve locally (requires a
    live cluster/API, or Go-template rendering) produces one `info`
    finding instead of guessing, following the same principle as an external
    `Application` source (see DESIGN.md)."""
    applications: list[Application] = []
    findings: list[Finding] = []

    for manifest_path, doc in discover_documents(repo_root):
        if not _is_application_set(doc):
            continue
        expanded_apps, doc_findings = _expand(doc, manifest_path, repo_root, local_origin, severity)
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
    )

    if spec.get("goTemplate"):
        return [], [
            _finding(
                ctx,
                "`goTemplate: true` (Go template rendering) is out of scope v1 — "
                "only the classic `{{key}}` substitution is supported.",
            )
        ]

    template = spec.get("template") or {}
    applications: list[Application] = []
    findings: list[Finding] = []

    for generator in spec.get("generators") or []:
        if not isinstance(generator, dict):
            continue
        param_sets, generator_findings = _resolve_generator(generator, ctx)
        findings.extend(generator_findings)
        for params in param_sets:
            applications.append(
                _build_generated_application(template, params, manifest_path, repo_root)
            )

    return applications, findings


def _build_generated_application(
    template: dict[str, Any], params: dict[str, str], manifest_path: Path, repo_root: Path
) -> Application:
    rendered = _substitute(template, params)
    doc = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": rendered.get("metadata") or {},
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
        return _resolve_list(generator.get("list") or {}), []
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


def _resolve_list(list_generator: dict[str, Any]) -> list[dict[str, str]]:
    elements = list_generator.get("elements") or []
    return [_flatten_params(element) for element in elements if isinstance(element, dict)]


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

    # The generator's own `revision` is independent of any generated
    # Application's `targetRevision` (confirmed against the upstream Git
    # generator docs). Discovering `directories`/`files` from the
    # checked-out working tree regardless was a real, silent-wrong-result
    # gap: a `revision` pinned away from HEAD would enumerate today's
    # directory structure, not the pinned one. Same snapshot mechanism as
    # `coverage._resolved_source_root` (DESIGN.md "targetRevision drift"),
    # reused here rather than a second implementation.
    revision = git_generator.get("revision") or "HEAD"
    matches_checkout = revision_matches_checkout(ctx.repo_root, revision)
    if matches_checkout is None:
        # `is None`, not `is False`: a revision that doesn't resolve at
        # all must never fall through as "matches HEAD" by default.
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

    param_sets: list[dict[str, str]] = []
    param_sets.extend(_resolve_git_directories(base_root, git_generator.get("directories") or []))
    param_sets.extend(_resolve_git_files(base_root, git_generator.get("files") or []))
    return param_sets, []


def _resolve_git_directories(repo_root: Path, entries: list[Any]) -> list[dict[str, str]]:
    all_dirs = _list_local_directories(repo_root)
    included: set[str] = set()
    excluded: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pattern = str(entry.get("path", ""))
        matches = {d for d in all_dirs if match_glob(pattern, d)}
        if entry.get("exclude"):
            excluded |= matches
        else:
            included |= matches

    return [_directory_params(d) for d in sorted(included - excluded)]


def _resolve_git_files(repo_root: Path, entries: list[Any]) -> list[dict[str, str]]:
    all_files = _list_local_files(repo_root)
    matched: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pattern = str(entry.get("path", ""))
        matched |= {f for f in all_files if match_glob(pattern, f)}

    param_sets: list[dict[str, str]] = []
    for rel_path in sorted(matched):
        content = _load_params_file(repo_root / rel_path)
        if content is None:
            continue
        params = _flatten_params(content)
        params.update(_directory_params(rel_path))
        param_sets.append(params)
    return param_sets


# Real-world matrix uses (environments x regions, clusters x apps) rarely
# reach even the low hundreds, so this is generous headroom, chosen the same way as
# fsutil's `_MAX_EXPANDED_NODES`: far below where the cost actually starts
# to matter. Confirmed for real, not theoretical: two `list` generators of
# 5,000 small elements each (comfortably under the alias-bomb node budget
# on their own; that budget catches a densely *aliased* document, not a
# large but flat one) produced 25,000,000 combinations in ~7s for the
# combine step alone, before a single generated Application is even built
# or run through a rule.
_MAX_MATRIX_COMBINATIONS = 10_000


def _resolve_matrix(
    matrix_generator: dict[str, Any], ctx: GeneratorContext
) -> tuple[list[dict[str, str]], list[Finding]]:
    children = matrix_generator.get("generators") or []

    if len(children) > 2:
        # ArgoCD's own matrix generator only supports combining exactly
        # two child generators: the controller reports an error on more
        # (see DESIGN.md), it doesn't just behave unpredictably. Guessing
        # at a 3+-way cartesian product here would report Applications
        # ArgoCD itself would never actually generate.
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

    for child in children:
        if not isinstance(child, dict):
            continue
        params, child_findings = _resolve_generator(child, ctx)
        findings.extend(child_findings)
        param_lists.append(params)

    if len(param_lists) < 2 or any(not params for params in param_lists):
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
        # No base entries to match against at all, same reasoning as
        # matrix's cross product being empty when a factor is empty.
        return [], findings

    # Base entries are kept even without a match in a later generator
    # (see DESIGN.md); a later generator only overrides fields on an
    # entry whose merge keys already match one from the base, and its
    # own non-matching entries are discarded rather than added as new
    # ones. An unresolvable later generator just contributes no override:
    # its own finding above already flags the gap, so this doesn't
    # silently drop the (fully known) base entries over it.
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
    return [
        path.relative_to(repo_root).as_posix() for path in walk_tree(repo_root) if path.is_dir()
    ]


def _list_local_files(repo_root: Path) -> list[str]:
    return [
        path.relative_to(repo_root).as_posix() for path in walk_tree(repo_root) if path.is_file()
    ]


def _directory_params(rel_path: str) -> dict[str, str]:
    basename = rel_path.rsplit("/", 1)[-1]
    normalized = re.sub(r"[^A-Za-z0-9-]", "-", basename).strip("-").lower() or "x"
    return {"path": rel_path, "path.basename": basename, "path.basenameNormalized": normalized}


def _load_params_file(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            content = _yaml_safe.load(f)  # valid JSON is also valid YAML
    except (YAMLError, UnicodeDecodeError, OSError):
        return None
    # This bypasses fsutil.load_yaml_documents' own budget check (a
    # `files:` generator target isn't a `kind: Application`-shaped
    # document, so it never goes through that path); checked directly
    # instead of letting a YAML alias bomb reach _flatten_params' str().
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
