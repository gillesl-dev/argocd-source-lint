from __future__ import annotations

from pathlib import Path

from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "malformed-ignore-diff-jq-expression"


class MalformedIgnoreDiffJqExpressionRule(Rule):
    """`ignoreDifferences[].jqPathExpressions` entries are compiled by
    ArgoCD's own `gojq` engine as `del(<expression>)`; every real
    example in ArgoCD's own docs, and every jq path expression in
    general, starts with `.` (`.spec.replicas`, `.metadata.labels["x"]`).
    An entry that doesn't is almost certainly a `jsonPointers`-style
    leading-slash path pasted into the wrong sibling field (`/spec/
    replicas`), which fails to parse as jq. Confirmed against ArgoCD's
    own source (`NewIgnoreNormalizer`, `util/argo/normalizers/
    diff_normalizer.go`): a parse failure aborts building the normalizer
    for the *whole* `ignoreDifferences` list, not just this one entry:
    a wider blast radius than `malformed-ignore-diff-pointer`'s, and
    matching real community reports of `jqPathExpressions` that "apply
    without errors but don't actually do anything" (see DESIGN.md)."""

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
                    if expression and not expression.startswith("."):
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
