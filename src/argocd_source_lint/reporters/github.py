from __future__ import annotations

from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.reporters.safe_text import visible_controls

# GitHub Actions workflow commands: `::error file=...,line=...,title=...::message`,
# which the runner turns into annotations on the pull request.
_COMMAND_BY_SEVERITY = {
    Severity.ERROR: "error",
    Severity.UNVERIFIABLE: "error",
    Severity.WARNING: "warning",
    Severity.INFO: "notice",
}


def render_findings(findings: list[Finding]) -> str:
    return "\n".join(_command(finding) for finding in findings)


def _command(finding: Finding) -> str:
    properties = {"file": finding.file.as_posix(), "title": finding.rule_id}
    if finding.line is not None:
        properties["line"] = str(finding.line)
    rendered = ",".join(f"{key}={_escape_property(value)}" for key, value in properties.items())
    command = _COMMAND_BY_SEVERITY[finding.severity]
    return f"::{command} {rendered}::{_escape_data(finding.message)}"


# Same escaping as escapeData/escapeProperty in actions/toolkit
# (packages/core/src/command.ts). Escaping newlines keeps a repo from starting
# a second workflow command; other control characters are made visible.
def _escape_data(value: str) -> str:
    return visible_controls(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")
