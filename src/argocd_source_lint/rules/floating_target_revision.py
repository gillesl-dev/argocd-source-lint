from __future__ import annotations

import re
from pathlib import Path

from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "floating-target-revision"

# `IsConstraint` in Argo CD's util/versions/tags.go: not an exact version,
# but a Masterminds semver constraint (github.com/Masterminds/semver/v3).
# Its regex accepts `x64`, but `parseConstraint` then fails to build a
# version from it, so a part is digits or a single wildcard.
_PART = r"(?:[0-9]+|[xX*])"
_VERSION = (
    rf"v?{_PART}(?:\.{_PART}){{0,2}}"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
# Matched after `_normalize`, so a separator is always one character.
_TERM = rf"(?:!=|>=|=>|<=|=<|~>|=|>|<|~|\^)? ?{_VERSION}"
_RANGE = rf"{_VERSION} - {_VERSION}"
# Terms need a separator, as in `validConstraintRegex`.
_AND = re.compile(rf"(?:{_RANGE}|{_TERM})(?:[ ,](?:{_RANGE}|{_TERM}))*")
_SPACES = re.compile(r"\s+")
_COMMA = re.compile(r" ?, ?")
_EXACT = re.compile(
    r"v?[0-9]+(?:\.[0-9]+){0,2}"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
# A Git branch can be named `1.x`: with no matching tag, Argo CD falls back
# to the branch, so this form is only reported for charts.
_X_ONLY = re.compile(r"v?[0-9xX]+(?:\.[0-9xX]+){0,2}")


def is_constraint(revision: str) -> bool:
    revision = revision.strip()
    # A longer value is not treated as a range; it bounds the match time.
    if not revision or len(revision) > 200:
        return False
    # `=1.2.3` is a constraint that only one version meets.
    if _EXACT.fullmatch(revision.removeprefix("=").strip()):
        return False
    return all(_AND.fullmatch(_normalize(part)) for part in revision.split("||"))


def _normalize(part: str) -> str:
    return _COMMA.sub(",", _SPACES.sub(" ", part.strip()))


class FloatingTargetRevisionRule(Rule):
    """Detect a `targetRevision` written as a semver constraint, such as `1.2.*`.

    Argo CD resolves it to the highest matching chart version or Git tag at
    each sync, so deployed content can change with no commit in this repo.
    Applies to external sources too, since the string is in the manifest.
    """

    rule_id = RULE_ID

    def check(
        self,
        applications: list[Application],
        repo_root: Path,
        policy: Policy,
        local_origin: str | None,
    ) -> list[Finding]:
        severity = policy.rules.get(RULE_ID, Severity.WARNING)
        findings: list[Finding] = []

        for app in applications:
            for source in app.sources:
                revision = source.target_revision
                if not is_constraint(revision):
                    continue
                if not source.chart and _X_ONLY.fullmatch(revision.strip()):
                    continue

                kind = "chart version" if source.chart else "Git tag"
                findings.append(
                    Finding(
                        rule_id=RULE_ID,
                        severity=severity,
                        application=app.name,
                        message=(
                            f"`targetRevision: {revision}` is a version range, not an exact "
                            f"pin. ArgoCD resolves it against the highest matching {kind} at "
                            "every sync, so the deployed content can change with no "
                            "corresponding commit in this repository."
                        ),
                        file=app.source_file,
                        line=source.line,
                    )
                )

        return findings
