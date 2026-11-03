from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from argocd_source_lint.models import Application, Finding, Severity, Source
from argocd_source_lint.policy import Policy


class Rule(ABC):
    rule_id: str

    @abstractmethod
    def check(
        self,
        applications: list[Application],
        repo_root: Path,
        policy: Policy,
        local_origin: str | None,
    ) -> list[Finding]: ...


def external_source_finding(rule_id: str, app: Application, source: Source) -> Finding:
    """Build the `info` finding for a source in an external repo.

    External repos are out of scope, see DESIGN.md.
    """
    return Finding(
        rule_id=rule_id,
        severity=Severity.INFO,
        application=app.name,
        message=(
            f"Source `{source.path}` (external repo): out of scope v1 — this tool "
            "only verifies sources in the repo it runs in, see DESIGN.md."
        ),
        file=app.source_file,
        line=source.line,
        external_source=True,
    )
