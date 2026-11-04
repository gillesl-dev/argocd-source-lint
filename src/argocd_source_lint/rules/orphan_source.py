from __future__ import annotations

import fnmatch
import posixpath
from pathlib import Path

from argocd_source_lint.coverage import covered_files_for_application, has_skip_rendering_marker
from argocd_source_lint.fsutil import iter_yaml_files
from argocd_source_lint.git_context import (
    external_path_sources,
    is_local_repo_url,
    tracked_paths,
)
from argocd_source_lint.globs import match_glob
from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule, external_source_finding
from argocd_source_lint.rules.broken_values_ref import (
    expand_build_env,
    is_glob,
    parse_ref_entry,
    ref_sources_by_name,
    value_file_path,
)

RULE_ID = "orphan-source"
IGNORE_MARKER = "argocd-lint:ignore"


class OrphanSourceRule(Rule):
    """Detect YAML files under the scan roots that no Application deploys.

    Argo CD only syncs what a source covers, so a manifest outside every
    source path is never applied and nothing reports it.
    """

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
        covered_globs: set[str] = set()

        for app in applications:
            for source in external_path_sources(app, local_origin):
                findings.append(external_source_finding(RULE_ID, app, source))

            covered.update(covered_files_for_application(app, repo_root, local_origin))

            # A values file referenced as `$ref/path.yaml` is covered even
            # though its source has no `path`, as in the common pattern of
            # an external chart with values from this repo.
            for rel_path in _referenced_value_file_paths(app, local_origin):
                if is_glob(rel_path):
                    covered_globs |= {rel_path, rel_path.replace("/**/", "/")}
                else:
                    covered.add(Path(rel_path))

        # An untracked or ignored file never reaches Argo CD, so it can't be
        # orphaned.
        tracked = tracked_paths(repo_root)
        for scan_root in policy.scan_roots:
            scan_dir = (repo_root / scan_root).resolve()
            if not scan_dir.is_dir():
                continue
            for candidate in iter_yaml_files(scan_dir):
                resolved = candidate.resolve()
                relative = resolved.relative_to(repo_root)
                if tracked is not None and relative.as_posix() not in tracked:
                    continue
                if relative in covered:
                    continue
                if any(_glob_covers(p, relative.as_posix()) for p in covered_globs):
                    continue
                if _is_excluded(relative, policy.exclude_paths):
                    continue
                if _has_ignore_marker(resolved) or has_skip_rendering_marker(resolved):
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


def soften_for_unexpanded_appsets(
    findings: list[Finding], unexpanded: list[Finding]
) -> list[Finding]:
    """Report as `info` the orphans an ApplicationSet in `unexpanded` may deploy.

    Its Applications were not generated, so their files look orphaned.
    """
    folders: dict[str, str] = {}
    for finding in unexpanded:
        for folder in finding.unexpanded_paths:
            folders.setdefault(folder, finding.application)

    softened: list[Finding] = []
    for finding in findings:
        path = finding.file.as_posix()
        owner = next(
            (
                name
                for folder, name in folders.items()
                if not folder or path.startswith(folder + "/")
            ),
            None,
        )
        if finding.rule_id == RULE_ID and finding.severity != Severity.INFO and owner is not None:
            finding = finding.model_copy(
                update={
                    "severity": Severity.INFO,
                    "message": (
                        f"{finding.message}. ApplicationSet `{owner}` may deploy it, but its "
                        "Applications could not be generated here."
                    ),
                }
            )
        softened.append(finding)
    return softened


def _referenced_value_file_paths(app: Application, local_origin: str | None) -> set[str]:
    ref_sources = ref_sources_by_name(app)
    paths: set[str] = set()
    for source in app.sources:
        for entry in source.helm_value_files:
            ref_name, rel_path = parse_ref_entry(entry, ref_sources)
            if ref_name is None:
                # A plain entry can sit outside the chart, such as ../../envs/prod/values.yaml.
                expanded = expand_build_env(entry, app, source)
                if source.path and expanded and "://" not in expanded:
                    if is_local_repo_url(source.repo_url, local_origin):
                        paths.add(value_file_path(source.path, expanded))
                continue
            ref_source = ref_sources.get(ref_name)
            if ref_source is None:
                continue  # Reported by broken-values-ref.
            if is_local_repo_url(ref_source.repo_url, local_origin):
                paths.add(posixpath.normpath(rel_path))
    return paths


def _glob_covers(pattern: str, path: str) -> bool:
    # match_glob has no [...] classes; fnmatch has them and is looser, which
    # can only miss an orphan, never report a covered file.
    if "[" in pattern:
        return fnmatch.fnmatchcase(path, pattern)
    return match_glob(pattern, path)


def _is_excluded(relative: Path, exclude_paths: list[str]) -> bool:
    # fnmatch lowercases both sides on Windows. The config is shared with
    # Linux CI runners, so matching must be case-sensitive everywhere.
    posix_path = relative.as_posix()
    return any(fnmatch.fnmatchcase(posix_path, pattern) for pattern in exclude_paths)


def _has_ignore_marker(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return IGNORE_MARKER in text
