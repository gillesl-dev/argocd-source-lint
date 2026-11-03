from __future__ import annotations

from pathlib import Path
from typing import Any

from argocd_source_lint.fsutil import discover_documents
from argocd_source_lint.loader import INVALID_MANIFEST_ERRORS
from argocd_source_lint.models import AppProject, AppProjectDestination


def discover_app_projects(repo_root: Path) -> list[AppProject]:
    """Every `kind: AppProject` manifest in the repo.

    A referenced project missing here may be Argo CD's implicit "default" or
    managed elsewhere; the calling rule decides which.
    """
    projects: list[AppProject] = []
    for manifest_path, doc in discover_documents(repo_root):
        if not _is_app_project(doc):
            continue
        try:
            projects.append(_build_app_project(doc, manifest_path, repo_root))
        except INVALID_MANIFEST_ERRORS:
            # Unreadable project: Applications using it get project-scope-violation's
            # "not found in this repo" info.
            continue
    return projects


def _is_app_project(doc: dict[str, Any]) -> bool:
    return doc.get("kind") == "AppProject" and str(doc.get("apiVersion", "")).startswith(
        "argoproj.io/"
    )


def _build_app_project(doc: dict[str, Any], manifest_path: Path, repo_root: Path) -> AppProject:
    metadata = doc.get("metadata") or {}
    spec = doc.get("spec") or {}

    destinations = [
        AppProjectDestination(
            server=entry.get("server"),
            name=entry.get("name"),
            namespace=entry.get("namespace"),
        )
        for entry in spec.get("destinations") or []
        if isinstance(entry, dict)
    ]

    try:
        source_file = manifest_path.relative_to(repo_root)
    except ValueError:
        source_file = manifest_path

    return AppProject(
        name=metadata.get("name", ""),
        source_repos=[str(entry) for entry in spec.get("sourceRepos") or []],
        destinations=destinations,
        source_file=source_file,
    )
