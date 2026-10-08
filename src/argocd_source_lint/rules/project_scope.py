from __future__ import annotations

import fnmatch
from pathlib import Path

from argocd_source_lint.app_projects import discover_app_projects
from argocd_source_lint.git_context import normalize_repo_url
from argocd_source_lint.globs import match_glob
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
    """An Application's `spec.project` restricts which repos it may sync
    from and which destinations it may sync to
    (`AppProject.spec.sourceRepos`/`destinations`); ArgoCD refuses to
    sync outside that scope. From a Git-only point of view that failure
    is silent: nothing in the repo itself looks wrong, it only shows up
    as a live Application condition once ArgoCD tries. Fully static:
    string/glob matching against manifests already in the repo, no
    cluster access needed (see DESIGN.md)."""

    rule_id = RULE_ID

    def check(
        self,
        applications: list[Application],
        repo_root: Path,
        policy: Policy,
        local_origin: str | None,
    ) -> list[Finding]:
        severity = policy.rules.get(RULE_ID, Severity.ERROR)
        projects_by_name = {p.name: p for p in discover_app_projects(repo_root)}
        findings: list[Finding] = []

        for app in applications:
            project = projects_by_name.get(app.project)
            if project is None:
                # "default" not found in the repo: assumed to be
                # ArgoCD's own auto-created, permissive one; flagging
                # it would be noise on the overwhelming majority of
                # repos that never declare it at all.
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
        return []  # nothing declared to compare, not this rule's business

    if not project.destinations:
        return [
            _finding(
                app, severity, f"AppProject `{project.name}` declares no `destinations` at all."
            )
        ]
    if any((d.server or d.name or d.namespace or "").startswith("!") for d in project.destinations):
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
    if destination.namespace is None:
        return True
    return _destination_field_matches(destination.namespace, app.destination_namespace or "")


def _source_repo_matches(pattern: str, repo_url: str) -> bool:
    """ArgoCD's sourceRepos matching, confirmed against
    `AppProject.IsSourcePermitted`'s actual source: a bare `*` always
    matches unconditionally (bypasses the glob entirely, a special case
    in ArgoCD's own `globMatch` wrapper); otherwise a `/`-segment-bounded
    glob (`*` within one segment, `**` crosses) against both sides
    normalized the same way `is_local_repo_url` already does elsewhere
    in this tool, so a harmless URL-form difference (ssh vs https,
    trailing `.git`) isn't a false violation."""
    if pattern == "*":
        return True
    return match_glob(normalize_repo_url(pattern), normalize_repo_url(repo_url))


def _destination_field_matches(pattern: str, value: str) -> bool:
    """ArgoCD's destinations matching (`server`/`name`/`namespace`),
    confirmed against `AppProject.isDestinationMatched`'s actual source:
    an *unbounded* glob, no `/`-segment-awareness, unlike sourceRepos.
    `fnmatchcase` (not `fnmatch`, which lowercases on Windows) to stay
    correct regardless of the host OS: a namespace/server/cluster name
    is case-sensitive data, not a filesystem path."""
    return fnmatch.fnmatchcase(value, pattern)


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
