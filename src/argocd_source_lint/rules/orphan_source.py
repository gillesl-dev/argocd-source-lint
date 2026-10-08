from __future__ import annotations

import fnmatch
from pathlib import Path

from argocd_source_lint.coverage import covered_files_for_application
from argocd_source_lint.fsutil import iter_yaml_files
from argocd_source_lint.git_context import external_path_sources, is_local_repo_url
from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule, external_source_finding
from argocd_source_lint.rules.broken_values_ref import parse_ref_entry, ref_sources_by_name

RULE_ID = "orphan-source"
IGNORE_MARKER = "argocd-lint:ignore"


class OrphanSourceRule(Rule):
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
        covered: set[Path] = set()

        for app in applications:
            for source in external_path_sources(app, local_origin):
                findings.append(external_source_finding(RULE_ID, app, source))

            covered.update(covered_files_for_application(app, repo_root, local_origin))

            # A Helm values file referenced via `$ref/path.yaml` in
            # `helm.valueFiles` (a very common pattern: external chart +
            # values from this repo) is covered even when the source that
            # references it has no `path` of its own; otherwise every
            # "chart + $values" Application produces a systematic false
            # positive.
            for rel_path in _referenced_value_file_paths(app, local_origin):
                covered.add(Path(rel_path))

        for scan_root in policy.scan_roots:
            scan_dir = (repo_root / scan_root).resolve()
            if not scan_dir.is_dir():
                continue
            for candidate in iter_yaml_files(scan_dir):
                resolved = candidate.resolve()
                relative = resolved.relative_to(repo_root)
                if relative in covered:
                    continue
                if _is_excluded(relative, policy.exclude_paths):
                    continue
                if _has_ignore_marker(resolved):
                    continue
                findings.append(
                    Finding(
                        rule_id=RULE_ID,
                        severity=severity,
                        application="",
                        message=(
                            f"File not covered by any Application source: {relative.as_posix()}"
                        ),
                        file=relative,
                    )
                )

        return findings


def _referenced_value_file_paths(app: Application, local_origin: str | None) -> set[str]:
    ref_sources = ref_sources_by_name(app)
    paths: set[str] = set()
    for source in app.sources:
        for entry in source.helm_value_files:
            ref_name, rel_path = parse_ref_entry(entry)
            if ref_name is None:
                continue
            ref_source = ref_sources.get(ref_name)
            if ref_source is None:
                continue  # nonexistent $ref: broken-values-ref will report it
            if is_local_repo_url(ref_source.repo_url, local_origin):
                paths.add(rel_path)
    return paths


def _is_excluded(relative: Path, exclude_paths: list[str]) -> bool:
    # `fnmatchcase` (not `fnmatch`, which lowercases both sides on
    # Windows): `.argocd-lint.yaml` is committed and run on a
    # case-sensitive CI runner as much as locally, so `exclude_paths`
    # must match the same files regardless of the host OS.
    posix_path = relative.as_posix()
    return any(fnmatch.fnmatchcase(posix_path, pattern) for pattern in exclude_paths)


def _has_ignore_marker(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return IGNORE_MARKER in text
