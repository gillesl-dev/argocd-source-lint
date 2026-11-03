from __future__ import annotations

import io
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from rich.console import Console

from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import DEFAULT_RULE_SEVERITIES, RULE_DESCRIPTIONS
from argocd_source_lint.reporters import github, gitlab_codequality, json_report, junit, sarif
from argocd_source_lint.reporters.table import render_findings as render_table
from argocd_source_lint.reporters.table import render_info_summary

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
    # driver.rules lists every known rule, not only those with results here.
    # The exact list is checked in
    # test_sarif_rules_metadata_covers_every_configurable_rule.
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
    """`driver.rules` is a third hand-maintained rule list: a missing rule still
    appears in `results` but loses its `fullDescription`, `helpUri` and
    `defaultConfiguration`."""
    assert set(RULE_DESCRIPTIONS) == set(DEFAULT_RULE_SEVERITIES)


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
    """`Table.add_row` parses strings as rich markup, and Application names,
    messages and filenames come from the repo: a `[link=...]` or style tag must
    not change the terminal output."""
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


def _external(rule_id: str, application: str = "web") -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=Severity.INFO,
        application=application,
        message="Source `charts/web` (external repo): out of scope v1",
        file=Path(f"apps/{application}.yaml"),
        line=7,
        external_source=True,
    )


def test_info_summary_counts_an_external_source_once_and_names_other_info_rules():
    infos = [
        _external("orphan-source"),
        _external("phantom-target"),
        _external("orphan-source", application="api"),
        _external("phantom-target", application="api"),
        Finding(
            rule_id="unresolvable-generator",
            severity=Severity.INFO,
            application="",
            message="`clusters` generator requires live cluster/API access",
            file=Path("appsets/x.yaml"),
        ),
    ]
    console = Console(file=io.StringIO(), no_color=True, width=200)
    render_info_summary(console, infos)

    output = console.file.getvalue()
    assert "5 info finding(s) not listed" in output
    assert "2 Application source(s) in another repo, not inspected: 4 finding(s) across 2" in output
    # An info that isn't about an external source is never folded into that line.
    assert "1 unresolvable-generator" in output


def test_info_summary_prints_nothing_without_info():
    console = Console(file=io.StringIO(), no_color=True, width=200)
    render_info_summary(console, [])

    assert console.file.getvalue() == ""


def test_external_source_flag_stays_out_of_machine_readable_reports():
    payload = json.loads(json_report.render_findings([_external("orphan-source")]))

    assert "external_source" not in payload["findings"][0]


_FINDING_WITH_CONTROLS = Finding(
    rule_id="phantom-target",
    severity=Severity.ERROR,
    application="evil\x1b[31mRED",
    message="path `nope\x1b[2J` not found\r",
    file=Path("bootstrap/argocd-apps/evil\x07.yaml"),
)


def test_table_shows_terminal_control_characters_instead_of_emitting_them():
    """`escape()` only covers rich markup, not raw control bytes such as ESC."""
    console = Console(file=io.StringIO(), no_color=True, width=300)
    render_table(console, [_FINDING_WITH_CONTROLS])

    output = console.file.getvalue()
    assert "\x1b" not in output
    assert "\x07" not in output
    assert "\r" not in output
    assert "evil\\x1b[31mRED" in output


def test_junit_stays_valid_xml_with_control_characters_in_findings():
    root = ET.fromstring(junit.render_findings([_FINDING_WITH_CONTROLS]))

    testcase = root.find("testsuite/testcase")
    assert testcase.get("name") == "path `nope\\x1b[2J` not found\\x0d"
    assert testcase.get("file") == "bootstrap/argocd-apps/evil\\x07.yaml"


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
    """GitLab's JUnit parser drops every testcase after the first with a given
    name, and two findings on different files can share a message."""
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
    assert len(names) == 2  # distinguishable, so GitLab drops neither


def test_gitlab_codequality_fingerprint_is_stable_and_unique():
    payload = json.loads(gitlab_codequality.render_findings([_FINDING_NO_LINE, _FINDING_WITH_LINE]))

    fingerprints = {issue["fingerprint"] for issue in payload}
    assert len(fingerprints) == 2  # two distinct findings -> two distinct fingerprints

    # Same finding replayed -> same fingerprint (stable across CI runs).
    replay = json.loads(gitlab_codequality.render_findings([_FINDING_NO_LINE]))
    original = next(i for i in payload if i["check_name"] == "orphan-source")
    assert replay[0]["fingerprint"] == original["fingerprint"]


def test_github_annotations_map_severities_and_carry_location():
    lines = github.render_findings([_FINDING_NO_LINE, _FINDING_WITH_LINE]).splitlines()

    assert lines[0] == (
        "::error file=manifests/orphaned/configmap.yaml,title=orphan-source"
        "::File not covered: manifests/orphaned/configmap.yaml"
    )
    assert lines[1].startswith(
        "::warning file=bootstrap/argocd-apps/demo-app.yaml,title=phantom-target,line=12::"
    )


def test_github_annotation_cannot_be_split_into_a_second_command():
    finding = Finding(
        rule_id="phantom-target",
        severity=Severity.INFO,
        application="x",
        message="50% done\n::error::forged",
        file=Path("a,b:c.yaml"),
    )

    output = github.render_findings([finding])

    assert "\n" not in output
    assert (
        output == "::notice file=a%2Cb%3Ac.yaml,title=phantom-target::50%25 done%0A::error::forged"
    )
