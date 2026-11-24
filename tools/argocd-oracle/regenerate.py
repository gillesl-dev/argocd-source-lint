"""Recompute the expected results of the Argo CD comparison tests with Argo CD's own code.

Each case of tests/fixtures/appset_oracle_cases.yaml becomes a Git repository
holding its files and its ApplicationSet (at bootstrap/appset.yaml), and the
oracle (main.go, importing Argo CD at the version pinned in go.mod) expands it
into tests/fixtures/appset_oracle_expected.json.

Each case of tests/fixtures/gotemplate_render_cases.yaml is a Go template that
the oracle renders as the controller renders a string of a goTemplate
ApplicationSet, into tests/fixtures/gotemplate_render_expected.json, along
with the template functions Argo CD accepts.

Needs Docker. Run from the repository root:

    uv run python tools/argocd-oracle/regenerate.py

Go modules and build outputs are cached in the `argocd-oracle-gomod` and
`argocd-oracle-gocache` Docker volumes, or in the directories named by
ORACLE_GOMODCACHE and ORACLE_GOCACHE.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
ORACLE = Path(__file__).resolve().parent
CASES = ROOT / "tests" / "fixtures" / "appset_oracle_cases.yaml"
EXPECTED = ROOT / "tests" / "fixtures" / "appset_oracle_expected.json"
RENDER_CASES = ROOT / "tests" / "fixtures" / "gotemplate_render_cases.yaml"
RENDER_EXPECTED = ROOT / "tests" / "fixtures" / "gotemplate_render_expected.json"
IMAGE = "golang:1.25.5-alpine"
APPSET_PATH = "bootstrap/appset.yaml"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=oracle", "-c", "user.email=oracle@invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _make_repo(root: Path, case: dict) -> None:
    root.mkdir(parents=True)
    files = {**(case.get("files") or {}), APPSET_PATH: case["appset"]}
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "case")


def _write_json(path: Path, data: object) -> None:
    path.write_bytes((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def render_inputs() -> list[dict]:
    """The render cases, each with its params set resolved."""
    data = YAML(typ="safe").load(RENDER_CASES.read_text(encoding="utf-8"))
    params = data["params"]
    return [
        {
            "template": case["template"],
            "params": params[case.get("params", "default")],
            "options": case.get("options") or [],
        }
        for case in data["cases"]
    ]


def main() -> int:
    cases = YAML(typ="safe").load(CASES.read_text(encoding="utf-8"))
    names = [case["name"] for case in cases]
    if len(set(names)) != len(names):
        sys.exit("duplicate case names")
    renders = render_inputs()

    with tempfile.TemporaryDirectory() as tmp:
        repos = Path(tmp)
        inputs = []
        for index, case in enumerate(cases):
            _make_repo(repos / str(index), case)
            inputs.append(
                {
                    "appset": case["appset"],
                    "repo": f"/repos/{index}",
                    "newGitFileGlobbing": bool(case.get("newGitFileGlobbing")),
                }
            )

        gomod = os.environ.get("ORACLE_GOMODCACHE", "argocd-oracle-gomod")
        gocache = os.environ.get("ORACLE_GOCACHE", "argocd-oracle-gocache")
        script = (
            "apk add --no-cache git >/dev/null "
            "&& git config --global --add safe.directory '*' "
            "&& CGO_ENABLED=0 go build -o /tmp/oracle . "
            "&& /tmp/oracle"
        )
        result = subprocess.run(
            [
                "docker", "run", "--rm", "-i",
                "-v", f"{ORACLE}:/src", "-v", f"{repos}:/repos", "-v", f"{gomod}:/go/pkg/mod",
                "-v", f"{gocache}:/root/.cache/go-build",
                "-w", "/src", IMAGE, "sh", "-c", script,
            ],
            input=json.dumps({"appsets": inputs, "renders": renders}).encode(),
            capture_output=True,
            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        )  # fmt: skip
        if result.returncode != 0:
            sys.stderr.write(result.stderr.decode(errors="replace"))
            return result.returncode
        outputs = json.loads(result.stdout)

    expected = dict(zip(names, outputs["appsets"], strict=True))
    _write_json(EXPECTED, expected)
    print(f"{len(expected)} cases written to {EXPECTED.relative_to(ROOT)}")
    rendered = [
        {**render, **output} for render, output in zip(renders, outputs["renders"], strict=True)
    ]
    _write_json(RENDER_EXPECTED, {"functions": outputs["functions"], "renders": rendered})
    print(f"{len(rendered)} renders written to {RENDER_EXPECTED.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
