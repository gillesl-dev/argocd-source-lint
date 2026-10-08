from __future__ import annotations

import importlib
import json
import pkgutil
from unittest.mock import patch
from xml.etree import ElementTree as ET

from typer.testing import CliRunner

from argocd_source_lint import applicationset
from argocd_source_lint import rules as rules_package
from argocd_source_lint.cli import RULES, app
from argocd_source_lint.policy import DEFAULT_RULE_SEVERITIES
from argocd_source_lint.rules.base import Rule

runner = CliRunner()


def test_every_rule_class_is_registered_in_cli_rules():
    """A `Rule` subclass that exists under `rules/` but is never added to
    `cli.RULES` would otherwise stay invisible to every test here: its
    own unit test instantiates it and calls `.check()` directly, so it
    can pass while the real CLI never runs it at all; confirmed for
    real, not hypothetical: a throwaway rule module left this way left
    the full suite green. `pkgutil.iter_modules` force-imports every
    module under `rules/` first, so a rule nobody imports yet is still
    visible to `Rule.__subclasses__()`."""
    for module_info in pkgutil.iter_modules(rules_package.__path__, rules_package.__name__ + "."):
        importlib.import_module(module_info.name)

    all_rule_classes = {
        cls
        for cls in Rule.__subclasses__()
        if cls.__module__.startswith("argocd_source_lint.rules.")
    }
    registered_classes = {type(rule) for rule in RULES}
    assert all_rule_classes == registered_classes


def test_every_rule_has_a_default_severity():
    """The same silent-omission risk as the test above, one dict over:
    `policy.DEFAULT_RULE_SEVERITIES` is maintained by hand too, and a
    rule missing from it would still run (its own `.get(RULE_ID, ...)`
    fallback covers that), but silently drop out of `Policy().rules`:
    e.g. a future feature that lists every configurable rule and its
    severity would miss it with no test ever noticing. `unresolvable-
    generator` is the one deliberate exception: its findings come from
    `applicationset.discover`, not a `Rule.check()` in `RULES`, but its
    severity is still policy-configurable."""
    registered_ids = {rule.rule_id for rule in RULES} | {applicationset.RULE_ID}
    assert registered_ids == set(DEFAULT_RULE_SEVERITIES)


def test_missing_git_exits_2_with_a_clear_error_instead_of_degrading(fixture_repo):
    """Without this check, a missing `git` silently turns into false
    `orphan-source` positives on every covered file instead of a clear
    error (see `git_context.is_git_available`)."""
    repo_root = fixture_repo("good_repo")

    with patch("argocd_source_lint.cli.is_git_available", return_value=False):
        result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 2
    assert "git" in result.stdout.lower()


def test_version_flag_prints_version_and_exits_0():
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip()


def test_exit_code_1_on_phantom_target_error(fixture_repo):
    repo_root = fixture_repo("bad_phantom_target")

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 1


def test_exit_code_0_on_good_repo(fixture_repo):
    repo_root = fixture_repo("good_repo")

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 0


def test_write_baseline_accepts_current_findings_and_exits_0(fixture_repo):
    repo_root = fixture_repo("bad_phantom_target")

    result = runner.invoke(app, [str(repo_root), "--write-baseline"])

    assert result.exit_code == 0
    assert (repo_root / ".argocd-lint-baseline.yaml").is_file()


def test_baselined_finding_no_longer_blocks_ci(fixture_repo):
    repo_root = fixture_repo("bad_phantom_target")
    runner.invoke(app, [str(repo_root), "--write-baseline"])

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 0
    assert "phantom-target" not in result.stdout


def test_new_finding_still_blocks_ci_after_baselining_a_different_one(fixture_repo):
    repo_root = fixture_repo("bad_phantom_target")
    runner.invoke(app, [str(repo_root), "--write-baseline"])
    # A new, never-baselined finding appears (a fresh orphan file).
    orphan = repo_root / "manifests" / "new-orphan.yaml"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: new-orphan\n")
    (repo_root / ".argocd-lint.yaml").write_text("scan_roots:\n  - manifests/\n")

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 1
    assert "phantom-target" not in result.stdout
    assert "orphan-source" in result.stdout


def test_stale_baseline_entry_is_reported_but_does_not_block_ci(fixture_repo, git_commit):
    repo_root = fixture_repo("bad_phantom_target")
    runner.invoke(app, [str(repo_root), "--write-baseline"])
    # The underlying issue is fixed: the missing path now exists
    # (committed: phantom-target resolves paths via `git ls-tree`, not
    # the raw working tree, see DESIGN.md "targetRevision drift").
    git_commit(
        repo_root,
        {"manifests/does-not-exist/deployment.yaml": "kind: Deployment\n"},
        message="fix: add the missing manifest",
    )

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 0
    assert "no longer match any finding" in result.stderr


def test_no_stale_baseline_message_when_baseline_still_fully_matches(fixture_repo):
    repo_root = fixture_repo("bad_phantom_target")
    runner.invoke(app, [str(repo_root), "--write-baseline"])

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 0
    assert "no longer match any finding" not in result.stderr


def test_unverifiable_blocks_ci_by_default(git_repo):
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

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 1


def test_unverifiable_blocks_ci_false_makes_it_non_blocking(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "unverifiable_blocks_ci: false\n",
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

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 0


def test_malformed_policy_exits_2_with_a_clear_error_instead_of_a_traceback(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "argocd_version: 'not-a-version'\n",
            "manifests/.gitkeep": "",
        }
    )

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert "argocd_version" in result.stdout
    assert "not a valid ArgoCD version" in result.stdout


def test_json_format_prints_to_stdout(fixture_repo):
    repo_root = fixture_repo("bad_phantom_target")

    result = runner.invoke(app, [str(repo_root), "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert len(payload["findings"]) == 1
    assert payload["findings"][0]["rule_id"] == "phantom-target"


def test_sarif_format_writes_to_output_file(fixture_repo, tmp_path):
    repo_root = fixture_repo("bad_phantom_target")
    output_file = tmp_path / "results.sarif"

    result = runner.invoke(app, [str(repo_root), "--format", "sarif", "--output", str(output_file)])

    assert result.exit_code == 1
    payload = json.loads(output_file.read_text(encoding="utf-8"))
    assert payload["version"] == "2.1.0"
    assert len(payload["runs"][0]["results"]) == 1


def test_gitlab_codequality_format_writes_to_output_file(fixture_repo, tmp_path):
    repo_root = fixture_repo("bad_phantom_target")
    output_file = tmp_path / "gl-code-quality-report.json"

    result = runner.invoke(
        app,
        [str(repo_root), "--format", "gitlab-codequality", "--output", str(output_file)],
    )

    assert result.exit_code == 1
    payload = json.loads(output_file.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert len(payload) == 1


def test_junit_format_writes_to_output_file(fixture_repo, tmp_path):
    repo_root = fixture_repo("bad_phantom_target")
    output_file = tmp_path / "junit.xml"

    result = runner.invoke(app, [str(repo_root), "--format", "junit", "--output", str(output_file)])

    assert result.exit_code == 1
    root = ET.fromstring(output_file.read_text(encoding="utf-8"))
    testsuite = root.find("testsuite")
    assert testsuite.get("tests") == "1"
    assert testsuite.get("failures") == "1"


def test_table_format_writes_plain_text_to_output_file(fixture_repo, tmp_path):
    repo_root = fixture_repo("bad_phantom_target")
    output_file = tmp_path / "report.txt"

    result = runner.invoke(app, [str(repo_root), "--output", str(output_file)])

    assert result.exit_code == 1
    text = output_file.read_text(encoding="utf-8")
    assert "phantom-target" in text
    assert "\x1b[" not in text  # no ANSI codes in a file
