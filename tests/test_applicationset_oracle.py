"""ApplicationSet expansion compared with Argo CD's own controller code.

The cases are in fixtures/appset_oracle_cases.yaml. The expected results in
fixtures/appset_oracle_expected.json come from tools/argocd-oracle, which
runs Argo CD's template.GenerateApplications (see CONTRIBUTING.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from argocd_source_lint import applicationset
from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.models import Severity

FIXTURES = Path(__file__).parent / "fixtures"
CASES = YAML(typ="safe").load((FIXTURES / "appset_oracle_cases.yaml").read_text(encoding="utf-8"))
EXPECTED = json.loads((FIXTURES / "appset_oracle_expected.json").read_text(encoding="utf-8"))


def _as_oracle(app) -> dict:
    return {
        "name": app.name,
        "namespace": app.namespace,
        "project": app.project,
        "sources": [
            {
                "repoURL": source.repo_url,
                "path": source.path or "",
                "targetRevision": source.target_revision,
                "chart": source.chart or "",
            }
            for source in app.sources
        ],
        "destinationServer": app.destination_server or "",
        "destinationName": app.destination_name or "",
        "destinationNamespace": app.destination_namespace or "",
    }


def _key(app: dict) -> str:
    return json.dumps(app, sort_keys=True)


def test_every_case_has_an_expected_result():
    assert sorted(EXPECTED) == sorted(case["name"] for case in CASES)


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_expansion_matches_argo_cd(git_repo, case):
    repo_root = git_repo({**(case.get("files") or {}), "bootstrap/appset.yaml": case["appset"]})
    expected = EXPECTED[case["name"]]

    applications, findings = applicationset.discover(
        repo_root, get_origin_url(repo_root), Severity.INFO
    )

    if expected.get("error"):
        # Argo CD stops: this tool must generate nothing and say why.
        assert applications == []
        assert len(findings) == 1
        assert "Argo CD can't expand" in findings[0].message
    else:
        assert findings == []
        assert sorted(map(_key, map(_as_oracle, applications))) == sorted(
            map(_key, expected["applications"])
        )
