"""Every place that names a release must name the one in pyproject.toml.
The README's CI examples were left one version behind twice (1.1.1, 1.2.1),
and the GitHub Action/GitLab template install that exact version."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
    "version"
]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_github_action_installs_this_release():
    assert re.findall(r"argocd-source-lint==([\w.]+)", _read("action.yml")) == [VERSION]


def test_gitlab_template_installs_this_release():
    assert re.findall(r"argocd-source-lint==([\w.]+)", _read("templates/lint.yml")) == [VERSION]


def test_readme_ci_examples_reference_this_release():
    readme = _read("README.md")
    references = {
        "GitHub Action": re.findall(r"gillesl-dev/argocd-source-lint@v([\w.]+)", readme),
        "GitLab component": re.findall(r"argocd-source-lint/lint@v([\w.]+)", readme),
        "pre-commit": re.findall(r"rev: v([\w.]+)", readme),
    }

    assert references == {name: [VERSION] for name in references}
