from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from argocd_source_lint.fsutil import iter_yaml_files, load_yaml_documents
from argocd_source_lint.git_context import (
    local_path_sources,
    materialize_revision,
    revision_matches_checkout,
)
from argocd_source_lint.models import Application, Source

# A Helm chart packaged in the repo: out of scope for v1 content
# interpretation (rendering a chart's templates is delegated to `helm
# template`/a dedicated linter, see DESIGN.md), so we can't know what is
# actually deployed. The whole subtree is treated as covered rather than
# flooding the user with false positives on a directory we can't
# interpret. Kustomize overlays get their own, non-opaque handling below.
_OPAQUE_TOOL_MARKERS = ("Chart.yaml",)

_KUSTOMIZATION_FILENAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")

# Keys under which `kustomization.yaml` lists a bare relative path (file or
# directory); resolved and marked covered, recursing into a directory
# that is itself a Kustomize overlay. A remote reference (git URL, SCM
# shorthand) simply won't resolve to anything on disk and is silently
# skipped: verifying it is out of scope, same principle as an external
# `Application` source (see DESIGN.md).
_KUSTOMIZATION_PATH_LIST_KEYS = ("resources", "bases", "components", "crds")


_COVERED_FILES_CACHE: dict[tuple[Path, Path, bool, str | None, str | None], tuple[Path, ...]] = {}


def covered_files_for_source(
    source_dir: Path, source: Source, base_root: Path | None = None
) -> Iterator[Path]:
    """Files actually covered by a `path` source, aligned with real ArgoCD
    behavior (`directory.recurse`/`include`/`exclude`, see DESIGN.md).
    Never called directly by a rule; only through
    `covered_files_for_application` (`orphan-source`, `double-coverage`)
    or `covered_documents_for_application` (`missing-ignore-diff`,
    `hpa-selfheal-conflict`, `unknown-sync-option`, `unknown-resource-hook`,
    `malformed-sync-wave`) below, seven rules in total; each of those
    independently walks the same source directory, so the result is
    cached by the only inputs it actually depends on (`source_dir`, plus
    the three `directory.*` fields that can change what's yielded from
    the same directory). Confirmed for real: 40 Applications x 15
    covered files each, x 3 rules that happened to reach this path in a
    minimal repro, cost ~4.9s of re-walking before this cache and the
    sibling one in `fsutil.load_yaml_documents` existed. Same reset
    story as every other cache here: `clear_caches`, called once at the
    start of `cli.lint`.

    `base_root` bounds a Kustomize overlay's own `resources`/`bases`/...
    references (see DESIGN.md "A Kustomize overlay can read outside the
    repo"); defaults to `source_dir` itself when not given (the
    direct-call/test case), so a reference can never escape further
    than wherever the walk started."""
    base_root = source_dir.resolve() if base_root is None else base_root
    key = (
        source_dir.resolve(),
        base_root,
        source.directory_recurse,
        source.directory_include,
        source.directory_exclude,
    )
    if key not in _COVERED_FILES_CACHE:
        _COVERED_FILES_CACHE[key] = tuple(_covered_files_for_source(source_dir, source, base_root))
    yield from _COVERED_FILES_CACHE[key]


def _covered_files_for_source(source_dir: Path, source: Source, base_root: Path) -> Iterator[Path]:
    if find_kustomization_file(source_dir) is not None:
        yield from covered_files_for_kustomize_dir(source_dir, base_root)
        return

    if is_opaque_tool_directory(source_dir):
        yield from iter_yaml_files(source_dir)
        return

    for candidate in iter_yaml_files(source_dir, recurse=source.directory_recurse):
        relative = candidate.relative_to(source_dir).as_posix()
        if source.directory_include and not match_directory_patterns(
            relative, source.directory_include
        ):
            continue
        if source.directory_exclude and match_directory_patterns(
            relative, source.directory_exclude
        ):
            continue
        yield candidate


def covered_files_for_application(
    app: Application, repo_root: Path, local_origin: str | None
) -> set[Path]:
    """Repo-relative identity of every file covered by any of `app`'s
    local `path` sources: the *identity* used for dedup/reporting by
    `orphan-source` and `double-coverage`, deliberately not tied to
    whether the file was actually found on disk at `repo_root` or in a
    snapshot of a divergent `targetRevision` (see `_resolved_source_root`
    and DESIGN.md "targetRevision drift"). Neither caller ever needs to
    open the file itself; `missing-ignore-diff` does, so it reads
    `covered_documents_for_application` instead."""
    covered: set[Path] = set()
    for source in local_path_sources(app, local_origin):
        resolved = _resolved_source_root(repo_root, source)
        if resolved is None:
            continue
        base_root, source_dir = resolved
        for found in covered_files_for_source(source_dir, source, base_root):
            covered.add(found.resolve().relative_to(base_root))
    return covered


def covered_documents_for_application(
    app: Application, repo_root: Path, local_origin: str | None
) -> Iterator[dict[str, Any]]:
    """Every YAML document actually covered by any of `app`'s local `path`
    sources, parsed; used exclusively by `missing-ignore-diff`, the only
    rule that needs real file content rather than just path identity: a
    source pinned to a divergent `targetRevision` is read from its
    materialized snapshot directly, never from the (possibly nonexistent)
    `repo_root`-shaped identity `covered_files_for_application` reports."""
    for source in local_path_sources(app, local_origin):
        resolved = _resolved_source_root(repo_root, source)
        if resolved is None:
            continue
        base_root, source_dir = resolved
        for found in covered_files_for_source(source_dir, source, base_root):
            yield from load_yaml_documents(found)


def _resolved_source_root(repo_root: Path, source: Source) -> tuple[Path, Path] | None:
    """`(base_root, source_dir)` for `source`: `base_root` is `repo_root`,
    or a materialized snapshot of `source.target_revision` when that
    differs from what's checked out. `None` if the resulting directory
    doesn't exist there, or if `source.path` (repo-controlled YAML,
    e.g. `path: ../../../etc`) would resolve *outside* `base_root`
    entirely: confirmed for real, not theoretical, this tool would
    otherwise walk and read arbitrary files anywhere on the host
    filesystem reachable from the repo root, not just inside it (see
    DESIGN.md "A Kustomize overlay can read outside the repo")."""
    base_root = repo_root
    if revision_matches_checkout(repo_root, source.target_revision) is False:
        snapshot_root = materialize_revision(repo_root, source.target_revision)
        if snapshot_root is None:
            return None
        base_root = snapshot_root

    base_root = base_root.resolve()
    source_dir = (base_root / source.path).resolve()
    if not source_dir.is_relative_to(base_root) or not source_dir.is_dir():
        return None
    return base_root, source_dir


def is_opaque_tool_directory(source_dir: Path) -> bool:
    return any((source_dir / marker).is_file() for marker in _OPAQUE_TOOL_MARKERS)


def find_kustomization_file(directory: Path) -> Path | None:
    for name in _KUSTOMIZATION_FILENAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def covered_files_for_kustomize_dir(
    directory: Path, base_root: Path | None = None, _seen: set[Path] | None = None
) -> set[Path]:
    """Files actually consumed by a Kustomize overlay: the
    `kustomization.yaml` itself, plus every local `resources`/`bases`/
    `components`/`crds` entry (recursing into a directory reference),
    every `patches`/`patchesStrategicMerge`/`patchesJson6902` file, and
    every `configMapGenerator`/`secretGenerator` `files`/`envs`/`envFile`
    entry. Anything in the directory but never referenced stays uncovered,
    the same `orphan-source` signal as a plain directory source, one
    level deeper.

    `base_root` bounds every reference resolved below; defaults to
    `directory` itself when not given (the direct-call/test case).
    Unlike an Application's own `path`, these come from *tracked YAML
    content*, not ArgoCD's own schema: a `resources: [../../../etc]`
    entry is otherwise resolved and walked exactly like a real one (see
    DESIGN.md "A Kustomize overlay can read outside the repo")."""
    base_root = directory.resolve() if base_root is None else base_root
    kustomization_file = find_kustomization_file(directory)
    if kustomization_file is None:
        return set()

    seen = set() if _seen is None else _seen
    resolved_dir = directory.resolve()
    if resolved_dir in seen:
        return set()  # guards against a `resources:` cycle
    seen.add(resolved_dir)

    covered = {kustomization_file.resolve()}
    docs = load_yaml_documents(kustomization_file)
    if not docs:
        return covered
    doc = docs[0]

    for key in _KUSTOMIZATION_PATH_LIST_KEYS:
        for entry in doc.get(key) or []:
            if isinstance(entry, str):
                covered |= _resolve_local_reference(directory, entry, base_root, seen)

    for entry in doc.get("patchesStrategicMerge") or []:
        if isinstance(entry, str):  # a dict entry is an inline patch, no file
            covered |= _resolve_local_reference(directory, entry, base_root, seen)

    for key in ("patches", "patchesJson6902"):
        for entry in doc.get(key) or []:
            path = (entry or {}).get("path") if isinstance(entry, dict) else None
            if path:
                covered |= _resolve_local_reference(directory, path, base_root, seen)

    for generator_key in ("configMapGenerator", "secretGenerator"):
        for generator in doc.get(generator_key) or []:
            covered |= _generator_referenced_files(directory, generator, base_root, seen)

    return covered


def _generator_referenced_files(
    directory: Path, generator: Any, base_root: Path, seen: set[Path]
) -> set[Path]:
    if not isinstance(generator, dict):
        return set()

    covered: set[Path] = set()
    for file_entry in generator.get("files") or []:
        # A `configMapGenerator`/`secretGenerator` file entry is either a
        # bare path or a `key=path` pair.
        _, _, rel_path = str(file_entry).rpartition("=")
        covered |= _resolve_local_reference(directory, rel_path, base_root, seen)

    env_entries = list(generator.get("envs") or [])
    if generator.get("envFile"):
        env_entries.append(generator["envFile"])
    for rel_path in env_entries:
        covered |= _resolve_local_reference(directory, rel_path, base_root, seen)

    return covered


def _resolve_local_reference(
    directory: Path, entry: str, base_root: Path, seen: set[Path]
) -> set[Path]:
    target = (directory / entry).resolve()
    if not target.is_relative_to(base_root):
        return set()  # escapes base_root: never resolved, real or not
    if target.is_dir():
        return covered_files_for_kustomize_dir(target, base_root, seen)
    if target.is_file():
        return {target}
    return set()  # doesn't exist locally: a remote reference, out of scope


def clear_caches() -> None:
    """Resets `covered_files_for_source`'s cache: called once at the
    start of `cli.lint`, same reasoning as `fsutil.clear_caches` and
    `git_context.clear_caches`."""
    _COVERED_FILES_CACHE.clear()


def match_directory_patterns(relative_posix: str, pattern: str) -> bool:
    """ArgoCD's `include`/`exclude`: a single glob pattern, or several
    comma-separated ones wrapped in braces (`{a,b}`). `fnmatchcase` (not
    `fnmatch`, which lowercases both sides on Windows) so a repo checked
    out on a case-sensitive CI runner and linted locally on Windows agree
    on the result, matching ArgoCD's own case-sensitive Go glob."""
    pattern = pattern.strip()
    if pattern.startswith("{") and pattern.endswith("}"):
        alternatives = pattern[1:-1].split(",")
    else:
        alternatives = [pattern]
    return any(fnmatch.fnmatchcase(relative_posix, alt.strip()) for alt in alternatives)
