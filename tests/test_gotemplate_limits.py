"""Bounds on Go template rendering: hostile ApplicationSets end quickly in `info`.

Templates come from the repository, which may not be trusted. Each bound has a
test whose template would run long or grow large without it; the test checks
that the ApplicationSet gets one "over this tool's limit" finding, no
Application, and returns within seconds.
"""

from __future__ import annotations

import time

import pytest

from argocd_source_lint import applicationset, gotemplate
from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.models import Severity

SECONDS = 10


def _appset(path_template: str, elements: str = "[{env: dev}]") -> str:
    return f"""apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {{name: s, namespace: argocd}}
spec:
  goTemplate: true
  generators:
    - list:
        elements: {elements}
  template:
    metadata: {{name: 'app-{{{{ .env }}}}'}}
    spec:
      project: default
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{path_template}'
      destination: {{server: https://kubernetes.default.svc, namespace: default}}
"""


def _expand(git_repo, appset: str):
    repo_root = git_repo({"bootstrap/appset.yaml": appset})
    start = time.monotonic()
    applications, findings = applicationset.discover(
        repo_root, get_origin_url(repo_root), Severity.INFO
    )
    return applications, findings, time.monotonic() - start


def _assert_limited(git_repo, appset: str, reason: str):
    applications, findings, seconds = _expand(git_repo, appset)
    assert applications == []
    assert len(findings) == 1
    assert "over this tool's limit" in findings[0].message
    assert reason in findings[0].message
    assert seconds < SECONDS, seconds


def test_template_field_size(git_repo):
    _assert_limited(git_repo, _appset("x" * 70_000), "template")


def test_output_size_of_one_field(git_repo):
    _assert_limited(git_repo, _appset("{{ range 10000 }}" + "x" * 30 + "{{ end }}"), "output")


def test_output_size_of_one_function_result(git_repo):
    _assert_limited(git_repo, _appset('{{ indent 100000000 "x" }}'), "output")


def test_output_size_of_printf_width(git_repo):
    _assert_limited(git_repo, _appset('{{ printf "%999999999s" "x" }}'), "output")


def test_output_size_of_a_doubling_variable(git_repo):
    template = '{{ $x := "ab" }}{{ range 60 }}{{ $x = printf "%s%s" $x $x }}{{ end }}{{ $x }}'
    _assert_limited(git_repo, _appset(template), "output")


def test_output_size_of_the_application_set(git_repo):
    elements = "[" + ", ".join(f"{{env: e{i}}}" for i in range(100)) + "]"
    template = "{{ range 200 }}" + "x" * 1000 + "{{ end }}"
    _assert_limited(git_repo, _appset(template, elements), "output")


def test_range_iterations(git_repo):
    _assert_limited(git_repo, _appset("{{ range 20000 }}{{ end }}"), "range")


def test_nested_range_iterations(git_repo):
    template = "{{ range 200 }}{{ range 200 }}{{ end }}{{ end }}"
    _assert_limited(git_repo, _appset(template), "range")


def test_operations_of_the_application_set(git_repo):
    elements = "[" + ", ".join(f"{{env: e{i}}}" for i in range(400)) + "]"
    template = "{{ range 9000 }}{{ if true }}{{ end }}{{ end }}"
    _assert_limited(git_repo, _appset(template, elements), "operations")


@pytest.mark.parametrize(
    "template",
    ["{{ if true }}" * 150 + "x" + "{{ end }}" * 150, "{{ " + "(" * 150 + "1" + ")" * 150 + " }}"],
    ids=["blocks", "parentheses"],
)
def test_nesting_depth(git_repo, template):
    _assert_limited(git_repo, _appset(template), "nest")


def test_bounds_leave_ordinary_templates_alone():
    params = {"items": [{"name": f"n{i}"} for i in range(1000)]}
    rendered = gotemplate.render("{{ range .items }}{{ .name }},{{ end }}", params)
    assert rendered.count(",") == 1000
