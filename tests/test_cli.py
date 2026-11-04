from __future__ import annotations

import importlib
import json
import os
import pkgutil
import subprocess
import sys
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest
from typer.testing import CliRunner

from argocd_source_lint import applicationset
from argocd_source_lint import rules as rules_package
from argocd_source_lint.cli import RULES, app
from argocd_source_lint.policy import DEFAULT_RULE_SEVERITIES
from argocd_source_lint.rules.base import Rule

runner = CliRunner()


def test_every_rule_class_is_registered_in_cli_rules():
    """A rule missing from `cli.RULES` passes its own unit test but never runs in
    the CLI. Every module under `rules/` is imported first, so an unimported
    rule still shows up in `Rule.__subclasses__()`."""
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
    """`DEFAULT_RULE_SEVERITIES` is maintained by hand: a rule missing from it still
    runs but drops out of `Policy().rules`. `unresolvable-generator` has no
    `Rule` class, but its severity is still configurable."""
    registered_ids = {rule.rule_id for rule in RULES} | {applicationset.RULE_ID}
    assert registered_ids == set(DEFAULT_RULE_SEVERITIES)


def test_missing_git_exits_2_with_a_clear_error_instead_of_degrading(fixture_repo):
    """A missing `git` would otherwise turn into false `orphan-source` findings on
    every covered file (see `git_context.is_git_available`)."""
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


def test_new_finding_still_blocks_ci_after_baselining_a_different_one(fixture_repo, git_commit):
    repo_root = fixture_repo("bad_phantom_target")
    runner.invoke(app, [str(repo_root), "--write-baseline"])
    # A new, never-baselined finding appears (a fresh orphan file).
    git_commit(
        repo_root,
        {"manifests/new-orphan.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n"},
        message="add an orphan",
    )
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
    # the raw working tree, see DESIGN.md).
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


def test_repo_without_application_warns_instead_of_reporting_no_issues(git_repo):
    repo_root = git_repo({"manifests/deployment.yaml": "kind: Deployment\n"})

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 0
    assert "No ArgoCD Application found" in result.stdout
    assert "No issues detected" not in result.stdout


def test_fail_on_empty_exits_1_when_no_application_is_found(git_repo):
    repo_root = git_repo({"manifests/deployment.yaml": "kind: Deployment\n"})

    result = runner.invoke(app, [str(repo_root), "--fail-on-empty"])

    assert result.exit_code == 1


def test_fail_on_empty_does_not_change_the_exit_code_when_applications_exist(fixture_repo):
    repo_root = fixture_repo("good_repo")

    result = runner.invoke(app, [str(repo_root), "--fail-on-empty"])

    assert result.exit_code == 0
    assert "No ArgoCD Application found" not in result.stdout


def test_no_application_warning_goes_to_stderr_for_json_and_sarif(git_repo):
    repo_root = git_repo({"manifests/deployment.yaml": "kind: Deployment\n"})

    json_result = runner.invoke(app, [str(repo_root), "--format", "json"])
    sarif_result = runner.invoke(app, [str(repo_root), "--format", "sarif", "--fail-on-empty"])

    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout)["findings"] == []
    assert "No ArgoCD Application found" in json_result.stderr
    assert sarif_result.exit_code == 1
    assert json.loads(sarif_result.stdout)["runs"][0]["results"] == []
    assert "No ArgoCD Application found" in sarif_result.stderr


DEMO_APP = """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/demo-app
"""


def test_unset_scan_roots_says_orphan_source_was_skipped(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/demo-app.yaml": DEMO_APP,
            "manifests/demo-app/deployment.yaml": "kind: Deployment\n",
        }
    )

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 0
    assert "orphan-source skipped" in result.stdout
    assert "No issues detected" in result.stdout


def test_configured_scan_roots_has_no_skipped_notice(fixture_repo):
    repo_root = fixture_repo("good_repo")

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 0
    assert "skipped" not in result.stdout


def test_missing_scan_root_directory_is_reported_as_skipped(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n  - infra/\n",
            "bootstrap/argocd-apps/demo-app.yaml": DEMO_APP,
            "manifests/demo-app/deployment.yaml": "kind: Deployment\n",
        }
    )

    result = runner.invoke(app, [str(repo_root), "--format", "json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["findings"] == []
    assert "`infra/` is not a directory" in result.stderr
    assert "manifests/" not in result.stderr


EXTERNAL_CHART_APP = """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: external-chart
spec:
  source:
    repoURL: https://charts.other.invalid/repo.git
    targetRevision: 1.0.0
    path: charts/web
"""


def test_table_summarizes_info_findings_by_default(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "bootstrap/argocd-apps/external-chart.yaml": EXTERNAL_CHART_APP,
            "manifests/.gitkeep": "",
        }
    )

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 0
    assert "info finding(s) not listed" in result.stdout
    assert "1 Application source(s) in another repo, not inspected" in result.stdout
    assert "DESIGN.md" not in result.stdout  # the per-rule message itself


def test_show_info_lists_info_findings_in_the_table(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "bootstrap/argocd-apps/external-chart.yaml": EXTERNAL_CHART_APP,
            "manifests/.gitkeep": "",
        }
    )

    result = runner.invoke(app, [str(repo_root), "--show-info"])

    assert result.exit_code == 0
    assert "DESIGN.md" in result.stdout  # the per-rule message itself
    assert "not listed" not in result.stdout


def test_json_keeps_every_info_finding_without_show_info(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/external-chart.yaml": EXTERNAL_CHART_APP,
            "manifests/.gitkeep": "",
        }
    )

    result = runner.invoke(app, [str(repo_root), "--format", "json"])

    findings = json.loads(result.stdout)["findings"]
    external = [f for f in findings if "external repo" in f["message"]]
    assert len(external) > 1  # one per rule, as before
    assert all("external_source" not in f for f in findings)


def test_scan_root_name_with_control_characters_is_shown_not_emitted(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": 'scan_roots:\n  - "bad\\e[31m"\n',
            "bootstrap/argocd-apps/demo-app.yaml": DEMO_APP,
            "manifests/demo-app/deployment.yaml": "kind: Deployment\n",
        }
    )

    result = runner.invoke(app, [str(repo_root), "--format", "json"])

    assert "\x1b" not in result.stderr
    assert "bad\\x1b[31m" in result.stderr


@pytest.mark.parametrize("output_format", ["json", "sarif", "junit", "gitlab-codequality"])
def test_stdout_report_is_utf8_even_when_stdout_encoding_is_cp1252(fixture_repo, output_format):
    """A redirected stdout on Windows defaults to cp1252, forced here so Linux CI
    sees the same. The phantom-target message holds an em dash (U+2014)."""
    repo_root = fixture_repo("bad_phantom_target")

    result = subprocess.run(
        [sys.executable, "-m", "argocd_source_lint.cli", str(repo_root), "-f", output_format],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        check=False,
    )

    assert result.returncode == 1
    assert "—".encode() in result.stdout
    result.stdout.decode("utf-8")  # raises on a stray cp1252 byte


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


def _phantom_app(annotations: str = "") -> str:
    block = f"  annotations:\n{annotations}" if annotations else ""
    return (
        "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: web\n"
        + block
        + "spec:\n  source:\n    repoURL: https://example.invalid/repo.git\n"
        "    targetRevision: HEAD\n    path: manifests/nowhere\n"
    )


def test_annotation_with_a_reason_suppresses_that_rule_for_that_application(git_repo):
    repo_root = git_repo(
        {
            "apps/web.yaml": _phantom_app(
                '    argocd-source-lint/ignore-phantom-target: "created by a Job at sync"\n'
            ),
            "manifests/.gitkeep": "",
        }
    )

    result = runner.invoke(app, [str(repo_root), "--format", "json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["findings"] == []
    assert "1 finding(s) suppressed by argocd-source-lint/ignore-<rule>" in result.stderr


def test_annotation_without_a_reason_suppresses_nothing_and_says_so(git_repo):
    repo_root = git_repo(
        {
            "apps/web.yaml": _phantom_app('    argocd-source-lint/ignore-phantom-target: ""\n'),
            "manifests/.gitkeep": "",
        }
    )

    result = runner.invoke(app, [str(repo_root), "--format", "json"])

    assert result.exit_code == 1
    assert [f["rule_id"] for f in json.loads(result.stdout)["findings"]] == ["phantom-target"]
    assert "has no reason" in " ".join(result.stderr.split())


def test_annotation_naming_an_unknown_rule_is_reported(git_repo):
    repo_root = git_repo(
        {
            "apps/web.yaml": _phantom_app('    argocd-source-lint/ignore-phantom-targt: "typo"\n'),
            "manifests/.gitkeep": "",
        }
    )

    result = runner.invoke(app, [str(repo_root), "--format", "json"])

    assert result.exit_code == 1
    assert "names no known rule" in " ".join(result.stderr.split())


def test_annotation_on_an_applicationset_template_reaches_generated_applications(git_repo):
    repo_root = git_repo(
        {
            "appsets/web.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: web
spec:
  generators:
    - list:
        elements:
          - env: dev
  template:
    metadata:
      name: 'web-{{env}}'
      annotations:
        argocd-source-lint/ignore-phantom-target: "path created at sync"
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/nowhere
""",
            "manifests/.gitkeep": "",
        }
    )

    result = runner.invoke(app, [str(repo_root), "--format", "json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["findings"] == []


def test_suppressed_findings_are_not_written_to_the_baseline(git_repo):
    repo_root = git_repo(
        {
            "apps/web.yaml": _phantom_app(
                '    argocd-source-lint/ignore-phantom-target: "created by a Job at sync"\n'
            ),
            "manifests/.gitkeep": "",
        }
    )

    runner.invoke(app, [str(repo_root), "--write-baseline"])

    assert "phantom-target" not in (repo_root / ".argocd-lint-baseline.yaml").read_text()


def test_list_rules_prints_every_rule_with_its_default_severity():
    result = runner.invoke(app, ["--list-rules"])

    assert result.exit_code == 0
    for rule_id, severity in DEFAULT_RULE_SEVERITIES.items():
        assert any(
            line.split()[:2] == [rule_id, severity.value] for line in result.stdout.splitlines()
        ), rule_id


def test_only_runs_just_the_named_rules(fixture_repo):
    repo_root = fixture_repo("bad_phantom_target")

    result = runner.invoke(app, [str(repo_root), "-f", "json", "--only", "orphan-source"])

    assert result.exit_code == 0
    assert all(f["rule_id"] == "orphan-source" for f in json.loads(result.stdout)["findings"])


def test_skip_leaves_out_the_named_rule(fixture_repo):
    repo_root = fixture_repo("bad_phantom_target")

    result = runner.invoke(app, [str(repo_root), "-f", "json", "--skip", "phantom-target"])

    assert result.exit_code == 0
    assert "phantom-target" not in result.stdout


def test_unknown_rule_in_only_or_skip_exits_2(fixture_repo):
    repo_root = fixture_repo("good_repo")

    result = runner.invoke(app, [str(repo_root), "--skip", "phantom-targt"])

    assert result.exit_code == 2
    assert "phantom-targt" in result.stdout


def test_write_baseline_refuses_a_subset_of_rules(fixture_repo):
    repo_root = fixture_repo("bad_phantom_target")

    result = runner.invoke(app, [str(repo_root), "--write-baseline", "--only", "orphan-source"])

    assert result.exit_code == 2
    assert not (repo_root / ".argocd-lint-baseline.yaml").exists()


def test_skipping_orphan_source_drops_the_scan_roots_notice(git_repo):
    repo_root = git_repo({"apps/web.yaml": _phantom_app(), "manifests/.gitkeep": ""})

    result = runner.invoke(app, [str(repo_root), "--skip", "orphan-source"])

    assert "scan_roots" not in result.stdout


def test_application_with_a_wrong_value_type_is_reported_not_a_crash(git_repo):
    repo_root = git_repo(
        {
            "apps/web.yaml": _phantom_app().replace("targetRevision: HEAD", "targetRevision: 2.0"),
            "manifests/.gitkeep": "",
        }
    )

    result = runner.invoke(app, [str(repo_root), "--format", "json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["findings"] == []
    assert "is not a valid Application" in " ".join(result.stderr.split())


def _pull_request_checkout_adding_an_app(git_repo, git_commit):
    """`main` has no Application yet. The pull request adds one with its
    manifests, and the checkout is detached with `main` only on origin."""
    repo_root = git_repo({"README.md": "x\n"})
    for args in (["branch", "-M", "main"], ["update-ref", "refs/remotes/origin/main", "HEAD"]):
        subprocess.run(["git", *args], cwd=repo_root, check=True, capture_output=True)
    git_commit(
        repo_root,
        {
            "apps/web.yaml": _phantom_app()
            .replace("targetRevision: HEAD", "targetRevision: main")
            .replace("path: manifests/nowhere", "path: manifests/web"),
            "manifests/web/cm.yaml": "kind: ConfigMap\nmetadata:\n  name: web\n",
        },
        message="add web",
    )
    for args in (["checkout", "-q", "--detach"], ["branch", "-q", "-D", "main"]):
        subprocess.run(["git", *args], cwd=repo_root, check=True, capture_output=True)
    return repo_root


def test_pull_request_is_checked_against_its_own_content_with_target_branch(git_repo, git_commit):
    repo_root = _pull_request_checkout_adding_an_app(git_repo, git_commit)

    without = runner.invoke(app, [str(repo_root), "-f", "json"])
    with_branch = runner.invoke(app, [str(repo_root), "-f", "json", "--target-branch", "main"])

    assert "phantom-target" in without.stdout
    assert with_branch.exit_code == 0
    assert json.loads(with_branch.stdout)["findings"] == []


def test_target_branch_is_read_from_github_base_ref(git_repo, git_commit, monkeypatch):
    repo_root = _pull_request_checkout_adding_an_app(git_repo, git_commit)
    monkeypatch.setenv("GITHUB_BASE_REF", "main")

    result = runner.invoke(app, [str(repo_root), "-f", "json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["findings"] == []


def test_repo_urls_identify_this_repository_under_another_url(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "repo_urls:\n  - https://example.invalid/repo.git\n",
            "apps/web.yaml": _phantom_app(),
            "manifests/.gitkeep": "",
        },
        origin_url="git@my-ssh-alias:team/repo.git",
    )

    result = runner.invoke(app, [str(repo_root), "-f", "json"])

    assert [f["rule_id"] for f in json.loads(result.stdout)["findings"]] == ["phantom-target"]


def test_no_source_matching_the_origin_is_reported(git_repo):
    repo_root = git_repo(
        {"apps/web.yaml": _phantom_app(), "manifests/.gitkeep": ""},
        origin_url="git@my-ssh-alias:team/repo.git",
    )

    result = runner.invoke(app, [str(repo_root), "-f", "json"])

    stderr = " ".join(result.stderr.split())
    assert "No Application source points to this repository" in stderr
    assert "repo_urls" in stderr


def test_staged_application_and_directory_are_checked_as_on_disk(git_repo):
    """What a pre-commit hook sees: the new files are staged, not committed."""
    repo_root = git_repo({"README.md": "x\n"})
    (repo_root / "apps").mkdir()
    (repo_root / "apps/web.yaml").write_text(
        _phantom_app().replace("manifests/nowhere", "manifests/web"), encoding="utf-8"
    )
    (repo_root / "manifests/web").mkdir(parents=True)
    (repo_root / "manifests/web/cm.yaml").write_text("kind: ConfigMap\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, capture_output=True)

    result = runner.invoke(app, [str(repo_root), "-f", "json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["findings"] == []


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        (".argocd-lint.yaml", "scan_roots: [unclosed\n"),
        (".argocd-lint.yaml", "- a list\n"),
        (".argocd-lint-baseline.yaml", "rule_id: not-a-list\n"),
    ],
)
def test_unreadable_config_or_baseline_exits_2_with_a_message(git_repo, filename, content):
    repo_root = git_repo({filename: content, "manifests/.gitkeep": ""})

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 2
    assert "Can't read" in result.stdout
