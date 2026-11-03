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
    """A repo-controlled revision such as `--remote=<url>` would reach `git archive`
    as a flag and open a network connection, so every function must reject a
    leading `-` before spawning `git`."""
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
    """Applications sharing `targetRevision: HEAD` would otherwise each spawn
    `git rev-parse` again, which dominates run time on Windows."""
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
    """`apps/app-1` and `apps/app-10` share a prefix: a `startswith` check without
    the trailing `/` would treat one as covering the other."""
    repo_root = git_repo({"apps/app-10/deployment.yaml": "x"})
    assert path_has_tracked_files(repo_root, "HEAD", "apps/app-1") is False


def test_path_has_tracked_files_true_for_an_exact_file_path(git_repo):
    repo_root = git_repo({"manifests/app.yaml": "x"})
    assert path_has_tracked_files(repo_root, "HEAD", "manifests/app.yaml") is True


def test_tree_paths_at_revision_is_cached_across_repeated_calls(git_repo):
    """`phantom-target` asks for the tree once per Application, and most
    Applications share `targetRevision: HEAD`."""
    repo_root = git_repo({"apps/app-1/deployment.yaml": "x"})
    clear_caches()

    with patch("subprocess.run", wraps=subprocess.run) as spy:
        for _ in range(5):
            assert path_has_tracked_files(repo_root, "HEAD", "apps/app-1") is True

    listings = [c for c in spy.call_args_list if c.args[0][1] in ("ls-tree", "ls-files")]
    assert len(listings) == 1


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
    """Two CLI runs in one process (only possible in a test harness) must see a
    commit made between them."""
    repo_root = git_repo({"a.txt": "x"})
    git_tag(repo_root, "v1.0")
    clear_caches()
    assert revision_matches_checkout(repo_root, "v1.0") is True  # HEAD == v1.0 right now

    git_commit(repo_root, {})  # HEAD moves past v1.0
    clear_caches()

    assert revision_matches_checkout(repo_root, "v1.0") is False


def _git(repo_root, *args):
    subprocess.run(["git", *args], cwd=repo_root, check=True, capture_output=True)


def _pull_request_checkout(repo_root, git_commit):
    """Same shape as a CI checkout of a pull request: detached HEAD on a
    newer commit, the target branch present only as `origin/main`."""
    _git(repo_root, "update-ref", "refs/remotes/origin/main", "HEAD")
    git_commit(repo_root, {"pr.txt": "x"}, message="pull request change")
    _git(repo_root, "checkout", "-q", "--detach")
    for branch in ("main", "master"):
        subprocess.run(["git", "branch", "-q", "-D", branch], cwd=repo_root, capture_output=True)


def test_branch_present_only_as_origin_branch_is_resolved(git_repo, git_commit):
    repo_root = git_repo({"manifests/app/deployment.yaml": "kind: Deployment\n"})
    _pull_request_checkout(repo_root, git_commit)
    clear_caches()

    assert is_revision_resolvable(repo_root, "main") is True
    assert revision_matches_checkout(repo_root, "main") is False
    assert path_has_tracked_files(repo_root, "main", "manifests/app") is True
    assert "pr.txt" not in tree_paths_at_revision(repo_root, "main")
    snapshot = materialize_revision(repo_root, "main")
    assert snapshot is not None
    assert (snapshot / "manifests/app/deployment.yaml").is_file()


def test_local_branch_wins_over_origin_branch(git_repo, git_commit):
    repo_root = git_repo({"a.txt": "x"})
    _git(repo_root, "branch", "-M", "main")
    _git(repo_root, "update-ref", "refs/remotes/origin/main", "HEAD")
    git_commit(repo_root, {"b.txt": "y"}, message="local only")
    clear_caches()

    assert revision_matches_checkout(repo_root, "main") is True


def test_revision_missing_locally_and_on_origin_stays_unresolvable(git_repo):
    repo_root = git_repo({"a.txt": "x"})
    clear_caches()

    assert is_revision_resolvable(repo_root, "no-such-branch") is False
    assert list_tree_paths(repo_root, "no-such-branch", ".") == []
