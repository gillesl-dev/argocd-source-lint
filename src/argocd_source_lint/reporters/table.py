from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from argocd_source_lint.models import Finding, Severity

_SEVERITY_STYLE = {
    Severity.ERROR: "bold red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
    Severity.UNVERIFIABLE: "bold magenta",
}


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
        # `rich.table.Table.add_row` parses every string argument as
        # rich markup by default; confirmed for real, not assumed: an
        # Application name or a finding message containing `[link=...]`
        # rendered as an actual clickable hyperlink, and `[bold red on
        # white]`-style tags actually re-styled the output. Every field
        # below can contain repo-controlled content (an Application's
        # own `metadata.name`, a filename, a rule's own message text
        # quoting something from the manifest); `escape()` so a
        # crafted repo can't spoof or restyle this tool's own terminal
        # output. `finding.severity.value`/`style` are never repo
        # content (a closed enum/style map this tool controls), so
        # that one stays real markup.
        table.add_row(
            f"[{style}]{finding.severity.value}[/{style}]",
            escape(finding.rule_id),
            escape(finding.application or "-"),
            escape(location),
            escape(finding.message),
        )
    console.print(table)
