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
from argocd_source_lint.globs import expand_braces
from argocd_source_lint.models import Application, Source

# A Helm chart's templates are not rendered (see DESIGN.md), so what it deploys
# is unknown. Its whole subtree counts as covered rather than reporting false
# positives. Kustomize overlays are resolved separately below.
_OPAQUE_TOOL_MARKERS = ("Chart.yaml",)

_KUSTOMIZATION_FILENAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")

# The files a directory source applies (`manifestFile` in Argo CD's
# reposerver/repository/repository.go). `.jsonnet` is evaluated there, not
# read as is, so it is left out.
_DIRECTORY_SOURCE_SUFFIXES = (".yaml", ".yml", ".json")

# Argo CD skips a file of a directory source that contains it.
SKIP_RENDERING_MARKER = "+argocd:skip-file-rendering"

# Keys under which `kustomization.yaml` lists relative file or directory paths.
# A remote reference (git URL, SCM shorthand) does not resolve on disk and is
# skipped, like an external Application source (see DESIGN.md).
_KUSTOMIZATION_PATH_LIST_KEYS = (
    "resources",
    "bases",
    "components",
    "crds",
    "configurations",
    # These also accept an inline YAML string, which is not on disk.
    "generators",
    "transformers",
    "validators",
)


_COVERED_FILES_CACHE: dict[
    tuple[Path, Path, bool, str | None, str | None, str | None], tuple[Path, ...]
] = {}


def covered_files_for_source(
    source_dir: Path, source: Source, base_root: Path | None = None
) -> Iterator[Path]:
    """Files covered by a `path` source, honoring `directory.recurse/include/exclude`.

    Several rules walk the same source directory, so results are cached
    until `clear_caches()`. `base_root` bounds Kustomize references (see
    DESIGN.md) and defaults
    to `source_dir`.
    """
    base_root = source_dir.resolve() if base_root is None else base_root
    key = (
        source_dir.resolve(),
        base_root,
        source.directory_recurse,
        source.directory_include,
        source.directory_exclude,
        source.explicit_type,
    )
    if key not in _COVERED_FILES_CACHE:
        _COVERED_FILES_CACHE[key] = tuple(_covered_files_for_source(source_dir, source, base_root))
    yield from _COVERED_FILES_CACHE[key]


def _covered_files_for_source(source_dir: Path, source: Source, base_root: Path) -> Iterator[Path]:
    # A plugin (CMP) gets the whole directory and may read any file in it.
    if source.explicit_type in ("helm", "plugin"):
        yield from iter_yaml_files(source_dir)
        return

    if source.explicit_type != "directory":
        if find_kustomization_file(source_dir) is not None:
            yield from covered_files_for_kustomize_dir(source_dir, base_root)
            return
        if is_opaque_tool_directory(source_dir):
            yield from iter_yaml_files(source_dir)
            return

    for candidate in iter_yaml_files(
        source_dir, recurse=source.directory_recurse, suffixes=_DIRECTORY_SOURCE_SUFFIXES
    ):
        if has_skip_rendering_marker(candidate):
            continue
        relative = candidate.relative_to(source_dir).as_posix()
        # A pattern too large to expand keeps the file: no false orphan.
        if source.directory_include and not match_directory_patterns(
            relative, source.directory_include, if_too_large=True
        ):
            continue
        if source.directory_exclude and match_directory_patterns(
            relative, source.directory_exclude, if_too_large=False
        ):
            continue
        yield candidate


def covered_files_for_application(
    app: Application, repo_root: Path, local_origin: str | None
) -> set[Path]:
    """Repo-relative paths of every file covered by `app`'s local `path` sources.

    These are identities for dedup and reporting: a file read from a
    `targetRevision` snapshot (DESIGN.md) may not
    exist at that path under `repo_root`.
    """
    return {
        identity for identity, _ in covered_file_paths_for_application(app, repo_root, local_origin)
    }


def covered_file_paths_for_application(
    app: Application, repo_root: Path, local_origin: str | None
) -> Iterator[tuple[Path, Path]]:
    """Yield `(repo-relative identity, readable path)` for every covered file.

    The readable path may be inside a snapshot of a divergent `targetRevision`.
    """
    for source in local_path_sources(app, local_origin):
        resolved = _resolved_source_root(repo_root, source)
        if resolved is None:
            continue
        base_root, source_dir = resolved
        for found in covered_files_for_source(source_dir, source, base_root):
            yield found.resolve().relative_to(base_root), found


# Kustomize fields that give the rendered resources another name or namespace
# than the files they come from.
_KUSTOMIZE_RENAMING_FIELDS = ("namePrefix", "nameSuffix", "namespace")


def renamed_by_kustomize(app: Application, repo_root: Path, local_origin: str | None) -> set[Path]:
    """Identities of files whose resources a source's own kustomization renames.

    Their names in the cluster differ from the files, so comparing the files
    says nothing about a conflict.
    """
    renamed: set[Path] = set()
    for source in local_path_sources(app, local_origin):
        resolved = _resolved_source_root(repo_root, source)
        if resolved is None:
            continue
        base_root, source_dir = resolved
        kustomization = find_kustomization_file(source_dir)
        if kustomization is None or source.explicit_type in ("directory", "helm", "plugin"):
            continue
        docs = load_yaml_documents(kustomization)
        if not any(field in doc for doc in docs for field in _KUSTOMIZE_RENAMING_FIELDS):
            continue
        for found in covered_files_for_source(source_dir, source, base_root):
            renamed.add(found.resolve().relative_to(base_root))
    return renamed


def covered_documents_for_application(
    app: Application, repo_root: Path, local_origin: str | None
) -> Iterator[dict[str, Any]]:
    """Parsed YAML documents covered by `app`'s local `path` sources.

    A source pinned to a divergent `targetRevision` is read from its snapshot.
    """
    for source in local_path_sources(app, local_origin):
        resolved = _resolved_source_root(repo_root, source)
        if resolved is None:
            continue
        base_root, source_dir = resolved
        for found in covered_files_for_source(source_dir, source, base_root):
            yield from load_yaml_documents(found)


def _resolved_source_root(repo_root: Path, source: Source) -> tuple[Path, Path] | None:
    """Return `(base_root, source_dir)` for `source`, or None if unusable.

    `base_root` is `repo_root`, or a snapshot of `source.target_revision`
    when it differs from the checkout. None when the directory is missing or
    `source.path` escapes `base_root`: it is repo-controlled, and `../../etc`
    would otherwise read host files (see DESIGN.md).
    """
    base_root = repo_root
    if revision_matches_checkout(repo_root, source.target_revision) is False:
        snapshot_root = materialize_revision(repo_root, source.target_revision)
        if snapshot_root is None:
            return None
        base_root = snapshot_root

    if source.path is None:
        return None
    base_root = base_root.resolve()
    source_dir = (base_root / source.path).resolve()
    if not source_dir.is_relative_to(base_root) or not source_dir.is_dir():
        return None
    return base_root, source_dir


def has_skip_rendering_marker(path: Path) -> bool:
    try:
        return SKIP_RENDERING_MARKER in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


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
    """Files consumed by a Kustomize overlay, including the kustomization itself.

    Covers local resources, patches and generator inputs, recursing into
    referenced directories. Unreferenced files stay uncovered. References
    outside `base_root` (default: `directory`) are ignored, since
    `resources: [../../../etc]` comes from repo content (see DESIGN.md).
    """
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

    referenced = [entry.get("path") for entry in _mappings(doc.get("replacements"))]
    referenced.append(_mapping(doc.get("openapi")).get("path"))
    helm_charts = _mappings(doc.get("helmCharts"))
    for chart in helm_charts:
        referenced.append(chart.get("valuesFile"))
        referenced += chart.get("additionalValuesFiles") or []
    for path in referenced:
        if isinstance(path, str) and path:
            covered |= _resolve_local_reference(directory, path, base_root, seen)

    if helm_charts:
        # Kustomize renders the charts it finds under `chartHome`.
        chart_home = _mapping(doc.get("helmGlobals")).get("chartHome") or "charts"
        target = (directory / str(chart_home)).resolve()
        if target.is_relative_to(base_root) and target.is_dir():
            covered |= {path.resolve() for path in iter_yaml_files(target)}

    return covered


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _mappings(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


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
    if "\n" in entry:
        return set()  # an inline generator or transformer
    try:
        target = (directory / entry).resolve()
    except (OSError, ValueError):
        return set()  # not a path on this system
    if not target.is_relative_to(base_root):
        return set()  # escapes base_root
    if target.is_dir():
        return covered_files_for_kustomize_dir(target, base_root, seen)
    if target.is_file():
        return {target}
    return set()  # not on disk: a remote reference


def clear_caches() -> None:
    """Reset the covered-files cache. Called at the start of each lint run."""
    _COVERED_FILES_CACHE.clear()


def match_directory_patterns(
    relative_posix: str, pattern: str, *, if_too_large: bool = False
) -> bool:
    """Match Argo CD's `include`/`exclude` glob, where `{a,b}` can appear anywhere.

    Uses `fnmatchcase` because `fnmatch` is case-insensitive on Windows,
    while Argo CD's Go glob is case-sensitive.
    """
    alternatives = expand_braces(pattern.strip())
    if alternatives is None:
        return if_too_large
    return any(fnmatch.fnmatchcase(relative_posix, alternative) for alternative in alternatives)
