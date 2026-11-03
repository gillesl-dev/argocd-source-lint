from __future__ import annotations

import atexit
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

from argocd_source_lint.models import Application, Source

_SCP_LIKE_RE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")


def _is_safe_revision(revision: str) -> bool:
    """Reject a repo-controlled revision that git would parse as an option.

    No valid ref or SHA starts with `-` (`git check-ref-format`), but
    `git archive --remote=<url>` would make a network request from CI,
    breaking the no-network guarantee (see DESIGN.md).
    A rejected value is treated as unresolvable.
    """
    return not revision.startswith("-")


def is_git_available() -> bool:
    """Whether a `git` binary is on PATH.

    Without it `get_origin_url` returns None, as for a repo with no
    `origin`, and every source would be reported as not covered. Slim
    images such as `python:3.11-slim` do not ship git.
    """
    return shutil.which("git") is not None


def get_origin_url(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return result.stdout.strip() or None


def normalize_repo_url(url: str) -> str:
    """Reduce an https, ssh:// or scp-like Git URL to lowercase `host/path`.

    The port and `.git` suffix are dropped so equivalent forms compare equal.
    """
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]

    if "://" in url:
        url = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", url)
        url = re.sub(r"^[^@/]+@", "", url)
        # The scp-like form (git@host:path) cannot carry a port, so drop it
        # here to keep ssh://git@host:2222/path equal to it.
        url = re.sub(r"^([^/]+):\d+(/|$)", r"\1\2", url)
    else:
        match = _SCP_LIKE_RE.match(url)
        if match:
            host, path = match.groups()
            url = f"{host}/{path}"

    return url.lower().rstrip("/")


# Other URLs ArgoCD uses for this repository (`repo_urls` in .argocd-lint.yaml),
# for an origin that differs from them, such as an SSH host alias or a mirror.
_REPO_URLS: set[str] = set()


def set_repo_urls(urls: list[str]) -> None:
    _REPO_URLS.clear()
    _REPO_URLS.update(normalize_repo_url(url) for url in urls)


_SSH_URL_RE = re.compile(r"^(ssh://)?([^/:]*?)@[^@]+$")


def argo_normalized_git_url(url: str) -> str:
    """`NormalizeGitURL` in Argo CD's util/git/git.go, which AppProject
    `sourceRepos` matching uses. Unlike `normalize_repo_url`, the scheme stays."""
    url = url.strip().lower()
    if _SSH_URL_RE.match(url) and not url.startswith("ssh://"):
        url = "ssh://" + url.replace(":", "/", 1)
    return url.removesuffix(".git").removeprefix("ssh://")


def is_local_repo_url(repo_url: str, local_origin: str | None) -> bool:
    if not repo_url:
        return False
    normalized = normalize_repo_url(repo_url)
    if normalized in _REPO_URLS:
        return True
    return local_origin is not None and normalized == normalize_repo_url(local_origin)


def local_path_sources(app: Application, local_origin: str | None) -> list[Source]:
    """Sources with a `path` whose repoURL matches the local repo.

    Chart-only sources have no local files and are excluded.
    """
    return [
        source
        for source in app.sources
        if source.path is not None and is_local_repo_url(source.repo_url, local_origin)
    ]


def external_path_sources(app: Application, local_origin: str | None) -> list[Source]:
    """Sources with a `path` in a different Git repo (see DESIGN.md).

    An Application can have both external and local sources, so rules
    report these individually and still check the local ones.
    """
    return [
        source
        for source in app.sources
        if source.path is not None and not is_local_repo_url(source.repo_url, local_origin)
    ]


def is_revision_resolvable(repo_root: Path, revision: str) -> bool:
    """Whether `revision` resolves to a commit in the local clone.

    In a shallow clone an unfetched revision is unverifiable, not a missing
    path (see DESIGN.md).
    """
    return _resolve_commit(repo_root, revision) is not None


_RESOLVED_COMMIT_CACHE: dict[tuple[Path, str], str | None] = {}
_CHECKOUT_BRANCHES: set[str] = set()


def treat_branch_as_checkout(branch: str | None) -> None:
    """Resolve `branch` to HEAD from now on.

    A pull request pipeline checks out the change on top of its target branch,
    so an Application tracking that branch is checked against the change.
    """
    _CHECKOUT_BRANCHES.clear()
    if branch:
        _CHECKOUT_BRANCHES.add(branch)


def _resolve_commit(repo_root: Path, revision: str) -> str | None:
    # Called once per local source per rule, mostly with the same revision,
    # and subprocess spawn is slow on Windows. Cached per run, since HEAD can
    # move between runs in the same process.
    if not _is_safe_revision(revision):
        return None
    if revision in _CHECKOUT_BRANCHES:
        revision = "HEAD"
    cache_key = (repo_root, revision)
    if cache_key in _RESOLVED_COMMIT_CACHE:
        return _RESOLVED_COMMIT_CACHE[cache_key]

    # A CI checkout of a pull request is detached: the branch an
    # Application targets (`main`) often exists only as `origin/main`.
    resolved = _rev_parse(repo_root, revision) or _rev_parse(
        repo_root, f"refs/remotes/origin/{revision}"
    )
    _RESOLVED_COMMIT_CACHE[cache_key] = resolved
    return resolved


def _rev_parse(repo_root: Path, revision: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def clear_caches() -> None:
    """Reset the caches and the target branch. Called at the start of each lint run."""
    _CHECKOUT_BRANCHES.clear()
    _REPO_URLS.clear()
    _RESOLVED_COMMIT_CACHE.clear()
    _REVISION_SNAPSHOT_CACHE.clear()
    _FULL_TREE_CACHE.clear()


def revision_matches_checkout(repo_root: Path, revision: str) -> bool | None:
    """Whether `revision` resolves to the same commit as HEAD.

    See DESIGN.md. Returns None when
    `revision` does not resolve, which is not a mismatch.
    """
    target = _resolve_commit(repo_root, revision)
    if target is None:
        return None
    head = _resolve_commit(repo_root, "HEAD")
    return head is not None and head == target


_REVISION_SNAPSHOT_CACHE: dict[tuple[Path, str], Path | None] = {}


def materialize_revision(repo_root: Path, revision: str) -> Path | None:
    """Extract the whole tree at `revision` into a temporary directory.

    The full tree is needed so Kustomize references across directories
    resolve. Cached per revision and removed at process exit. Returns None
    if `revision` does not resolve or the archive cannot be safely extracted.
    """
    cache_key = (repo_root, revision)
    if cache_key in _REVISION_SNAPSHOT_CACHE:
        return _REVISION_SNAPSHOT_CACHE[cache_key]
    commit = _resolve_commit(repo_root, revision)
    if commit is None:
        return None

    result = subprocess.run(
        ["git", "archive", commit],
        cwd=repo_root,
        capture_output=True,
    )
    snapshot_root: Path | None = None
    if result.returncode == 0 and result.stdout:
        # On Windows `mkdtemp()` can return a short (8.3) path, which would
        # break `relative_to()` against resolved paths inside it.
        tmp_root = Path(tempfile.mkdtemp(prefix="argocd-source-lint-")).resolve()
        atexit.register(shutil.rmtree, tmp_root, ignore_errors=True)
        try:
            with tarfile.open(fileobj=BytesIO(result.stdout)) as tar:
                # `filter="data"` (PEP 706) raises on `../` or absolute
                # entries instead of writing outside `tmp_root`. The error is
                # caught so one bad revision stays unresolved instead of
                # crashing the run.
                tar.extractall(tmp_root, filter="data")
                snapshot_root = tmp_root
        except tarfile.TarError:
            pass

    _REVISION_SNAPSHOT_CACHE[cache_key] = snapshot_root
    return snapshot_root


def list_tree_paths(repo_root: Path, revision: str, pathspec: str) -> list[str]:
    """File paths under `pathspec` at `revision`, or [] if it does not resolve."""
    commit = _resolve_commit(repo_root, revision)
    if commit is None:
        return []
    result = subprocess.run(
        # -z: without it git quotes non-ASCII names ("caf\303\251").
        ["git", "ls-tree", "-r", "-z", commit, "--name-only", "--", pathspec],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return []
    return [path for path in result.stdout.split("\0") if path]


_FULL_TREE_CACHE: dict[tuple[Path, str], frozenset[str]] = {}


def tree_paths_at_revision(repo_root: Path, revision: str) -> frozenset[str]:
    """Every file path at `revision`, cached per revision.

    For the checked-out revision this is the working tree, not the commit, as
    the other rules read it: a pre-commit run sees files that are only staged.
    """
    cache_key = (repo_root, revision)
    if cache_key not in _FULL_TREE_CACHE:
        if revision_matches_checkout(repo_root, revision):
            paths = _working_tree_paths(repo_root)
        else:
            paths = list_tree_paths(repo_root, revision, ".")
        _FULL_TREE_CACHE[cache_key] = frozenset(paths)
    return _FULL_TREE_CACHE[cache_key]


def _working_tree_paths(repo_root: Path) -> list[str]:
    """Tracked and untracked, non-ignored paths, without files deleted from disk.

    Symlinks and submodules are kept, as `git ls-tree` lists them. So are
    files a sparse checkout leaves out (tag `S`): they are in the commit
    Argo CD clones.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "-t", "--cached", "--others", "--exclude-standard"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return []
    paths = []
    for entry in result.stdout.split("\0"):
        tag, _, path = entry.partition(" ")
        if path and (tag == "S" or os.path.lexists(repo_root / path)):
            paths.append(path)
    return paths


def path_has_tracked_files(repo_root: Path, revision: str, pathspec: str) -> bool:
    """Whether `pathspec` is a tracked file, or a directory with one, at `revision`.

    Matches literally, unlike git pathspecs: glob characters are not expanded.
    """
    pathspec = posixpath.normpath(pathspec.strip("/") or ".")
    paths = tree_paths_at_revision(repo_root, revision)
    if pathspec == ".":
        return bool(paths)
    prefix = pathspec + "/"
    return any(path == pathspec or path.startswith(prefix) for path in paths)
