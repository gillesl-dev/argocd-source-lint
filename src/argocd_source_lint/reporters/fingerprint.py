from __future__ import annotations

import hashlib

from argocd_source_lint.models import Finding


def stable_fingerprint(finding: Finding) -> str:
    """Hash the rule, file, Application and message of a finding.

    The hash ignores line numbers and list position, so GitLab Code Quality
    and SARIF consumers can match the same finding across runs.
    """
    identity = (
        f"{finding.rule_id}|{finding.file.as_posix()}|{finding.application}|{finding.message}"
    )
    return hashlib.md5(identity.encode("utf-8"), usedforsecurity=False).hexdigest()
