from __future__ import annotations

import json

from argocd_source_lint.models import Finding


def render_findings(findings: list[Finding]) -> str:
    payload = {"findings": [_finding_dict(finding) for finding in findings]}
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _finding_dict(finding: Finding) -> dict:
    # `model_dump` would serialize `file` via `str(Path)`: backslashes on
    # Windows, invalid for cross-platform JSON consumers.
    data = finding.model_dump(mode="json")
    data["file"] = finding.file.as_posix()
    return data
