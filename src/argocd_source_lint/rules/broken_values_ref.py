from __future__ import annotations

from pathlib import Path

from argocd_source_lint.git_context import (
    is_local_repo_url,
    is_revision_resolvable,
    tree_paths_at_revision,
)
from argocd_source_lint.models import Application, Finding, Severity, Source
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "broken-values-ref"


class BrokenValuesRefRule(Rule):
    rule_id = RULE_ID

    def check(
        self,
        applications: list[Application],
        repo_root: Path,
        policy: Policy,
        local_origin: str | None,
    ) -> list[Finding]:
        severity = policy.rules.get(RULE_ID, Severity.ERROR)
        findings: list[Finding] = []
        revision_resolvable: dict[str, bool] = {}

        for app in applications:
            ref_sources = ref_sources_by_name(app)

            for source in app.sources:
                for idx, entry in enumerate(source.helm_value_files):
                    entry_line = (
                        source.helm_value_files_lines[idx]
                        if idx < len(source.helm_value_files_lines)
                        else None
                    )

                    ref_name, rel_path = parse_ref_entry(entry)
                    if ref_name is None:
                        findings.extend(
                            _check_plain_entry(
                                app,
                                source,
                                entry,
                                entry_line,
                                repo_root,
                                local_origin,
                                severity,
                                revision_resolvable,
                            )
                        )
                        continue

                    ref_source = ref_sources.get(ref_name)
                    if ref_source is None:
                        findings.append(
                            _finding(
                                app,
                                severity,
                                f"`{entry}` references `$ref: {ref_name}`, but no source "
                                f"of the Application declares `ref: {ref_name}`.",
                                line=entry_line,
                            )
                        )
                        continue

                    if not is_local_repo_url(ref_source.repo_url, local_origin):
                        findings.append(
                            _finding(
                                app,
                                Severity.INFO,
                                f"`{entry}`: the `ref: {ref_name}` source points to an "
                                "external repo — out of scope v1, this tool only "
                                "verifies sources in the repo it runs in, see DESIGN.md.",
                                line=entry_line,
                            )
                        )
                        continue

                    revision = ref_source.target_revision
                    if revision not in revision_resolvable:
                        revision_resolvable[revision] = is_revision_resolvable(repo_root, revision)

                    if not revision_resolvable[revision]:
                        findings.append(
                            _finding(
                                app,
                                Severity.UNVERIFIABLE,
                                f"`{entry}`: revision `{revision}` (source `ref: "
                                f"{ref_name}`) missing from the local checkout — unable "
                                "to verify the file. Add `fetch-depth: 0` or fetch the "
                                "branch in question in CI.",
                                line=entry_line,
                            )
                        )
                        continue

                    if rel_path not in tree_paths_at_revision(repo_root, revision):
                        findings.append(
                            _finding(
                                app,
                                severity,
                                f"`{entry}`: file `{rel_path}` not found at revision "
                                f"`{revision}` (source `ref: {ref_name}`).",
                                line=entry_line,
                            )
                        )

        return findings


def _check_plain_entry(
    app: Application,
    source: Source,
    entry: str,
    entry_line: int | None,
    repo_root: Path,
    local_origin: str | None,
    severity: Severity,
    revision_resolvable: dict[str, bool],
) -> list[Finding]:
    """A plain (non-`$ref`) `helm.valueFiles` entry, resolved relative to
    this source's own `path` (the common case), and a real gap
    (confirmed by argoproj/argo-cd#4558, "New Applications with
    misconfiguration show up as Healthy"): a missing values file fails
    Helm template generation, but the Application can converge to a
    misleadingly healthy status instead of a clear sync error."""
    if source.helm_ignore_missing_value_files:
        return []  # ArgoCD itself silently tolerates a missing file here
    if not source.path or not is_local_repo_url(source.repo_url, local_origin):
        return []  # no local directory to resolve a plain path against

    revision = source.target_revision
    if revision not in revision_resolvable:
        revision_resolvable[revision] = is_revision_resolvable(repo_root, revision)
    if not revision_resolvable[revision]:
        return [
            _finding(
                app,
                Severity.UNVERIFIABLE,
                f"`{entry}`: revision `{revision}` missing from the local checkout — "
                "unable to verify the file. Add `fetch-depth: 0` or fetch the branch "
                "in question in CI.",
                line=entry_line,
            )
        ]

    full_path = f"{source.path.rstrip('/')}/{entry}"
    if full_path in tree_paths_at_revision(repo_root, revision):
        return []

    return [
        _finding(
            app,
            severity,
            f"`{entry}` (Helm valueFiles): file not found at `{full_path}` on revision "
            f"`{revision}` — Helm template generation fails, and ArgoCD can converge to "
            "a misleadingly healthy status instead of a clear sync error.",
            line=entry_line,
        )
    ]


def parse_ref_entry(entry: str) -> tuple[str | None, str]:
    """`$ref_name/rel/path.yaml` -> `(ref_name, "rel/path.yaml")`, or
    `(None, "")` for a plain (non-`$ref`) entry. Public: also used by
    `orphan-source`, which needs to recognize the same `$ref` entries to
    know a values file is covered even when its own source has no
    `path` (see DESIGN.md)."""
    if not entry.startswith("$"):
        return None, ""
    ref_name, separator, rel_path = entry[1:].partition("/")
    if not separator:
        return None, ""
    return ref_name, rel_path


def ref_sources_by_name(app: Application) -> dict[str, Source]:
    """`app`'s own sources that declare `ref:`, indexed by that name:
    how a `$ref_name/...` entry resolves to the source it points at.
    Public for the same reason as `parse_ref_entry`."""
    return {source.ref: source for source in app.sources if source.ref}


def _finding(
    app: Application, severity: Severity, message: str, line: int | None = None
) -> Finding:
    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        application=app.name,
        message=message,
        file=app.source_file,
        line=line,
    )
