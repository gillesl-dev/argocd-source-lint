"""A hostile ApplicationSet must end quickly with an `info` finding.

An ApplicationSet comes from the repository being linted, which may not be
trusted. Each test builds one that would be expensive to expand fully and
checks that the tool stops at its bound instead.
"""

from __future__ import annotations

import time

from argocd_source_lint import applicationset
from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.models import Severity

# Generous for a slow CI runner: unbounded, these cases take minutes or hang.
_SECONDS = 20


def _discover_timed(repo_root):
    start = time.perf_counter()
    result = applicationset.discover(repo_root, get_origin_url(repo_root), Severity.INFO)
    return result, time.perf_counter() - start


def _appset(generators: str) -> str:
    return (
        "apiVersion: argoproj.io/v1alpha1\n"
        "kind: ApplicationSet\n"
        "metadata:\n"
        "  name: hostile\n"
        "spec:\n"
        "  generators:\n"
        f"{generators}"
        "  template:\n"
        "    metadata:\n"
        "      name: 'app-{{id}}'\n"
        "    spec:\n"
        "      source:\n"
        "        repoURL: https://example.invalid/repo.git\n"
        "        targetRevision: HEAD\n"
        "        path: 'manifests/{{id}}'\n"
    )


def _elements_yaml(count: int, offset: int = 0) -> str:
    lines = "".join(f"        - id: '{offset + i}'\n" for i in range(count))
    return f"    - list:\n        elementsYaml: |\n{lines}".replace(
        "        - id", "          - id"
    )


def _assert_bounded(findings, elapsed, words):
    assert elapsed < _SECONDS, f"took {elapsed:.1f}s"
    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO
    assert words in findings[0].message


def test_application_count_is_bounded(git_repo):
    generators = "".join(_elements_yaml(1_000, offset=i * 1_000) for i in range(11))
    repo_root = git_repo({"bootstrap/appset.yaml": _appset(generators)})

    (applications, findings), elapsed = _discover_timed(repo_root)

    assert applications == []
    _assert_bounded(findings, elapsed, "more than 10000 Applications")


def test_chained_matrix_over_a_huge_first_child_is_bounded(git_repo):
    first = (
        _elements_yaml(12_000)
        .replace("    - list", "          - list")
        .replace("\n        ", "\n              ")
    )
    generators = (
        "    - matrix:\n"
        "        generators:\n"
        f"{first}"
        "          - git:\n"
        "              repoURL: https://example.invalid/repo.git\n"
        "              revision: '{{id}}'\n"
        "              files:\n"
        "                - path: 'envs/{{id}}.json'\n"
    )
    repo_root = git_repo({"bootstrap/appset.yaml": _appset(generators)})

    (applications, findings), elapsed = _discover_timed(repo_root)

    assert applications == []
    _assert_bounded(findings, elapsed, "first child has more than 10000 entries")


def test_one_git_revision_per_matrix_entry_is_bounded(git_repo):
    first = (
        _elements_yaml(500)
        .replace("    - list", "          - list")
        .replace("\n        ", "\n              ")
    )
    generators = (
        "    - matrix:\n"
        "        generators:\n"
        f"{first}"
        "          - git:\n"
        "              repoURL: https://example.invalid/repo.git\n"
        "              revision: 'branch-{{id}}'\n"
        "              files:\n"
        "                - path: 'envs/*.json'\n"
    )
    repo_root = git_repo({"bootstrap/appset.yaml": _appset(generators)})

    (applications, findings), elapsed = _discover_timed(repo_root)

    assert applications == []
    _assert_bounded(findings, elapsed, "more than 20 revisions")


def test_huge_params_file_is_not_read(git_repo):
    big = '{"id": "x", "pad": "' + "a" * (2 * 1024 * 1024) + '"}'
    generators = (
        "    - git:\n"
        "        repoURL: https://example.invalid/repo.git\n"
        "        revision: HEAD\n"
        "        files:\n"
        "          - path: 'envs/*.json'\n"
    )
    repo_root = git_repo({"envs/big.json": big, "bootstrap/appset.yaml": _appset(generators)})

    (applications, findings), elapsed = _discover_timed(repo_root)

    assert applications == []
    _assert_bounded(findings, elapsed, "is over 1024 KiB")


def test_deeply_nested_params_file_does_not_crash(git_repo):
    deep = "id: x\npad: " + "[" * 5_000 + "]" * 5_000 + "\n"
    generators = (
        "    - git:\n"
        "        repoURL: https://example.invalid/repo.git\n"
        "        revision: HEAD\n"
        "        files:\n"
        "          - path: 'envs/*.yaml'\n"
    )
    repo_root = git_repo({"envs/deep.yaml": deep, "bootstrap/appset.yaml": _appset(generators)})

    (applications, findings), elapsed = _discover_timed(repo_root)

    assert applications == []
    _assert_bounded(findings, elapsed, "nests deeper than 250 levels")
