from __future__ import annotations

import json

from argocd_source_lint import tool_version
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import DEFAULT_RULE_SEVERITIES, RULE_DESCRIPTIONS, rule_docs_url
from argocd_source_lint.reporters.fingerprint import stable_fingerprint

_INFORMATION_URI = "https://github.com/gillesl-dev/argocd-source-lint"

_LEVEL_BY_SEVERITY = {
    Severity.ERROR: "error",
    Severity.WARNING: "warning",
    Severity.INFO: "note",
    Severity.UNVERIFIABLE: "warning",
}


def render_findings(findings: list[Finding]) -> str:
    payload = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "argocd-source-lint",
                        "informationUri": _INFORMATION_URI,
                        "version": tool_version(),
                        "rules": [
                            _rule(rule_id, description)
                            for rule_id, description in RULE_DESCRIPTIONS.items()
                        ],
                    }
                },
                "results": [_result(finding) for finding in findings],
            }
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _rule(rule_id: str, description: str) -> dict:
    return {
        "id": rule_id,
        "shortDescription": {"text": description},
        "fullDescription": {"text": description},
        "helpUri": rule_docs_url(rule_id),
        "defaultConfiguration": {"level": _LEVEL_BY_SEVERITY[DEFAULT_RULE_SEVERITIES[rule_id]]},
    }


def _result(finding: Finding) -> dict:
    physical_location: dict = {"artifactLocation": {"uri": finding.file.as_posix()}}
    if finding.line is not None:
        physical_location["region"] = {"startLine": finding.line}

    return {
        "ruleId": finding.rule_id,
        "level": _LEVEL_BY_SEVERITY[finding.severity],
        "message": {"text": finding.message},
        "locations": [{"physicalLocation": physical_location}],
        # Without it, GitHub code scanning opens new alerts on every run
        # (SARIF support docs, "partialFingerprints object").
        "partialFingerprints": {"primaryLocationLineHash": stable_fingerprint(finding)},
    }
