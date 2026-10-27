from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape

from argocd_source_lint import applicationset, tool_version
from argocd_source_lint.baseline import (
    DEFAULT_BASELINE_FILENAME,
    load_baseline,
    split_by_baseline,
    stale_baseline_entries,
    write_baseline,
)
from argocd_source_lint.coverage import clear_caches as clear_coverage_cache
from argocd_source_lint.fsutil import clear_caches as clear_discovery_cache
from argocd_source_lint.git_context import clear_caches as clear_git_caches
from argocd_source_lint.git_context import get_origin_url, is_git_available
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy, load_policy
from argocd_source_lint.reporters import gitlab_codequality, json_report, junit, sarif
from argocd_source_lint.reporters.safe_text import visible_controls
from argocd_source_lint.reporters.table import render_findings as render_table
from argocd_source_lint.reporters.table import render_info_summary
from argocd_source_lint.rules.base import Rule
from argocd_source_lint.rules.broken_values_ref import BrokenValuesRefRule
from argocd_source_lint.rules.double_coverage import DoubleCoverageRule
from argocd_source_lint.rules.duplicate_application_name import DuplicateApplicationNameRule
from argocd_source_lint.rules.floating_target_revision import FloatingTargetRevisionRule
from argocd_source_lint.rules.hpa_selfheal_conflict import HpaSelfHealConflictRule
from argocd_source_lint.rules.malformed_ignore_diff_jq_expression import (
    MalformedIgnoreDiffJqExpressionRule,
)
from argocd_source_lint.rules.malformed_ignore_diff_pointer import (
    MalformedIgnoreDiffPointerRule,
)
from argocd_source_lint.rules.malformed_sync_wave import MalformedSyncWaveRule
from argocd_source_lint.rules.missing_ignore_diff import MissingIgnoreDiffRule
from argocd_source_lint.rules.orphan_source import OrphanSourceRule
from argocd_source_lint.rules.phantom_target import PhantomTargetRule
from argocd_source_lint.rules.project_scope import ProjectScopeViolationRule
from argocd_source_lint.rules.revision_mismatch import RevisionMismatchRule
from argocd_source_lint.rules.sync_validation_disabled import SyncValidationDisabledRule
from argocd_source_lint.rules.unknown_resource_hook import UnknownResourceHookRule
from argocd_source_lint.rules.unknown_sync_option import UnknownSyncOptionRule

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)

RULES: list[Rule] = [
    OrphanSourceRule(),
    PhantomTargetRule(),
    BrokenValuesRefRule(),
    MissingIgnoreDiffRule(),
    DoubleCoverageRule(),
    RevisionMismatchRule(),
    ProjectScopeViolationRule(),
    HpaSelfHealConflictRule(),
    SyncValidationDisabledRule(),
    DuplicateApplicationNameRule(),
    MalformedIgnoreDiffPointerRule(),
    MalformedIgnoreDiffJqExpressionRule(),
    UnknownSyncOptionRule(),
    UnknownResourceHookRule(),
    MalformedSyncWaveRule(),
    FloatingTargetRevisionRule(),
]


NO_APPLICATION_WARNING = (
    "No ArgoCD Application found, so no Application was checked. Check the path "
    "being analyzed, or pass --fail-on-empty to make this fail CI."
)
ORPHAN_SOURCE_SKIPPED_NOTICE = (
    "orphan-source skipped: scan_roots is not set in .argocd-lint.yaml, so no "
    "file was checked for missing Application coverage."
)


class OutputFormat(str, Enum):
    TABLE = "table"
    JSON = "json"
    SARIF = "sarif"
    GITLAB_CODEQUALITY = "gitlab-codequality"
    JUNIT = "junit"


def _print_version_and_exit(show: bool) -> None:
    if show:
        console.print(tool_version())
        raise typer.Exit()


@app.command()
def lint(
    path: Path = typer.Argument(Path("."), help="Root of the Git repo to analyze"),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_print_version_and_exit,
        is_eager=True,
        help="Show the version and exit.",
    ),
    format: OutputFormat = typer.Option(OutputFormat.TABLE, "--format", "-f", help="Report format"),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Write the report to this file instead of stdout"
    ),
    update_baseline: bool = typer.Option(
        False,
        "--write-baseline",
        help=(
            "Accept every current finding into the baseline file instead of "
            "reporting them, so only new findings block CI from now on."
        ),
    ),
    fail_on_empty: bool = typer.Option(
        False,
        "--fail-on-empty",
        help="Exit with code 1 when no ArgoCD Application is found, instead of 0.",
    ),
    show_info: bool = typer.Option(
        False,
        "--show-info",
        help=(
            "List info findings in the table output instead of summarizing them. "
            "Other formats always include them."
        ),
    ),
) -> None:
    """Analyze the repo's ArgoCD Applications and report detected issues."""
    repo_root = path.resolve()
    if not repo_root.is_dir():
        console.print(f"[red]Path not found: {repo_root}[/red]")
        raise typer.Exit(code=2)

    if not is_git_available():
        console.print(
            "[red]`git` was not found on PATH.[/red] Every rule here resolves a "
            "source through it (revision lookups, tree listings) -- without it, "
            "results silently degrade into false positives instead of erroring, "
            "rather than the other way around. Install it (e.g. `apt-get install "
            "-y git` in a minimal container image) and try again."
        )
        raise typer.Exit(code=2)

    clear_git_caches()
    clear_discovery_cache()
    clear_coverage_cache()
    try:
        policy = load_policy(repo_root)
    except ValidationError as exc:
        console.print(f"[red]Invalid {repo_root / '.argocd-lint.yaml'}:[/red]")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            message = error["msg"].removeprefix("Value error, ")
            # The message can quote the invalid value from the repo.
            console.print(
                f"  [red]{escape(visible_controls(field))}:[/red] "
                f"{escape(visible_controls(message))}"
            )
        raise typer.Exit(code=2) from None
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)

    generated_apps, findings = applicationset.discover(
        repo_root, local_origin, policy.rules.get(applicationset.RULE_ID, Severity.INFO)
    )
    applications += generated_apps

    for rule in RULES:
        findings.extend(rule.check(applications, repo_root, policy, local_origin))

    if update_baseline:
        baseline_file = write_baseline(repo_root, findings)
        console.print(f"[green]{len(findings)}[/green] finding(s) accepted into {baseline_file}")
        raise typer.Exit(code=0)

    baseline = load_baseline(repo_root)
    new_findings, known_findings = split_by_baseline(findings, baseline)
    if known_findings:
        err_console.print(
            f"[dim]{len(known_findings)} finding(s) suppressed by "
            f"{repo_root / DEFAULT_BASELINE_FILENAME}[/dim]"
        )

    stale = stale_baseline_entries(findings, baseline)
    if stale:
        err_console.print(
            f"[yellow]{len(stale)} baseline entrie(s) no longer match any "
            "finding — re-run --write-baseline to drop them[/yellow]"
        )

    notices = _skipped_check_notices(applications, repo_root, policy)
    _report(format, new_findings, applications, repo_root, output, notices, show_info)
    if format != OutputFormat.TABLE:
        # stderr, so a JSON/SARIF/... report on stdout stays parseable.
        for notice in notices:
            err_console.print(f"[yellow]{notice}[/yellow]")

    if fail_on_empty and not applications:
        raise typer.Exit(code=1)
    raise typer.Exit(code=_exit_code(new_findings, policy))


def _skipped_check_notices(
    applications: list[Application], repo_root: Path, policy: Policy
) -> list[str]:
    """Checks that silently did nothing, so a quiet report isn't mistaken
    for a clean one. Already markup-escaped."""
    notices = []
    if not applications:
        notices.append(NO_APPLICATION_WARNING)
    if not policy.scan_roots:
        notices.append(ORPHAN_SOURCE_SKIPPED_NOTICE)
    for scan_root in policy.scan_roots:
        if not (repo_root / scan_root).resolve().is_dir():
            notices.append(
                f"scan_roots entry `{escape(visible_controls(scan_root))}` is not a "
                "directory in this repository, so orphan-source skipped it."
            )
    return notices


def _report(
    output_format: OutputFormat,
    findings: list[Finding],
    applications: list[Application],
    repo_root: Path,
    output: Path | None,
    notices: list[str],
    show_info: bool,
) -> None:
    if output_format == OutputFormat.TABLE:
        _report_table(findings, applications, repo_root, output, notices, show_info)
        return

    if output_format == OutputFormat.JSON:
        text = json_report.render_findings(findings)
    elif output_format == OutputFormat.SARIF:
        text = sarif.render_findings(findings)
    elif output_format == OutputFormat.JUNIT:
        text = junit.render_findings(findings)
    else:
        text = gitlab_codequality.render_findings(findings)

    if output is None:
        _write_utf8_to_stdout(text)
    else:
        output.write_text(text, encoding="utf-8")


def _write_utf8_to_stdout(text: str) -> None:
    """`print()` encodes with stdout's own encoding, cp1252 for a redirected
    stdout on Windows, while JUnit declares UTF-8 and JSON/SARIF consumers
    expect it: a `—` came out as the single byte 0x97."""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        print(text)
        return
    sys.stdout.flush()
    buffer.write((text + "\n").encode("utf-8"))
    buffer.flush()


def _report_table(
    findings: list[Finding],
    applications: list[Application],
    repo_root: Path,
    output: Path | None,
    notices: list[str],
    show_info: bool,
) -> None:
    count = len(applications)
    if output is None:
        console.print(f"[bold]{count}[/bold] ArgoCD Application(s) discovered under {repo_root}\n")
        _render_table_body(console, findings, applications, notices, show_info)
        return

    with output.open("w", encoding="utf-8") as f:
        file_console = Console(file=f, no_color=True, width=120)
        file_console.print(f"{count} ArgoCD Application(s) discovered under {repo_root}\n")
        _render_table_body(file_console, findings, applications, notices, show_info)


def _render_table_body(
    target: Console,
    findings: list[Finding],
    applications: list[Application],
    notices: list[str],
    show_info: bool,
) -> None:
    for notice in notices:
        target.print(f"[yellow]{notice}[/yellow]")
    if notices:
        target.print()
    # One external Helm chart alone yields 7 identical infos, one per rule:
    # summarized unless asked for (see render_info_summary).
    hidden = [] if show_info else [f for f in findings if f.severity == Severity.INFO]
    listed = findings if show_info else [f for f in findings if f.severity != Severity.INFO]
    # "No issues detected" with no Application would read as a clean
    # result when nothing was checked at all.
    if applications or listed:
        render_table(target, listed)
    render_info_summary(target, hidden)


def _exit_code(findings: list[Finding], policy: Policy) -> int:
    for finding in findings:
        if finding.severity == Severity.ERROR:
            return 1
        if finding.severity == Severity.UNVERIFIABLE and policy.unverifiable_blocks_ci:
            return 1
    return 0


def main() -> None:
    app()


if __name__ == "__main__":
    main()
