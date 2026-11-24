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
import re
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
RUNS = 3


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


# Argo CD builds some results by iterating over Go maps, whose order changes
# from run to run. Each pattern replaces the part of an error message that
# depends on that order.
_UNSTABLE_ERRORS = [
    # CombineStringMaps (applicationset/utils/utils.go) names the first
    # conflicting key it meets.
    (
        re.compile(r"found duplicate key \S+ with different value, a: .* ,b: .*$", re.DOTALL),
        "found duplicate key <key> with different value",
    ),
]


def _stable(outputs: dict) -> dict:
    """The oracle's outputs without what depends on Go's map order."""
    appsets = []
    for appset in outputs["appsets"]:
        appset = dict(appset)
        if appset.get("error"):
            for pattern, replacement in _UNSTABLE_ERRORS:
                appset["error"] = pattern.sub(replacement, appset["error"])
        if appset.get("applications"):
            # The merge generator emits its entries in map order; the tests
            # compare Applications as an unordered set.
            appset["applications"] = sorted(
                appset["applications"], key=lambda app: json.dumps(app, sort_keys=True)
            )
        appsets.append(appset)
    return {**outputs, "appsets": appsets}


def _decode_all(stdout: bytes) -> list[dict]:
    """The JSON documents the oracle runs printed one after the other."""
    text = stdout.decode("utf-8")
    decoder = json.JSONDecoder()
    documents, index = [], 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        document, index = decoder.raw_decode(text, index)
        documents.append(document)
    return documents


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
        # The oracle runs several times on the same input, so that an output
        # depending on Go's map order shows up here rather than as churn in
        # the expected files.
        script = (
            "apk add --no-cache git >/dev/null "
            "&& git config --global --add safe.directory '*' "
            "&& CGO_ENABLED=0 go build -o /tmp/oracle . "
            "&& cat > /tmp/input.json "
            f"&& for run in $(seq {RUNS}); do /tmp/oracle < /tmp/input.json || exit 1; done"
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
        runs = [_stable(outputs) for outputs in _decode_all(result.stdout)]

    if len(runs) != RUNS:
        sys.exit(f"expected {RUNS} oracle runs, got {len(runs)}")
    outputs = runs[0]
    unstable = [
        name
        for index, name in enumerate(names)
        if any(run["appsets"][index] != outputs["appsets"][index] for run in runs[1:])
    ]
    unstable += [
        f"render {index}: {render['template']!r}"
        for index, render in enumerate(renders)
        if any(run["renders"][index] != outputs["renders"][index] for run in runs[1:])
    ]
    if unstable or any(run["functions"] != outputs["functions"] for run in runs[1:]):
        sys.exit(
            "the oracle's results change from run to run; add a pattern to _UNSTABLE_ERRORS: "
            + ", ".join(unstable or ["functions"])
        )

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
