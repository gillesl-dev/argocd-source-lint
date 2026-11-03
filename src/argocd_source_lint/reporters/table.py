from __future__ import annotations

from collections import Counter

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.reporters.safe_text import visible_controls

_SEVERITY_STYLE = {
    Severity.ERROR: "bold red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
    Severity.UNVERIFIABLE: "bold magenta",
}


def render_info_summary(console: Console, infos: list[Finding]) -> None:
    """Print a count of hidden `info` findings, one line per kind.

    External sources are counted once, not once per rule that skipped them.
    """
    if not infos:
        return
    external = [finding for finding in infos if finding.external_source]
    other = Counter(finding.rule_id for finding in infos if not finding.external_source)

    console.print(f"{len(infos)} info finding(s) not listed. Pass --show-info to list them.")
    if external:
        sources = {
            (finding.application, finding.file.as_posix(), finding.line, finding.message)
            for finding in external
        }
        rules = {finding.rule_id for finding in external}
        console.print(
            f"  {len(sources)} Application source(s) in another repo, not inspected: "
            f"{len(external)} finding(s) across {len(rules)} rule(s)"
        )
    for rule_id, count in sorted(other.items()):
        console.print(f"  {count} {escape(visible_controls(rule_id))}")


def render_findings(console: Console, findings: list[Finding]) -> None:
    if not findings:
        console.print("[green]No issues detected.[/green]")
        return

    table = Table(show_lines=False)
    table.add_column("Severity")
    table.add_column("Rule")
    table.add_column("Application")
    table.add_column("File")
    table.add_column("Message")

    for finding in findings:
        style = _SEVERITY_STYLE.get(finding.severity, "")
        location = finding.file.as_posix()
        if finding.line is not None:
            location += f":{finding.line}"
        # add_row parses strings as Rich markup, and these fields carry
        # repo content: escape them so a crafted repo cannot inject links
        # or styles. escape() lets control characters through, hence
        # visible_controls. Severity and style are ours, so stay markup.
        table.add_row(
            f"[{style}]{finding.severity.value}[/{style}]",
            escape(visible_controls(finding.rule_id)),
            escape(visible_controls(finding.application or "-")),
            escape(visible_controls(location)),
            escape(visible_controls(finding.message)),
        )
    console.print(table)
