from __future__ import annotations

from pathlib import Path
from typing import Any

from argocd_source_lint.fsutil import discover_documents
from argocd_source_lint.models import AppProject, AppProjectDestination


def discover_app_projects(repo_root: Path) -> list[AppProject]:
    """Walks the repo for `kind: AppProject` manifests, used only by
    `project-scope-violation`. A project referenced by an Application but
    not found here is either genuinely undeclared (ArgoCD's own
    permissive auto-created "default") or managed out-of-band in another
    repo: the rule itself decides which, this just reports what's here."""
    projects: list[AppProject] = []
    for manifest_path, doc in discover_documents(repo_root):
        if _is_app_project(doc):
            projects.append(_build_app_project(doc, manifest_path, repo_root))
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
