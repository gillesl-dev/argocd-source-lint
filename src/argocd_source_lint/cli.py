from __future__ import annotations

import os
import sys
from enum import Enum
from pathlib import Path
from typing import NoReturn

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from ruamel.yaml.error import YAMLError

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
from argocd_source_lint.fsutil import unreadable_argocd_files
from argocd_source_lint.git_context import clear_caches as clear_git_caches
from argocd_source_lint.git_context import (
    get_origin_url,
    is_git_available,
    is_local_repo_url,
    set_repo_urls,
    treat_branch_as_checkout,
)
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import (
    DEFAULT_RULE_SEVERITIES,
    RULE_DESCRIPTIONS,
    RULES_DOC_URL,
    Policy,
    load_policy,
)
from argocd_source_lint.reporters import github, gitlab_codequality, json_report, junit, sarif
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
from argocd_source_lint.rules.orphan_source import OrphanSourceRule, soften_for_unexpanded_appsets
from argocd_source_lint.rules.phantom_target import PhantomTargetRule
from argocd_source_lint.rules.project_scope import ProjectScopeViolationRule
from argocd_source_lint.rules.revision_mismatch import RevisionMismatchRule
from argocd_source_lint.rules.sync_validation_disabled import SyncValidationDisabledRule
from argocd_source_lint.rules.unknown_resource_hook import UnknownResourceHookRule
from argocd_source_lint.rules.unknown_sync_option import UnknownSyncOptionRule
from argocd_source_lint.suppressions import (
    ANNOTATION_PREFIX,
    annotation_problems,
    split_by_annotations,
)

app = typer.Typer(add_completion=False, no_args_is_help=True)
# Off a terminal, as in CI logs, rich falls back to 80 columns and truncates
# rule IDs and paths. COLUMNS still wins when set.
console = Console(width=None if sys.stdout.isatty() or "COLUMNS" in os.environ else 200)
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
    GITHUB = "github"


def _print_version_and_exit(show: bool) -> None:
    if show:
        console.print(tool_version())
        raise typer.Exit()


def _print_rules_and_exit(show: bool) -> None:
    if show:
        for rule_id, description in RULE_DESCRIPTIONS.items():
            severity = DEFAULT_RULE_SEVERITIES[rule_id].value
            print(f"{rule_id:<37} {severity:<8} {description}")
        print(f"\nDetails: {RULES_DOC_URL}")
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
    list_rules: bool = typer.Option(
        False,
        "--list-rules",
        callback=_print_rules_and_exit,
        is_eager=True,
        help="List every rule with its default severity and exit.",
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
    only: list[str] | None = typer.Option(
        None, "--only", help="Run only this rule. Repeat for several rules."
    ),
    skip: list[str] | None = typer.Option(
        None, "--skip", help="Don't run this rule. Repeat for several rules."
    ),
    target_branch: str | None = typer.Option(
        None,
        "--target-branch",
        envvar=["GITHUB_BASE_REF", "CI_MERGE_REQUEST_TARGET_BRANCH_NAME"],
        help=(
            "Branch the change will be merged into. Applications tracking it are checked "
            "against the checkout instead of the branch. Read from GITHUB_BASE_REF or "
            "CI_MERGE_REQUEST_TARGET_BRANCH_NAME when set."
        ),
    ),
) -> None:
    """Analyze the repo's ArgoCD Applications and report detected issues."""
    selected_rules = _selected_rules(only or [], skip or [])
    if update_baseline and (only or skip):
        # A baseline written from a subset of rules would drop every other entry.
        console.print("[red]--write-baseline can't be combined with --only or --skip.[/red]")
        raise typer.Exit(code=2)
    repo_root = path.resolve()
    if not repo_root.is_dir():
        console.print(f"[red]Path not found: {repo_root}[/red]")
        raise typer.Exit(code=2)

    if not is_git_available():
        console.print(
            "[red]`git` was not found on PATH.[/red] The rules resolve every "
            "source through it. Install it (for example `apt-get install -y git` "
            "in a slim container image) and try again."
        )
        raise typer.Exit(code=2)

    clear_git_caches()
    treat_branch_as_checkout(target_branch or None)
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
    except UNREADABLE_FILE_ERRORS as exc:
        _exit_unreadable(repo_root / ".argocd-lint.yaml", exc)
    set_repo_urls(policy.repo_urls)
    discovery = RawManifestDiscovery()
    applications = discovery.discover(repo_root)
    local_origin = get_origin_url(repo_root)

    generated_apps, findings = applicationset.discover(
        repo_root,
        local_origin,
        policy.rules.get(applicationset.RULE_ID, Severity.INFO),
        new_git_file_globbing=policy.new_git_file_globbing,
    )
    applications += generated_apps

    unexpanded = [finding for finding in findings if finding.unexpanded_paths]
    findings = [finding for finding in findings if finding.rule_id in selected_rules]
    for rule in RULES:
        if rule.rule_id in selected_rules:
            findings.extend(rule.check(applications, repo_root, policy, local_origin))
    findings = soften_for_unexpanded_appsets(findings, unexpanded)

    findings, annotated = split_by_annotations(findings, applications)
    if annotated:
        err_console.print(
            f"[dim]{len(annotated)} finding(s) suppressed by "
            f"{ANNOTATION_PREFIX}<rule> annotations[/dim]"
        )

    if update_baseline:
        baseline_file = write_baseline(repo_root, findings)
        console.print(f"[green]{len(findings)}[/green] finding(s) accepted into {baseline_file}")
        raise typer.Exit(code=0)

    try:
        baseline = load_baseline(repo_root)
    except UNREADABLE_FILE_ERRORS as exc:
        _exit_unreadable(repo_root / DEFAULT_BASELINE_FILENAME, exc)
    new_findings, known_findings = split_by_baseline(findings, baseline)
    if known_findings:
        err_console.print(
            f"[dim]{len(known_findings)} finding(s) suppressed by "
            f"{repo_root / DEFAULT_BASELINE_FILENAME}[/dim]"
        )

    stale = stale_baseline_entries(findings, baseline)
    if stale:
        err_console.print(
            f"[yellow]{len(stale)} baseline entry(ies) no longer match any "
            "finding. Re-run --write-baseline to drop them.[/yellow]"
        )

    notices = _skipped_check_notices(
        applications, repo_root, policy, selected_rules, discovery.skipped, local_origin
    )
    _report(format, new_findings, applications, repo_root, output, notices, show_info)
    if format != OutputFormat.TABLE:
        # On stderr, so a machine-readable report on stdout stays parseable.
        for notice in notices:
            err_console.print(f"[yellow]{notice}[/yellow]")

    if fail_on_empty and not applications:
        raise typer.Exit(code=1)
    raise typer.Exit(code=_exit_code(new_findings, policy))


# A config or baseline file that isn't valid YAML, or has the wrong shape.
UNREADABLE_FILE_ERRORS = (YAMLError, UnicodeDecodeError, ValueError)


def _exit_unreadable(path: Path, exc: Exception) -> NoReturn:
    reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    console.print(f"[red]Can't read {path}:[/red] {escape(visible_controls(reason))}")
    raise typer.Exit(code=2)


def _selected_rules(only: list[str], skip: list[str]) -> set[str]:
    known = set(DEFAULT_RULE_SEVERITIES)
    unknown = sorted(set(only + skip) - known)
    if unknown:
        names = ", ".join(escape(visible_controls(rule_id)) for rule_id in unknown)
        console.print(f"[red]Unknown rule(s): {names}.[/red] See --list-rules.")
        raise typer.Exit(code=2)
    return (set(only) if only else known) - set(skip)


def _skipped_check_notices(
    applications: list[Application],
    repo_root: Path,
    policy: Policy,
    selected_rules: set[str],
    skipped_manifests: list[tuple[Path, str]],
    local_origin: str | None,
) -> list[str]:
    """Notices for checks that did nothing, so a quiet report is not read as clean.

    The returned strings are already markup-escaped.
    """
    notices = annotation_problems(applications, set(DEFAULT_RULE_SEVERITIES))
    for path, reason in sorted(unreadable_argocd_files().items()):
        shown = path.relative_to(repo_root) if path.is_relative_to(repo_root) else path
        notices.append(
            f"`{escape(visible_controls(shown.as_posix()))}` is not valid YAML "
            f"({escape(visible_controls(reason))}), so the Argo CD resources in it "
            "may not have been checked."
        )
    for manifest, reason in skipped_manifests:
        notices.append(
            f"`{escape(visible_controls(manifest.as_posix()))}` is not a valid Application "
            f"({escape(visible_controls(reason))}), so it was not checked."
        )
    if not applications:
        notices.append(NO_APPLICATION_WARNING)
    path_sources = [s for app in applications for s in app.sources if s.path is not None]
    if path_sources and not any(is_local_repo_url(s.repo_url, local_origin) for s in path_sources):
        origin = escape(visible_controls(local_origin or "none"))
        notices.append(
            f"No Application source points to this repository (origin: `{origin}`), so "
            "every source was treated as external. If ArgoCD reaches it under another URL, "
            "list that URL under repo_urls in .argocd-lint.yaml."
        )
    if "orphan-source" not in selected_rules:
        return notices
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
    elif output_format == OutputFormat.GITHUB:
        text = github.render_findings(findings)
    else:
        text = gitlab_codequality.render_findings(findings)

    if output is None:
        _write_utf8_to_stdout(text)
    else:
        output.write_text(text, encoding="utf-8")


def _write_utf8_to_stdout(text: str) -> None:
    """Write `text` to stdout as UTF-8 whatever the console encoding is.

    `print()` uses stdout's encoding, cp1252 when redirected on Windows, but
    JUnit declares UTF-8 and JSON/SARIF consumers expect it.
    """
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
    # A single external Helm chart yields one identical info per rule, so
    # infos are summarized unless --show-info is set.
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
