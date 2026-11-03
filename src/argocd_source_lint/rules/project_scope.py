from __future__ import annotations

import fnmatch
from collections import defaultdict
from pathlib import Path

from argocd_source_lint.app_projects import discover_app_projects
from argocd_source_lint.git_context import argo_normalized_git_url
from argocd_source_lint.globs import expand_braces, match_glob
from argocd_source_lint.models import (
    Application,
    AppProject,
    AppProjectDestination,
    Finding,
    Severity,
)
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "project-scope-violation"


class ProjectScopeViolationRule(Rule):
    """Detect sources or destinations not permitted by the Application's AppProject.

    Argo CD refuses to sync outside `sourceRepos` and `destinations`, but
    the refusal only appears as a live Application condition, never in the
    repo. Only AppProjects declared in this repo are checked (see DESIGN.md).
    """

    rule_id = RULE_ID

    def check(
        self,
        applications: list[Application],
        repo_root: Path,
        policy: Policy,
        local_origin: str | None,
    ) -> list[Finding]:
        severity = policy.rules.get(RULE_ID, Severity.ERROR)
        projects_by_name: dict[str, list[AppProject]] = defaultdict(list)
        for discovered in discover_app_projects(repo_root):
            projects_by_name[discovered.name].append(discovered)
        findings: list[Finding] = []

        for app in applications:
            candidates = projects_by_name.get(app.project, [])
            if len(candidates) > 1:
                # Picking one would check some Applications against
                # another folder's AppProject.
                files = ", ".join(sorted(p.source_file.as_posix() for p in candidates))
                findings.append(
                    _info(
                        app,
                        f"{len(candidates)} AppProjects named `{app.project}` in this repo "
                        f"({files}); cannot tell which one applies, scope not verified.",
                    )
                )
                continue
            project = candidates[0] if candidates else None
            if project is None:
                # Argo CD auto-creates a permissive "default" project, and
                # most repos never declare it.
                if app.project != "default":
                    findings.append(
                        _info(
                            app,
                            f"references AppProject `{app.project}`, not found in this "
                            "repo — likely managed elsewhere; sourceRepos/destinations "
                            "scope not verified.",
                        )
                    )
                continue

            findings.extend(_check_source_repos(app, project, severity))
            findings.extend(_check_destination(app, project, severity))

        return findings


def _check_source_repos(app: Application, project: AppProject, severity: Severity) -> list[Finding]:
    if not project.source_repos:
        return [
            _finding(
                app, severity, f"AppProject `{project.name}` declares no `sourceRepos` at all."
            )
        ]
    if any(pattern.startswith("!") for pattern in project.source_repos):
        return [
            _info(
                app,
                f"AppProject `{project.name}`'s sourceRepos has a negated (`!`) pattern — "
                "not evaluated, out of scope.",
            )
        ]

    findings: list[Finding] = []
    for repo_url in sorted({source.repo_url for source in app.sources}):
        if not any(_source_repo_matches(pattern, repo_url) for pattern in project.source_repos):
            findings.append(
                _finding(
                    app,
                    severity,
                    f"source `{repo_url}` is not permitted by AppProject `{project.name}`'s "
                    "sourceRepos — ArgoCD would refuse to sync it.",
                )
            )
    return findings


def _check_destination(app: Application, project: AppProject, severity: Severity) -> list[Finding]:
    if app.destination_server is None and app.destination_name is None:
        return []

    if not project.destinations:
        return [
            _finding(
                app, severity, f"AppProject `{project.name}` declares no `destinations` at all."
            )
        ]
    if any(
        (field or "").startswith("!")
        for d in project.destinations
        for field in (d.server, d.name, d.namespace)
    ):
        return [
            _info(
                app,
                f"AppProject `{project.name}`'s destinations has a negated (`!`) pattern — "
                "not evaluated, out of scope.",
            )
        ]

    comparable = [
        d
        for d in project.destinations
        if (app.destination_server is not None and d.server is not None)
        or (app.destination_name is not None and d.name is not None)
    ]
    if not comparable:
        declared = "`name`" if app.destination_server is None else "`server`"
        return [
            _info(
                app,
                f"AppProject `{project.name}` only declares destinations by {declared} — "
                "cannot cross-check `server` against `name` without live cluster access "
                "to resolve which cluster each refers to.",
            )
        ]

    if any(_destination_matches(app, d) for d in comparable):
        return []

    identity = app.destination_server or app.destination_name
    return [
        _finding(
            app,
            severity,
            f"destination `{identity}` (namespace `{app.destination_namespace}`) is not "
            f"permitted by AppProject `{project.name}`'s destinations — ArgoCD would "
            "refuse to sync it.",
        )
    ]


def _destination_matches(app: Application, destination: AppProjectDestination) -> bool:
    server_ok = (
        app.destination_server is not None
        and destination.server is not None
        and _destination_field_matches(destination.server, app.destination_server)
    )
    name_ok = (
        app.destination_name is not None
        and destination.name is not None
        and _destination_field_matches(destination.name, app.destination_name)
    )
    if not (server_ok or name_ok):
        return False
    # An entry without `namespace` only allows an empty one, as in Argo CD.
    return _destination_field_matches(destination.namespace or "", app.destination_namespace or "")


def _source_repo_matches(pattern: str, repo_url: str) -> bool:
    """Match a repo URL against a `sourceRepos` pattern like `AppProject.IsSourcePermitted`.

    A bare `*` matches anything. Otherwise the glob is `/`-segment-bounded
    (`**` crosses segments). Both sides are normalized as Argo CD does, which
    drops `.git` and the case but keeps the scheme: an `https://` pattern
    doesn't permit the `git@` URL of the same repository.
    """
    if pattern == "*":
        return True
    return match_glob(argo_normalized_git_url(pattern), argo_normalized_git_url(repo_url))


def _destination_field_matches(pattern: str, value: str) -> bool:
    """Match a destination field like `AppProject.isDestinationMatched`.

    Unlike sourceRepos, the glob is not bounded by `/`, and `{a,b}` works.
    Uses `fnmatchcase` because `fnmatch` lowercases on Windows and these
    values are case-sensitive.
    """
    alternatives = expand_braces(pattern)
    if alternatives is None:
        return True  # too large to expand: no false violation
    return any(fnmatch.fnmatchcase(value, p) for p in alternatives)


def _finding(app: Application, severity: Severity, message: str) -> Finding:
    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        application=app.name,
        message=message,
        file=app.source_file,
        line=app.project_line,
    )


def _info(app: Application, message: str) -> Finding:
    return _finding(app, Severity.INFO, message)
