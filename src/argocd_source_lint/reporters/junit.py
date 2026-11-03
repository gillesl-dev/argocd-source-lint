from __future__ import annotations

from xml.etree import ElementTree as ET

from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.reporters.safe_text import visible_controls

# JUnit has no warning or info level. The severities that fail CI by default
# (cli._exit_code) become <failure>. The others become <skipped>, which
# Jenkins, GitLab and Azure DevOps show apart from a pass.
_FAILING = {Severity.ERROR, Severity.UNVERIFIABLE}


def render_findings(findings: list[Finding]) -> str:
    failures = sum(1 for finding in findings if finding.severity in _FAILING)
    skipped = len(findings) - failures

    attrs = {
        "name": "argocd-source-lint",
        "tests": str(len(findings)),
        "failures": str(failures),
        "errors": "0",
        "skipped": str(skipped),
        "time": "0",
    }
    testsuite = ET.Element("testsuite", attrs)
    seen: dict[tuple[str, str], int] = {}
    for finding in findings:
        testsuite.append(_testcase(finding, seen))

    testsuites = ET.Element("testsuites", attrs)
    testsuites.append(testsuite)

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(testsuites, encoding="unicode")


def _testcase(finding: Finding, seen: dict[tuple[str, str], int]) -> ET.Element:
    # GitLab's JUnit parser drops testcases whose name repeats an earlier
    # one, and two findings in different files can share a message.
    key = (finding.rule_id, finding.message)
    seen[key] = seen.get(key, 0) + 1
    # XML 1.0 rejects most control characters, which repo content can carry.
    message = visible_controls(finding.message)
    name = message if seen[key] == 1 else f"{message} (#{seen[key]})"

    attrs = {
        "classname": visible_controls(finding.rule_id),
        "name": name,
        "file": visible_controls(finding.file.as_posix()),
        "time": "0",
    }
    if finding.line is not None:
        attrs["line"] = str(finding.line)

    testcase = ET.Element("testcase", attrs)
    if finding.severity in _FAILING:
        ET.SubElement(testcase, "failure", {"message": message, "type": finding.severity.value})
    else:
        ET.SubElement(testcase, "skipped", {"message": f"{finding.severity.value}: not blocking"})
    return testcase
