from __future__ import annotations

import subprocess
from pathlib import Path

from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.phantom_target import PhantomTargetRule


def _run_phantom_target(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    return PhantomTargetRule().check(applications, repo_root, policy, local_origin)


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_good_repo_has_no_phantom_target_findings(fixture_repo):
    repo_root = fixture_repo("good_repo")

    findings = _run_phantom_target(repo_root)

    assert findings == []


def test_bad_phantom_target_flags_missing_path(fixture_repo):
    repo_root = fixture_repo("bad_phantom_target")

    findings = _run_phantom_target(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "phantom-target"
    assert finding.severity == Severity.ERROR
    assert finding.application == "demo-app"
    assert "manifests/does-not-exist" in finding.message
    assert finding.line == 8  # the source mapping's `repoURL:` line


def test_multi_repo_application_is_flagged_info_and_not_checked(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/external-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: external-app
spec:
  source:
    repoURL: https://example.invalid/some-other-repo.git
    targetRevision: HEAD
    path: this/path/is/never/checked
""",
        }
    )

    findings = _run_phantom_target(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO
    assert findings[0].application == "external-app"


def test_mixed_local_and_external_sources_still_checks_the_local_source(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/mixed-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: mixed-app
spec:
  sources:
    - repoURL: https://example.invalid/repo.git
      targetRevision: HEAD
      path: manifests/does-not-exist
    - repoURL: https://example.invalid/some-other-repo.git
      targetRevision: HEAD
      path: charts/bar
""",
        }
    )

    findings = _run_phantom_target(repo_root)

    severities = {f.severity for f in findings}
    assert Severity.INFO in severities
    assert Severity.ERROR in severities
    error_finding = next(f for f in findings if f.severity == Severity.ERROR)
    assert "manifests/does-not-exist" in error_finding.message


def test_unresolvable_revision_is_unverifiable_not_skipped(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/demo-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: never-fetched-branch
    path: manifests/demo-app
""",
            "manifests/demo-app/deployment.yaml": "kind: Deployment\n",
        }
    )

    findings = _run_phantom_target(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == Severity.UNVERIFIABLE
    assert "never-fetched-branch" in finding.message
    assert "fetch-depth" in finding.message


def test_path_present_on_current_branch_but_absent_on_declared_revision(git_repo):
    """Paths are checked with `git ls-tree <targetRevision>`, not the working
    tree: here the path exists on the current branch only."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/demo-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: old-release
    path: manifests/demo-app
""",
        }
    )
    # `old-release` is created BEFORE `manifests/demo-app` exists.
    _run_git("branch", "old-release", cwd=repo_root)

    (repo_root / "manifests/demo-app").mkdir(parents=True)
    (repo_root / "manifests/demo-app/deployment.yaml").write_text("kind: Deployment\n")
    _run_git("add", "-A", cwd=repo_root)
    _run_git("commit", "-q", "-m", "add demo-app manifests", cwd=repo_root)

    # On the current branch (main/master), the path does exist.
    assert (repo_root / "manifests/demo-app/deployment.yaml").exists()

    findings = _run_phantom_target(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == Severity.ERROR
    assert "old-release" in finding.message
    assert "manifests/demo-app" in finding.message


def test_rule_severity_is_configurable_via_policy(fixture_repo):
    repo_root = fixture_repo("bad_phantom_target")
    (repo_root / ".argocd-lint.yaml").write_text(
        "rules:\n  phantom-target: warning\n",
        encoding="utf-8",
    )

    findings = _run_phantom_target(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.WARNING
