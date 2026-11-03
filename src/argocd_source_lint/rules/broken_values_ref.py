from __future__ import annotations

import posixpath
import re
from pathlib import Path

from argocd_source_lint.git_context import (
    is_local_repo_url,
    is_revision_resolvable,
    tree_paths_at_revision,
)
from argocd_source_lint.globs import match_glob
from argocd_source_lint.models import Application, Finding, Severity, Source
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "broken-values-ref"


class BrokenValuesRefRule(Rule):
    """Detect `helm.valueFiles` entries that point to a missing file.

    Covers both `$ref/...` entries, resolved against the source declaring
    that `ref`, and plain entries, resolved against the source's own `path`.
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

                    ref_name, rel_path = parse_ref_entry(entry, ref_sources)
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

                    if source.helm_ignore_missing_value_files:
                        continue  # Argo CD skips a missing $ref file too.
                    expanded = expand_build_env(rel_path, app, source)
                    if expanded is None:
                        continue
                    rel_path = expanded
                    if is_glob(rel_path) and (
                        "[" in rel_path
                        or glob_matches(rel_path, tree_paths_at_revision(repo_root, revision))
                    ):
                        continue
                    if posixpath.normpath(rel_path) not in tree_paths_at_revision(
                        repo_root, revision
                    ):
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
    """Check a plain (non-`$ref`) `helm.valueFiles` entry against the source's `path`.

    A missing values file fails Helm template generation, yet the
    Application can still report Healthy (argoproj/argo-cd#4558).
    """
    if source.helm_ignore_missing_value_files:
        return []  # Argo CD tolerates a missing file here.
    if not source.path or not is_local_repo_url(source.repo_url, local_origin):
        return []
    if "://" in entry:
        return []  # A remote values file can't be checked from the checkout.

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

    expanded = expand_build_env(entry, app, source)
    if expanded is None:
        return []
    full_path = value_file_path(source.path, expanded)
    if is_glob(expanded):
        if "[" in expanded or glob_matches(full_path, tree_paths_at_revision(repo_root, revision)):
            return []  # Character classes are not evaluated, so never reported.
        if source.helm_ignore_missing_value_files:
            return []
        return [
            _finding(
                app,
                severity,
                f"`{entry}` (Helm valueFiles): the pattern `{full_path}` matches no file on "
                f"revision `{revision}`, so ArgoCD reports a ComparisonError.",
                line=entry_line,
            )
        ]
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


def is_glob(entry: str) -> bool:
    return any(char in entry for char in "*?[")


def glob_matches(pattern: str, paths: frozenset[str] | set[str]) -> list[str]:
    """Paths matching a `valueFiles` glob. Argo CD lets `**` match zero
    segments too, so `envs/**/*.yaml` also matches `envs/a.yaml`."""
    patterns = {pattern, pattern.replace("/**/", "/")}
    return sorted(path for path in paths if any(match_glob(p, path) for p in patterns))


def value_file_path(source_path: str, entry: str) -> str:
    """Repo-relative path of a plain `valueFiles` entry, as Argo CD resolves it:
    relative to the source path, or to the repo root when it starts with `/`."""
    if entry.startswith("/"):
        return posixpath.normpath(entry.lstrip("/"))
    return posixpath.normpath(posixpath.join(source_path, entry))


def parse_ref_entry(entry: str, ref_sources: dict[str, Source]) -> tuple[str | None, str]:
    """Split `$ref_name/rel/path.yaml` into `(ref_name, "rel/path.yaml")`.

    Returns `(None, "")` for a plain entry, including one that starts with a
    build variable such as `$ARGOCD_APP_NAME/` and no source has that `ref`.
    Also used by `orphan-source`.
    """
    if not entry.startswith("$"):
        return None, ""
    ref_name, separator, rel_path = entry[1:].partition("/")
    if not separator:
        return None, ""
    if ref_name not in ref_sources and _BUILD_ENV_RE.fullmatch(f"${ref_name}"):
        name = ref_name.strip("{}")
        if name in _BUILD_ENV_NAMES or name in _REVISION_ENV_NAMES:
            return None, ""
    return ref_name, rel_path


# The variables Argo CD substitutes in `valueFiles` (`newEnv` and
# `Env.Envsubst` in reposerver/repository/repository.go). Any other `$NAME`
# becomes an empty string.
_BUILD_ENV_RE = re.compile(r"\$(?:(\$)|\{(\w+)\}|(\w+))")
_BUILD_ENV_NAMES = (
    "ARGOCD_APP_NAME",
    "ARGOCD_APP_NAMESPACE",
    "ARGOCD_APP_PROJECT_NAME",
    "ARGOCD_APP_SOURCE_PATH",
    "ARGOCD_APP_SOURCE_REPO_URL",
    "ARGOCD_APP_SOURCE_TARGET_REVISION",
)
# The resolved commit, which this tool doesn't compute.
_REVISION_ENV_NAMES = (
    "ARGOCD_APP_REVISION",
    "ARGOCD_APP_REVISION_SHORT",
    "ARGOCD_APP_REVISION_SHORT_8",
)


def expand_build_env(entry: str, app: Application, source: Source) -> str | None:
    """`entry` with Argo CD's build variables substituted, or None when it uses
    one this tool can't know."""
    values = dict(
        zip(
            _BUILD_ENV_NAMES,
            (
                app.name,
                app.destination_namespace or "",
                app.project,
                source.path or "",
                source.repo_url,
                source.target_revision,
            ),
            strict=True,
        )
    )
    unknown = False

    def substitute(match: re.Match[str]) -> str:
        nonlocal unknown
        if match.group(1):
            return "$"
        name = match.group(2) or match.group(3)
        if name in _REVISION_ENV_NAMES:
            unknown = True
        return values.get(name, "")

    expanded = _BUILD_ENV_RE.sub(substitute, entry)
    return None if unknown else expanded


def ref_sources_by_name(app: Application) -> dict[str, Source]:
    """Index the sources of `app` that declare `ref:` by that name."""
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
