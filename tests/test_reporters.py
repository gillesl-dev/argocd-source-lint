from __future__ import annotations

import io
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from rich.console import Console

from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import DEFAULT_RULE_SEVERITIES
from argocd_source_lint.reporters import gitlab_codequality, json_report, junit, sarif
from argocd_source_lint.reporters.sarif import _RULES_METADATA
from argocd_source_lint.reporters.table import render_findings as render_table

_FINDING_NO_LINE = Finding(
    rule_id="orphan-source",
    severity=Severity.ERROR,
    application="",
    message="File not covered: manifests/orphaned/configmap.yaml",
    file=Path("manifests/orphaned/configmap.yaml"),
)

_FINDING_WITH_LINE = Finding(
    rule_id="phantom-target",
    severity=Severity.WARNING,
    application="demo-app",
    message="path not found",
    file=Path("bootstrap/argocd-apps/demo-app.yaml"),
    line=12,
)


def test_json_report_uses_posix_paths_regardless_of_platform():
    windows_style = Finding(
        rule_id="orphan-source",
        severity=Severity.ERROR,
        application="",
        message="x",
        file=Path("manifests") / "orphaned" / "configmap.yaml",
    )

    payload = json.loads(json_report.render_findings([windows_style]))

    assert payload["findings"][0]["file"] == "manifests/orphaned/configmap.yaml"
    assert "\\" not in payload["findings"][0]["file"]


def test_json_report_round_trips_all_fields():
    payload = json.loads(json_report.render_findings([_FINDING_WITH_LINE]))

    finding = payload["findings"][0]
    assert finding["rule_id"] == "phantom-target"
    assert finding["severity"] == "warning"
    assert finding["application"] == "demo-app"
    assert finding["line"] == 12


def test_sarif_structure_and_severity_mapping():
    payload = json.loads(sarif.render_findings([_FINDING_NO_LINE, _FINDING_WITH_LINE]))

    assert payload["$schema"] == "https://json.schemastore.org/sarif-2.1.0.json"
    assert payload["version"] == "2.1.0"
    run = payload["runs"][0]
    # Every known rule, not just the two with results here: covered
    # more precisely (against DEFAULT_RULE_SEVERITIES) by
    # test_sarif_rules_metadata_covers_every_configurable_rule below;
    # this just confirms render_findings doesn't filter driver.rules
    # down to the rules that actually produced a result.
    rule_ids = {rule["id"] for rule in run["tool"]["driver"]["rules"]}
    assert rule_ids == set(DEFAULT_RULE_SEVERITIES)

    results = run["results"]
    assert len(results) == 2
    error_result = next(r for r in results if r["ruleId"] == "orphan-source")
    assert error_result["level"] == "error"
    warning_result = next(r for r in results if r["ruleId"] == "phantom-target")
    assert warning_result["level"] == "warning"


def test_sarif_rules_have_recommended_metadata():
    payload = json.loads(sarif.render_findings([]))

    rules = {rule["id"]: rule for rule in payload["runs"][0]["tool"]["driver"]["rules"]}
    orphan_source = rules["orphan-source"]
    assert orphan_source["fullDescription"]["text"]
    assert orphan_source["helpUri"].startswith("https://github.com/gillesl-dev/argocd-source-lint")
    assert orphan_source["defaultConfiguration"]["level"] == "error"

    # unresolvable-generator defaults to info -> SARIF "note".
    assert rules["unresolvable-generator"]["defaultConfiguration"]["level"] == "note"


def test_sarif_rules_metadata_covers_every_configurable_rule():
    """A third manually maintained rule-ID list, same silent-omission
    risk as `cli.RULES`/`policy.DEFAULT_RULE_SEVERITIES`: a rule missing
    here doesn't crash (the SARIF `results` still reference it by id),
    it just silently loses its `fullDescription`/`helpUri`/
    `defaultConfiguration` in the `driver.rules` array."""
    assert set(_RULES_METADATA) == set(DEFAULT_RULE_SEVERITIES)


def test_sarif_results_carry_a_stable_fingerprint():
    payload = json.loads(sarif.render_findings([_FINDING_NO_LINE]))

    fingerprints = payload["runs"][0]["results"][0]["partialFingerprints"]
    assert fingerprints["primaryLocationLineHash"]

    replay = json.loads(sarif.render_findings([_FINDING_NO_LINE]))
    replay_fingerprints = replay["runs"][0]["results"][0]["partialFingerprints"]
    assert replay_fingerprints["primaryLocationLineHash"] == fingerprints["primaryLocationLineHash"]


def test_sarif_omits_region_when_line_is_none():
    payload = json.loads(sarif.render_findings([_FINDING_NO_LINE]))

    location = payload["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    assert "region" not in location
    assert location["artifactLocation"]["uri"] == "manifests/orphaned/configmap.yaml"


def test_sarif_includes_region_when_line_is_set():
    payload = json.loads(sarif.render_findings([_FINDING_WITH_LINE]))

    location = payload["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    assert location["region"]["startLine"] == 12


def test_gitlab_codequality_structure_and_severity_mapping():
    payload = json.loads(gitlab_codequality.render_findings([_FINDING_NO_LINE, _FINDING_WITH_LINE]))

    assert isinstance(payload, list)
    assert len(payload) == 2

    error_issue = next(i for i in payload if i["check_name"] == "orphan-source")
    assert error_issue["severity"] == "major"
    assert error_issue["location"]["path"] == "manifests/orphaned/configmap.yaml"
    assert error_issue["location"]["lines"]["begin"] == 1  # default when line=None

    warning_issue = next(i for i in payload if i["check_name"] == "phantom-target")
    assert warning_issue["severity"] == "minor"
    assert warning_issue["location"]["lines"]["begin"] == 12


def test_table_shows_placeholder_when_application_is_empty():
    console = Console(file=io.StringIO(), no_color=True, width=200)
    render_table(console, [_FINDING_NO_LINE, _FINDING_WITH_LINE])

    output = console.file.getvalue()
    assert "demo-app" in output
    lines = [line for line in output.splitlines() if "orphan-source" in line]
    assert len(lines) == 1
    assert " - " in lines[0]  # placeholder in the empty Application column


def test_table_prints_no_issues_message_when_findings_empty():
    console = Console(file=io.StringIO(), no_color=True, width=200)
    render_table(console, [])

    assert "No issues detected." in console.file.getvalue()


def test_table_escapes_rich_markup_in_repo_controlled_fields():
    """`Table.add_row` parses every string argument as rich markup by
    default; confirmed for real: an Application name containing
    `[link=...]` rendered as an actual clickable hyperlink, and a
    `[bold red on white]`-style tag actually re-styled the output,
    before this was fixed. `application`/`message`/`file` (via
    `metadata.name`, a rule's message text, a filename) are all
    repo-controlled: a crafted repo must never be able to spoof or
    restyle this tool's own terminal output."""
    finding = Finding(
        rule_id="orphan-source",
        severity=Severity.ERROR,
        application="[bold red on white]FAKE[/bold red on white] [link=https://evil.example]x[/link]",
        message="normal message [green]looks legit[/green]",
        file=Path("manifests/evil.yaml"),
    )
    console = Console(file=io.StringIO(), no_color=True, width=200)
    render_table(console, [finding])

    output = console.file.getvalue()
    assert "[bold red on white]FAKE[/bold red on white]" in output
    assert "[link=https://evil.example]x[/link]" in output
    assert "[green]looks legit[/green]" in output


def test_junit_marks_error_as_failure_and_warning_as_skipped():
    root = ET.fromstring(junit.render_findings([_FINDING_NO_LINE, _FINDING_WITH_LINE]))

    testsuite = root.find("testsuite")
    assert testsuite.get("tests") == "2"
    assert testsuite.get("failures") == "1"
    assert testsuite.get("skipped") == "1"

    testcases = testsuite.findall("testcase")
    error_case = next(tc for tc in testcases if tc.get("classname") == "orphan-source")
    assert error_case.find("failure") is not None
    assert error_case.find("skipped") is None

    warning_case = next(tc for tc in testcases if tc.get("classname") == "phantom-target")
    assert warning_case.find("skipped") is not None
    assert warning_case.find("failure") is None
    assert warning_case.get("line") == "12"


def test_junit_marks_unverifiable_as_failure():
    unverifiable = Finding(
        rule_id="phantom-target",
        severity=Severity.UNVERIFIABLE,
        application="demo-app",
        message="revision missing from the local checkout",
        file=Path("bootstrap/argocd-apps/demo-app.yaml"),
    )

    root = ET.fromstring(junit.render_findings([unverifiable]))

    testcase = root.find("testsuite").find("testcase")
    assert testcase.find("failure") is not None
    assert testcase.find("failure").get("type") == "unverifiable"


def test_junit_is_valid_empty_suite_when_no_findings():
    root = ET.fromstring(junit.render_findings([]))

    testsuite = root.find("testsuite")
    assert testsuite.get("tests") == "0"
    assert testsuite.findall("testcase") == []


def test_junit_disambiguates_duplicate_testcase_names():
    """GitLab's own JUnit parser silently drops every testcase after the
    first sharing the same name: two different findings (different
    file/Application) can share the exact same message."""
    first = Finding(
        rule_id="unknown-resource-hook",
        severity=Severity.WARNING,
        application="app-a",
        message="`argocd.argoproj.io/hook: PreSyncc` on `ConfigMap` `shared-name` doesn't match",
        file=Path("bootstrap/argocd-apps/app-a.yaml"),
    )
    second = Finding(
        rule_id="unknown-resource-hook",
        severity=Severity.WARNING,
        application="app-b",
        message="`argocd.argoproj.io/hook: PreSyncc` on `ConfigMap` `shared-name` doesn't match",
        file=Path("bootstrap/argocd-apps/app-b.yaml"),
    )

    root = ET.fromstring(junit.render_findings([first, second]))

    names = {tc.get("name") for tc in root.find("testsuite").findall("testcase")}
    assert len(names) == 2  # both stayed distinguishable, neither would be dropped by GitLab


def test_gitlab_codequality_fingerprint_is_stable_and_unique():
    payload = json.loads(gitlab_codequality.render_findings([_FINDING_NO_LINE, _FINDING_WITH_LINE]))

    fingerprints = {issue["fingerprint"] for issue in payload}
    assert len(fingerprints) == 2  # two distinct findings -> two distinct fingerprints

    # Same finding replayed -> same fingerprint (stable across CI runs).
    replay = json.loads(gitlab_codequality.render_findings([_FINDING_NO_LINE]))
    original = next(i for i in payload if i["check_name"] == "orphan-source")
    assert replay[0]["fingerprint"] == original["fingerprint"]
