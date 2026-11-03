"""Per-Application suppression through `argocd-source-lint/ignore-<rule-id>`
annotations, whose value must give the reason."""

from __future__ import annotations

from rich.markup import escape

from argocd_source_lint.models import Application, Finding
from argocd_source_lint.reporters.safe_text import visible_controls

ANNOTATION_PREFIX = "argocd-source-lint/ignore-"


def split_by_annotations(
    findings: list[Finding], applications: list[Application]
) -> tuple[list[Finding], list[Finding]]:
    """(kept, suppressed). A finding is suppressed when an Application with
    its name ignores its rule with a non-empty reason."""
    ignored: dict[str, set[str]] = {}
    for app in applications:
        for rule_id, reason in app.ignored_rules.items():
            if reason.strip():
                ignored.setdefault(app.name, set()).add(rule_id)

    kept: list[Finding] = []
    suppressed: list[Finding] = []
    for finding in findings:
        if finding.rule_id in ignored.get(finding.application, set()):
            suppressed.append(finding)
        else:
            kept.append(finding)
    return kept, suppressed


def annotation_problems(applications: list[Application], known_rules: set[str]) -> list[str]:
    """Notices for annotations that suppress nothing, already markup-escaped."""
    problems = []
    for app in applications:
        name = escape(visible_controls(app.name))
        for rule_id, reason in sorted(app.ignored_rules.items()):
            key = escape(visible_controls(ANNOTATION_PREFIX + rule_id))
            if rule_id not in known_rules:
                problems.append(f"Application `{name}`: `{key}` names no known rule.")
            elif not reason.strip():
                problems.append(
                    f"Application `{name}`: `{key}` has no reason, so it suppresses nothing."
                )
    return problems
