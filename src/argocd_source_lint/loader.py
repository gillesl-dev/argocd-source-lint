from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from argocd_source_lint.fsutil import discover_documents
from argocd_source_lint.models import Application, IgnoreDiffRule, Source


class ApplicationDiscovery(ABC):
    @abstractmethod
    def discover(self, repo_root: Path) -> list[Application]: ...


class RawManifestDiscovery(ApplicationDiscovery):
    """Walks YAML files containing `kind: Application`. Files that don't
    parse as valid YAML (e.g. Helm templates using Go syntax) are silently
    skipped: they aren't raw Application manifests anyway."""

    def discover(self, repo_root: Path) -> list[Application]:
        applications: list[Application] = []
        for manifest_path, doc in discover_documents(repo_root):
            if _is_argocd_application(doc):
                applications.append(build_application(doc, manifest_path, repo_root))
        return applications


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
    )


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
        line=_line_of(raw),
    )


def _line_of(node: Any) -> int | None:
    """1-indexed line where a round-trip-parsed mapping/sequence starts;
    `None` for a plain dict/list (e.g. a `Source` built directly in a
    test, without going through YAML at all)."""
    lc = getattr(node, "lc", None)
    line = getattr(lc, "line", None)
    return line + 1 if isinstance(line, int) else None


def _item_lines(seq: Any) -> list[int | None]:
    """1-indexed line of each item in a round-trip-parsed sequence, same
    order/index as `seq` itself."""
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
