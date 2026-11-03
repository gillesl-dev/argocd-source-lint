from __future__ import annotations

import re
from pathlib import Path

from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "malformed-ignore-diff-jq-expression"

# Argo CD compiles each entry as `del(<expression>)`, so any jq path
# expression is valid, `getpath(...)` and `select(...) | .a` included. Only
# a JSON pointer (`/spec/replicas`) or a path missing its leading dot
# (`spec.replicas`, read as a call to an undefined `spec` function) fails.
_MISTAKEN_PATH_RE = re.compile(r"\s*(/|[A-Za-z_]\w*[.\[])")


class MalformedIgnoreDiffJqExpressionRule(Rule):
    """Detect a `jqPathExpressions` entry written as a JSON pointer or a dotless path.

    Such an entry is usually a `jsonPointers` path pasted into the wrong
    field, and fails to compile as jq. A failure aborts the normalizer
    for the whole `ignoreDifferences` list, with no error shown
    (`NewIgnoreNormalizer` in util/argo/normalizers/diff_normalizer.go).
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
            for rule in app.ignore_differences:
                for expression in rule.jq_path_expressions:
                    if _MISTAKEN_PATH_RE.match(expression):
                        findings.append(_finding(app, rule.kind, expression, severity))

        return findings


def _finding(app: Application, kind: str, expression: str, severity: Severity) -> Finding:
    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        application=app.name,
        message=(
            f"`ignoreDifferences` jqPathExpressions entry `{expression}` (on `{kind}`) "
            "doesn't start with `.` -- every jq path expression does (e.g. "
            f"`.spec.replicas`, not `{expression}`), likely a jsonPointers-style path "
            "pasted into the wrong field. This fails to compile, which voids the "
            "*entire* ignoreDifferences list for this Application, not just this entry."
        ),
        file=app.source_file,
        line=app.ignore_differences_line,
    )
