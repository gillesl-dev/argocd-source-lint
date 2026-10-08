from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "duplicate-application-name"


class DuplicateApplicationNameRule(Rule):
    """ArgoCD keys an `Application` by `(namespace, name)`: two
    manifests declaring the same pair silently overwrite/fight each
    other in the cluster, with nothing wrong visible from either
    manifest read on its own (see multiple real reports, e.g.
    argoproj/argo-cd#9420, #23808, #15874). Applies equally to two plain
    manifests, a plain manifest colliding with an `ApplicationSet`-generated
    one, or two generated entries from the same generator's own output;
    the `applications` list this rule receives already includes both
    (see `cli.py`)."""

    rule_id = RULE_ID

    def check(
        self,
        applications: list[Application],
        repo_root: Path,
        policy: Policy,
        local_origin: str | None,
    ) -> list[Finding]:
        severity = policy.rules.get(RULE_ID, Severity.ERROR)
        groups: dict[tuple[str, str], list[Application]] = defaultdict(list)

        for app in applications:
            if app.name:
                groups[(app.namespace, app.name)].append(app)

        findings = [
            _finding(name, namespace, apps, severity)
            for (namespace, name), apps in groups.items()
            if len(apps) > 1
        ]
        findings.sort(key=lambda f: f.file.as_posix())
        return findings


def _finding(name: str, namespace: str, apps: list[Application], severity: Severity) -> Finding:
    # An ApplicationSet generating several colliding entries from its own
    # generator all share the same source_file (the ApplicationSet's own
    # manifest, see DESIGN.md "Finding.line"), de-duplicated so it's
    # only listed once.
    files = sorted({app.source_file.as_posix() for app in apps})

    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        application=name,
        message=(
            f"{len(apps)} Applications named `{name}` in namespace `{namespace}` -- "
            "ArgoCD keys an Application by namespace+name, so one silently "
            f"overwrites/fights the other instead of both existing. Declared in: "
            f"{', '.join(files)}."
        ),
        file=Path(files[0]),
    )
