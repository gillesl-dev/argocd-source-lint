from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from argocd_source_lint.fsutil import discover_documents
from argocd_source_lint.models import Application, IgnoreDiffRule, Source
from argocd_source_lint.suppressions import ANNOTATION_PREFIX


class RawManifestDiscovery:
    """Finds `kind: Application` documents in the repo's YAML files.

    Files that do not parse, such as Helm templates, are skipped.
    """

    def __init__(self) -> None:
        # (manifest path, reason) for Application documents that could not be read.
        self.skipped: list[tuple[Path, str]] = []

    def discover(self, repo_root: Path) -> list[Application]:
        applications: list[Application] = []
        for manifest_path, doc in discover_documents(repo_root):
            if not _is_argocd_application(doc):
                continue
            try:
                applications.append(build_application(doc, manifest_path, repo_root))
            except INVALID_MANIFEST_ERRORS as exc:
                self.skipped.append((_relative(manifest_path, repo_root), invalid_reason(exc)))
        return applications


# What a manifest with a wrong value type raises: a number or null where text is
# expected, or a mapping where a list is expected. The API server would reject
# such a manifest too.
INVALID_MANIFEST_ERRORS = (ValidationError, AttributeError, TypeError)


def invalid_reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        error = exc.errors()[0]
        field = ".".join(str(part) for part in error["loc"])
        return f"{field}: {error['msg']}"
    return str(exc) or type(exc).__name__


def _relative(path: Path, repo_root: Path) -> Path:
    try:
        return path.relative_to(repo_root)
    except ValueError:
        return path


def _is_argocd_application(doc: dict[str, Any]) -> bool:
    return doc.get("kind") == "Application" and str(doc.get("apiVersion", "")).startswith(
        "argoproj.io/"
    )


def build_application(doc: dict[str, Any], manifest_path: Path, repo_root: Path) -> Application:
    metadata = doc.get("metadata", {}) or {}
    spec = doc.get("spec", {}) or {}

    raw_sources: list[dict[str, Any]]
    if "sources" in spec:
        raw_sources = spec["sources"] or []
    elif "source" in spec:
        raw_sources = [spec["source"]]
    else:
        raw_sources = []

    sources = [build_source(raw) for raw in raw_sources]

    sync_policy = spec.get("syncPolicy") or {}
    automated = sync_policy.get("automated") or {}
    self_heal = bool(automated.get("selfHeal", False))
    self_heal_line = _key_line(automated, "selfHeal") if self_heal else None
    sync_options = list(sync_policy.get("syncOptions") or [])
    sync_options_line = _key_line(sync_policy, "syncOptions") if sync_options else None

    ignore_differences = [
        _build_ignore_diff_rule(raw) for raw in spec.get("ignoreDifferences", []) or []
    ]
    ignore_differences_line = _key_line(spec, "ignoreDifferences") if ignore_differences else None

    destination = spec.get("destination") or {}

    try:
        source_file = manifest_path.relative_to(repo_root)
    except ValueError:
        source_file = manifest_path

    return Application(
        name=metadata.get("name", ""),
        namespace=metadata.get("namespace") or "argocd",
        sources=sources,
        sync_policy_self_heal=self_heal,
        self_heal_line=self_heal_line,
        ignore_differences=ignore_differences,
        ignore_differences_line=ignore_differences_line,
        sync_options=sync_options,
        sync_options_line=sync_options_line,
        source_file=source_file,
        project=spec.get("project") or "default",
        project_line=_key_line(spec, "project"),
        destination_server=destination.get("server"),
        destination_name=destination.get("name"),
        destination_namespace=destination.get("namespace"),
        ignored_rules=_ignored_rules(metadata),
    )


def _ignored_rules(metadata: dict[str, Any]) -> dict[str, str]:
    annotations = metadata.get("annotations") or {}
    if not isinstance(annotations, dict):
        return {}
    return {
        str(key).removeprefix(ANNOTATION_PREFIX): str(value or "")
        for key, value in annotations.items()
        if str(key).startswith(ANNOTATION_PREFIX)
    }


def build_source(raw: dict[str, Any]) -> Source:
    helm = raw.get("helm") or {}
    directory = raw.get("directory") or {}
    value_files = helm.get("valueFiles", []) or []
    return Source(
        repo_url=raw.get("repoURL", ""),
        target_revision=raw.get("targetRevision") or "HEAD",
        path=raw.get("path"),
        chart=raw.get("chart"),
        ref=raw.get("ref"),
        helm_value_files=list(value_files),
        helm_value_files_lines=_item_lines(value_files),
        helm_ignore_missing_value_files=bool(helm.get("ignoreMissingValueFiles", False)),
        directory_recurse=bool(directory.get("recurse", False)),
        directory_include=directory.get("include"),
        directory_exclude=directory.get("exclude"),
        explicit_type=_explicit_type(raw),
        line=_line_of(raw),
    )


# The fields each `IsZero` in Argo CD's types.go checks. `NormalizeSource`
# (util/argo/argo.go) drops a block where none is set before `ExplicitType`
# runs, so `helm: {}` or `directory: {recurse: false}` leaves the type to
# auto-detection. A directory block keeps its include and exclude.
_SOURCE_TYPE_FIELDS = {
    "directory": ("recurse", "jsonnet", "disableExtensionFilter", "include", "exclude"),
    "helm": (
        "version",
        "releaseName",
        "valueFiles",
        "parameters",
        "fileParameters",
        "values",
        "valuesObject",
        "passCredentials",
        "ignoreMissingValueFiles",
        "skipCrds",
        "skipTests",
        "skipSchemaValidation",
        "kubeVersion",
        "apiVersions",
        "namespace",
    ),
    "kustomize": (
        "namePrefix",
        "nameSuffix",
        "version",
        "namespace",
        "images",
        "replicas",
        "commonLabels",
        "commonAnnotations",
        "patches",
        "components",
        "kubeVersion",
        "apiVersions",
        "ignoreMissingComponents",
    ),
}


def _sets_type(raw: dict[str, Any], key: str) -> bool:
    block = raw.get(key)
    if block is None:
        return False
    if key == "plugin":
        return True
    if not isinstance(block, dict):
        return bool(block)
    if key == "directory" and isinstance(block.get("jsonnet"), dict):
        jsonnet = block["jsonnet"]
        if not any(jsonnet.get(field) for field in ("extVars", "tlas", "libs")):
            block = {k: v for k, v in block.items() if k != "jsonnet"}
    return any(block.get(field) for field in _SOURCE_TYPE_FIELDS[key])


def _explicit_type(raw: dict[str, Any]) -> str | None:
    # Setting more than one is invalid in Argo CD: leave the type undetermined.
    types = [key for key in (*_SOURCE_TYPE_FIELDS, "plugin") if _sets_type(raw, key)]
    return types[0] if len(types) == 1 else None


def _line_of(node: Any) -> int | None:
    """1-indexed start line of a round-trip-parsed node, or None for a plain dict/list."""
    lc = getattr(node, "lc", None)
    line = getattr(lc, "line", None)
    return line + 1 if isinstance(line, int) else None


def _item_lines(seq: Any) -> list[int | None]:
    """1-indexed line of each item in a round-trip-parsed sequence."""
    lc = getattr(seq, "lc", None)
    if lc is None:
        return [None] * len(seq)
    lines: list[int | None] = []
    for index in range(len(seq)):
        try:
            line, _col = lc.item(index)
            lines.append(line + 1)
        except (KeyError, IndexError):
            lines.append(None)
    return lines


def _key_line(mapping: Any, key: str) -> int | None:
    """1-indexed line of `key` within a round-trip-parsed mapping."""
    lc = getattr(mapping, "lc", None)
    if lc is None:
        return None
    try:
        line, _col = lc.key(key)
        return line + 1
    except KeyError:
        return None


def _build_ignore_diff_rule(raw: dict[str, Any]) -> IgnoreDiffRule:
    return IgnoreDiffRule(
        group=raw.get("group"),
        kind=raw.get("kind", ""),
        name=raw.get("name"),
        namespace=raw.get("namespace"),
        json_pointers=raw.get("jsonPointers", []) or [],
        jq_path_expressions=raw.get("jqPathExpressions", []) or [],
        managed_fields_managers=raw.get("managedFieldsManagers", []) or [],
    )
