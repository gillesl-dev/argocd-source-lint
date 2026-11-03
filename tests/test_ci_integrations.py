"""Runs the GitLab component's `script` and the GitHub Action's lint step under
`bash -eo pipefail`, with the CLI replaced by this checkout's code, to guard the
exit-code handling that CLI unit tests never reach."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parent.parent
APP = """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: web
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: {path}
  destination:
    server: https://kubernetes.default.svc
    namespace: web
"""
PYTHON = Path(sys.executable).as_posix()


def _bash() -> str | None:
    if sys.platform == "win32":
        # Not shutil.which("bash"): on Windows that can be WSL's bash.exe.
        git = shutil.which("git")
        for parent in Path(git).resolve().parents if git else []:
            if (parent / "bin" / "bash.exe").is_file():
                return str(parent / "bin" / "bash.exe")
        return None
    return shutil.which("bash")


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _gitlab(scope: Path, inputs: dict[str, str], tmp_path: Path) -> tuple[str, dict[str, str]]:
    template = (ROOT / "templates/lint.yml").read_text(encoding="utf-8")
    job = list(YAML(typ="safe").load_all(template))[1]["$[[ inputs.job_name ]]"]
    script = "\n".join(job["script"]).replace("$[[ inputs.scope ]]", scope.as_posix())
    for name, value in inputs.items():
        script = script.replace(f"$[[ inputs.{name.replace('-', '_')} ]]", value)
    shim = f'argocd-source-lint() {{ "{PYTHON}" -m argocd_source_lint.cli "$@"; }}\n'
    return shim + script, {}


def _action(scope: Path, inputs: dict[str, str], tmp_path: Path) -> tuple[str, dict[str, str]]:
    action = YAML(typ="safe").load((ROOT / "action.yml").read_text(encoding="utf-8"))
    step = next(step for step in action["runs"]["steps"] if step.get("id") == "lint")
    values = {"path": scope.as_posix(), "upload-sarif": "true", **inputs}
    env = {
        key: str(value).replace("${{ inputs.", "").replace(" }}", "")
        for key, value in step["env"].items()
    }
    env = {key: values[name] for key, name in env.items()}
    # Stands in for the venv the install step creates under RUNNER_TEMP.
    bin_dir = tmp_path / "runner" / "argocd-source-lint" / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "argocd-source-lint"
    shim.write_bytes(
        f'#!/usr/bin/env bash\nexec "{PYTHON}" -m argocd_source_lint.cli "$@"\n'.encode()
    )
    shim.chmod(0o755)
    env["RUNNER_TEMP"] = (tmp_path / "runner").as_posix()
    env["GITHUB_OUTPUT"] = (tmp_path / "github_output").as_posix()
    return step["run"], env


def _run(script: str, env: dict[str, str], cwd: Path) -> int:
    return subprocess.run(
        [BASH, "-eo", "pipefail", "-c", script],
        cwd=cwd,
        env={**os.environ, **env},
        capture_output=True,
    ).returncode


CASES = [
    # (repo, fail-on-error, fail-on-empty, args, expected exit code)
    ("errors", "true", "true", "", 1),
    ("errors", "false", "true", "", 0),
    ("clean", "true", "true", "", 0),
    ("missing", "false", "true", "", 2),  # tool error: fails even in report-only mode
    ("empty", "true", "true", "", 1),  # no Application: usually a wrong path
    ("empty", "true", "false", "", 0),
    ("errors", "true", "true", "--skip phantom-target", 0),
]


@pytest.mark.parametrize("integration", [_gitlab, _action], ids=["gitlab", "action"])
@pytest.mark.parametrize(("repo", "fail_on_error", "fail_on_empty", "args", "expected"), CASES)
def test_ci_integration_exit_code(
    git_repo, tmp_path, integration, repo, fail_on_error, fail_on_empty, args, expected
):
    manifests = {"manifests/web/cm.yaml": "kind: ConfigMap\n"}
    if repo == "missing":
        scope = tmp_path / "does-not-exist"
    elif repo == "empty":
        scope = git_repo(manifests)
    else:
        path = "manifests/web" if repo == "clean" else "manifests/nowhere"
        scope = git_repo({"apps/web.yaml": APP.format(path=path), **manifests})
    inputs = {"fail-on-error": fail_on_error, "fail-on-empty": fail_on_empty, "args": args}

    script, env = integration(scope, inputs, tmp_path)

    assert _run(script, env, tmp_path) == expected


@pytest.mark.parametrize(
    ("fail_on_error", "expected"),
    [("True", 1), ("TRUE", 1), ("False", 0), ("yes", 2)],
)
def test_action_boolean_inputs_are_case_insensitive_and_checked(
    git_repo, tmp_path, fail_on_error, expected
):
    scope = git_repo({"apps/web.yaml": APP.format(path="manifests/nowhere")})
    inputs = {"fail-on-error": fail_on_error, "fail-on-empty": "true", "args": ""}

    script, env = _action(scope, inputs, tmp_path)

    assert _run(script, env, tmp_path) == expected


def test_action_removes_a_stale_report_on_an_input_error(git_repo, tmp_path):
    scope = git_repo({"apps/web.yaml": APP.format(path="manifests/nowhere")})
    stale = tmp_path / "results.sarif"
    stale.write_bytes(b"{}")
    inputs = {"fail-on-error": "maybe", "fail-on-empty": "true", "args": ""}

    script, env = _action(scope, inputs, tmp_path)

    assert _run(script, env, tmp_path) == 2
    assert not stale.exists()


def test_action_removes_a_stale_report_before_installing():
    action = YAML(typ="safe").load((ROOT / "action.yml").read_text(encoding="utf-8"))
    install = next(
        s for s in action["runs"]["steps"] if s.get("name") == "Install argocd-source-lint"
    )

    lines = install["run"].splitlines()

    assert lines.index("rm -f results.sarif") < next(
        i for i, line in enumerate(lines) if "venv" in line
    )
