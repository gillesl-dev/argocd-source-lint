from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console

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
from argocd_source_lint.reporters.table import render_findings as render_table
from argocd_source_lint.rules.base import Rule
from argocd_source_lint.rules.broken_values_ref import BrokenValuesRefRule
from argocd_source_lint.rules.double_coverage import DoubleCoverageRule
from argocd_source_lint.rules.duplicate_application_name import DuplicateApplicationNameRule
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
]


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
            console.print(f"  [red]{field}:[/red] {message}")
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

    _report(format, new_findings, applications, repo_root, output)

    raise typer.Exit(code=_exit_code(new_findings, policy))


def _report(
    output_format: OutputFormat,
    findings: list[Finding],
    applications: list[Application],
    repo_root: Path,
    output: Path | None,
) -> None:
    if output_format == OutputFormat.TABLE:
        _report_table(findings, applications, repo_root, output)
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
        print(text)
    else:
        output.write_text(text, encoding="utf-8")


def _report_table(
    findings: list[Finding],
    applications: list[Application],
    repo_root: Path,
    output: Path | None,
) -> None:
    count = len(applications)
    if output is None:
        console.print(f"[bold]{count}[/bold] ArgoCD Application(s) discovered under {repo_root}\n")
        render_table(console, findings)
        return

    with output.open("w", encoding="utf-8") as f:
        file_console = Console(file=f, no_color=True, width=120)
        file_console.print(f"{count} ArgoCD Application(s) discovered under {repo_root}\n")
        render_table(file_console, findings)


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
