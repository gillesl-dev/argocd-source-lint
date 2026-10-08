from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from argocd_source_lint.git_context import (
    clear_caches,
    is_git_available,
    is_local_repo_url,
    is_revision_resolvable,
    list_tree_paths,
    materialize_revision,
    normalize_repo_url,
    path_has_tracked_files,
    revision_matches_checkout,
    tree_paths_at_revision,
)


def test_is_git_available_true_when_git_is_on_path():
    assert is_git_available() is True


def test_is_git_available_false_when_git_is_missing():
    with patch("shutil.which", return_value=None):
        assert is_git_available() is False


EQUIVALENT_FORMS = [
    "https://gitlab.example.com/group/example-repo.git",
    "https://gitlab.example.com/group/example-repo",
    "https://gitlab.example.com/group/example-repo.git/",
    "ssh://git@gitlab.example.com/group/example-repo.git",
    "ssh://git@gitlab.example.com:22/group/example-repo.git",
    "git@gitlab.example.com:group/example-repo.git",
]


@pytest.mark.parametrize("url", EQUIVALENT_FORMS)
def test_equivalent_url_forms_normalize_identically(url: str):
    assert normalize_repo_url(url) == "gitlab.example.com/group/example-repo"


@pytest.mark.parametrize("url", EQUIVALENT_FORMS)
def test_is_local_repo_url_matches_across_forms(url: str):
    local_origin = "git@gitlab.example.com:group/example-repo.git"
    assert is_local_repo_url(url, local_origin) is True


def test_is_local_repo_url_rejects_different_repo():
    assert (
        is_local_repo_url(
            "https://gitlab.example.com/group/other-repo.git",
            "git@gitlab.example.com:group/example-repo.git",
        )
        is False
    )


def test_is_local_repo_url_handles_missing_origin():
    assert is_local_repo_url("https://gitlab.example.com/group/example-repo.git", None) is False


def test_revision_matches_checkout_true_for_head(git_repo):
    repo_root = git_repo({"a.txt": "x"})
    assert revision_matches_checkout(repo_root, "HEAD") is True


def test_revision_matches_checkout_false_for_a_diverged_tag(git_repo, git_tag, git_commit):
    repo_root = git_repo({"a.txt": "x"})
    git_tag(repo_root, "v1.0")
    git_commit(repo_root, {})
    assert revision_matches_checkout(repo_root, "v1.0") is False


def test_revision_matches_checkout_none_for_unresolvable_revision(git_repo):
    repo_root = git_repo({"a.txt": "x"})
    assert revision_matches_checkout(repo_root, "does-not-exist-anywhere") is None


def test_flag_like_revision_is_rejected_without_ever_calling_git(git_repo):
    """A `targetRevision` (or an ApplicationSet `git` generator's own
    `revision`) is repo-controlled YAML, not a value this tool ever
    chose: a value like `--remote=<url>` passed straight through to
    `git archive` is read as a *flag*, not a revision, and git actually
    reaches out to `<url>` instead of failing to parse. Confirmed for
    real (manually, not in this test to avoid a slow/flaky network
    call): `git archive "--remote=https://192.0.2.1/x"` spends the full
    TCP connect timeout instead of erroring instantly. Every function
    here must reject a `-`-prefixed revision *before* it ever reaches a
    `git` subprocess (asserted here by spying on `subprocess.run`)."""
    repo_root = git_repo({"a.txt": "x"})
    clear_caches()
    malicious = "--remote=https://192.0.2.1/x"

    with patch("subprocess.run", wraps=subprocess.run) as spy:
        assert is_revision_resolvable(repo_root, malicious) is False
        assert revision_matches_checkout(repo_root, malicious) is None
        assert materialize_revision(repo_root, malicious) is None
        assert list_tree_paths(repo_root, malicious, ".") == []

    spy.assert_not_called()


def test_resolve_commit_is_cached_across_repeated_calls(git_repo):
    """50 Applications sharing `targetRevision: HEAD`, each checked by
    several local-source rules, cost 702 identical `git rev-parse`
    calls before this cache existed (~58s on Windows, where subprocess
    spawn itself dominates), confirmed for real, not estimated."""
    repo_root = git_repo({"a.txt": "x"})
    clear_caches()

    with patch("subprocess.run", wraps=subprocess.run) as spy:
        for _ in range(5):
            assert revision_matches_checkout(repo_root, "HEAD") is True

    rev_parse_calls = [c for c in spy.call_args_list if c.args[0][:2] == ["git", "rev-parse"]]
    assert len(rev_parse_calls) == 1  # not 10 (2 per call: revision + HEAD, x5 calls)


def test_path_has_tracked_files_true_for_a_directory_with_files(git_repo):
    repo_root = git_repo({"apps/app-1/deployment.yaml": "x"})
    assert path_has_tracked_files(repo_root, "HEAD", "apps/app-1") is True


def test_path_has_tracked_files_false_for_a_missing_path(git_repo):
    repo_root = git_repo({"apps/app-1/deployment.yaml": "x"})
    assert path_has_tracked_files(repo_root, "HEAD", "apps/does-not-exist") is False


def test_path_has_tracked_files_does_not_confuse_a_prefix_collision(git_repo):
    """`apps/app-1` and `apps/app-10` share a textual prefix but are
    different directories: a naive `str.startswith(pathspec)` (missing
    the trailing `/`) would wrongly treat the second as covering the
    first."""
    repo_root = git_repo({"apps/app-10/deployment.yaml": "x"})
    assert path_has_tracked_files(repo_root, "HEAD", "apps/app-1") is False


def test_path_has_tracked_files_true_for_an_exact_file_path(git_repo):
    repo_root = git_repo({"manifests/app.yaml": "x"})
    assert path_has_tracked_files(repo_root, "HEAD", "manifests/app.yaml") is True


def test_tree_paths_at_revision_is_cached_across_repeated_calls(git_repo):
    """`phantom-target` used to run one `git ls-tree` per Application even
    though most share `targetRevision: HEAD`; confirmed for real: 40
    Applications cost ~2.3s of it before this cache existed."""
    repo_root = git_repo({"apps/app-1/deployment.yaml": "x"})
    clear_caches()

    with patch("subprocess.run", wraps=subprocess.run) as spy:
        for _ in range(5):
            assert path_has_tracked_files(repo_root, "HEAD", "apps/app-1") is True

    ls_tree_calls = [c for c in spy.call_args_list if c.args[0][:2] == ["git", "ls-tree"]]
    assert len(ls_tree_calls) == 1


def test_clear_caches_picks_up_a_new_tree(git_repo, git_commit):
    repo_root = git_repo({"apps/app-1/deployment.yaml": "x"})
    clear_caches()
    assert path_has_tracked_files(repo_root, "HEAD", "apps/app-2") is False

    git_commit(repo_root, {"apps/app-2/deployment.yaml": "x"})
    clear_caches()

    assert path_has_tracked_files(repo_root, "HEAD", "apps/app-2") is True


def test_tree_paths_at_revision_lists_every_file(git_repo):
    repo_root = git_repo({"a.txt": "x", "dir/b.txt": "y"})
    assert tree_paths_at_revision(repo_root, "HEAD") == frozenset({"a.txt", "dir/b.txt"})


def test_clear_caches_picks_up_a_new_commit(git_repo, git_tag, git_commit):
    """The one scenario the cache must never get wrong: a repo mutated
    between two `argocd-source-lint` invocations that happen to share
    a process (only possible in a test harness calling the CLI twice
    in-process; a real run is always a fresh OS process)."""
    repo_root = git_repo({"a.txt": "x"})
    git_tag(repo_root, "v1.0")
    clear_caches()
    assert revision_matches_checkout(repo_root, "v1.0") is True  # HEAD == v1.0 right now

    git_commit(repo_root, {})  # HEAD moves past v1.0
    clear_caches()

    assert revision_matches_checkout(repo_root, "v1.0") is False
