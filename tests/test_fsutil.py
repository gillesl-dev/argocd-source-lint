from __future__ import annotations

import os
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from argocd_source_lint.fsutil import (
    clear_caches,
    discover_documents,
    is_within_budget,
    iter_yaml_files,
    load_yaml_documents,
    walk_tree,
)


def _symlink_or_skip(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("creating a directory symlink needs a privilege not available here")


def _alias_bomb_yaml(layers: int, tail: str) -> str:
    """A YAML "billion laughs" document: each layer aliases the previous
    one twice, so `layers` anchors (a tiny file) expand to 2**layers
    nodes if anything ever fully materializes it."""
    lines = ['a0: &a0 ["x"]']
    for i in range(1, layers):
        lines.append(f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]")
    lines.append(tail.format(ref=f"a{layers - 1}"))
    return "\n".join(lines) + "\n"


def test_is_within_budget_true_for_an_ordinary_document():
    doc = {"kind": "Application", "spec": {"source": {"path": "manifests/app"}}}

    assert is_within_budget(doc) is True


def test_is_within_budget_false_for_an_alias_bomb():
    text = _alias_bomb_yaml(30, "bomb: *{ref}")
    doc = YAML(typ="rt").load(text)

    assert is_within_budget(doc) is False


def test_is_within_budget_reuses_a_shared_subtree_across_siblings():
    """The whole point: an alias referenced twice must be *sized* once,
    not twice; otherwise this check would be exponential itself."""
    doc = YAML(typ="rt").load('a: &x ["1", "2", "3"]\nb: [*x, *x, *x, *x, *x]\n')

    assert is_within_budget(doc) is True


def test_load_yaml_documents_silently_drops_an_alias_bomb(tmp_path: Path):
    """Same fallback as a file that fails to parse outright: never
    hangs, never raises."""
    path = tmp_path / "bomb.yaml"
    path.write_text(
        _alias_bomb_yaml(
            30,
            "apiVersion: *{ref}\nkind: Application\nmetadata:\n  name: bomb\n",
        ),
        encoding="utf-8",
    )

    assert load_yaml_documents(path) == []


def test_load_yaml_documents_still_parses_an_ordinary_multi_document_file(tmp_path: Path):
    path = tmp_path / "docs.yaml"
    path.write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n---\n"
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: b\n",
        encoding="utf-8",
    )

    docs = load_yaml_documents(path)

    assert [doc["metadata"]["name"] for doc in docs] == ["a", "b"]


def test_walk_tree_breaks_a_self_referential_directory_cycle(tmp_path: Path):
    """A directory symlink/junction pointing back at an ancestor of
    itself must terminate here (confirmed to hang `Path.rglob`, and
    even `os.walk(followlinks=False)`, since a Windows junction isn't
    reported as a symlink, so that guard never triggers)."""
    (tmp_path / "app.yaml").write_text("kind: ConfigMap\n", encoding="utf-8")
    _symlink_or_skip(tmp_path, tmp_path / "loop")

    paths = list(walk_tree(tmp_path))

    assert tmp_path / "app.yaml" in paths


def test_iter_yaml_files_breaks_a_self_referential_directory_cycle(tmp_path: Path):
    (tmp_path / "app.yaml").write_text("kind: ConfigMap\n", encoding="utf-8")
    _symlink_or_skip(tmp_path, tmp_path / "loop")

    found = list(iter_yaml_files(tmp_path))

    # `loop` points at `tmp_path` itself, so descending into it is
    # exactly the cycle being broken: `app.yaml` is found once, via
    # its real path, never via `loop` at all.
    assert found == [tmp_path / "app.yaml"]


def test_discover_documents_is_cached_across_repeated_calls(tmp_path: Path, monkeypatch):
    """`RawManifestDiscovery`, the ApplicationSet walker and
    `discover_app_projects` each call this looking for a different
    `kind`; without caching, that's the whole repo walked and parsed
    three times over. Confirmed to matter for real: 2,000 plain
    manifests (no Application/ApplicationSet/AppProject among them)
    cost ~5s across the three independent passes."""
    (tmp_path / "app.yaml").write_text(
        "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: a\n",
        encoding="utf-8",
    )
    clear_caches()
    calls = []
    real_iter_yaml_files = iter_yaml_files
    monkeypatch.setattr(
        "argocd_source_lint.fsutil.iter_yaml_files",
        lambda *a, **k: calls.append(1) or real_iter_yaml_files(*a, **k),
    )

    first = discover_documents(tmp_path)
    second = discover_documents(tmp_path)

    assert first == second
    assert len(calls) == 1


def test_clear_caches_picks_up_a_new_file(tmp_path: Path):
    clear_caches()
    assert discover_documents(tmp_path) == []

    (tmp_path / "app.yaml").write_text("kind: ConfigMap\n", encoding="utf-8")
    clear_caches()

    assert len(discover_documents(tmp_path)) == 1
