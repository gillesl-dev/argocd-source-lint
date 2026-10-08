from __future__ import annotations

import pytest
from pydantic import ValidationError

from argocd_source_lint.models import Severity
from argocd_source_lint.policy import load_policy


def test_default_policy_when_no_file_present(git_repo):
    repo_root = git_repo({"manifests/.gitkeep": ""})

    policy = load_policy(repo_root)

    assert policy.scan_roots == []
    assert policy.unverifiable_blocks_ci is True
    assert policy.rules["orphan-source"] == Severity.ERROR
    assert policy.rules["missing-ignore-diff"] == Severity.WARNING
    assert policy.argocd_version is None


def test_partial_override_merges_with_defaults(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": """\
scan_roots:
  - manifests/
rules:
  missing-ignore-diff: error
""",
        }
    )

    policy = load_policy(repo_root)

    assert policy.scan_roots == ["manifests/"]
    # Overridden rule takes the new severity...
    assert policy.rules["missing-ignore-diff"] == Severity.ERROR
    # ...but rules not mentioned in the file keep their default.
    assert policy.rules["orphan-source"] == Severity.ERROR
    assert policy.rules["phantom-target"] == Severity.ERROR
    # Fields absent from the file keep their default too.
    assert policy.unverifiable_blocks_ci is True


def test_unverifiable_blocks_ci_can_be_disabled(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "unverifiable_blocks_ci: false\n",
        }
    )

    policy = load_policy(repo_root)

    assert policy.unverifiable_blocks_ci is False


def test_argocd_version_is_loaded(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "argocd_version: '3.2.0'\n",
        }
    )

    policy = load_policy(repo_root)

    assert policy.argocd_version == "3.2.0"


def test_argocd_version_rejects_malformed_value(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "argocd_version: 'not-a-version'\n",
        }
    )

    with pytest.raises(ValidationError):
        load_policy(repo_root)
