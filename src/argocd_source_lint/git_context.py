from __future__ import annotations

import atexit
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
    """A real git revision (branch, tag, SHA) never starts with `-`:
    `git check-ref-format` itself disallows a ref starting with `-`, and
    a hex SHA can't either. But `revision` here always comes from
    repo-controlled YAML (a source's `targetRevision`, or an
    ApplicationSet `git` generator's own `revision`), and git's argument
    parser doesn't know that: passed straight through as a positional
    argument, a value like `--remote=<url>` is read as a *flag*, not a
    revision. Confirmed for real, not theoretical, and worse than a
    parse error: `git archive "--remote=https://<host>/x"` (exactly
    `materialize_revision`'s own command below) spends the full connect
    timeout actually reaching out to `<host>` instead of failing
    instantly: a live SSRF primitive from inside whatever CI job runs
    this tool, breaking the "no network access" guarantee the whole
    tool is built on (see DESIGN.md "Mono-repo v1 scope"). Every
    function in this module that shells out with a `revision` argument
    checks this first and treats a rejected value the same as any other
    unresolvable revision, never passing it to `git` at all."""
    return not revision.startswith("-")


def is_git_available() -> bool:
    """Every rule that resolves a source ultimately shells out to `git`
    (revision lookups, tree listings, the `targetRevision`-drift
    snapshot); but `get_origin_url` is the only one of those call
    sites that catches a missing binary, and it does so by returning
    `None`, the exact same value it returns for a repo with no `origin`
    remote configured at all. Every rule already treats that `None` as
    "nothing here is local", correct for a missing remote, silently
    wrong for a missing `git`: every source floods `orphan-source`/
    `double-coverage` as a false "not covered" instead of a clear error
    (confirmed against `python:3.11-slim`, the base image this repo's
    own `templates/lint.yml` recommends, which doesn't ship `git`).
    Checked once, loudly, at startup instead."""
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
    """Reduces a Git URL (https, ssh://, or scp-like git@host:path) to
    `host/path` in lowercase, without a port or `.git` suffix, so that
    different forms of the same URL can be compared against each other."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]

    if "://" in url:
        url = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", url)
        url = re.sub(r"^[^@/]+@", "", url)
        # Explicit port on the host (e.g. ssh://git@host:2222/...) must not
        # make this diverge from the equivalent scp-like form
        # (git@host:path), which never carries a port.
        url = re.sub(r"^([^/]+):\d+(/|$)", r"\1\2", url)
    else:
        match = _SCP_LIKE_RE.match(url)
        if match:
            host, path = match.groups()
            url = f"{host}/{path}"

    return url.lower().rstrip("/")


def is_local_repo_url(repo_url: str, local_origin: str | None) -> bool:
    if not repo_url or not local_origin:
        return False
    return normalize_repo_url(repo_url) == normalize_repo_url(local_origin)


def local_path_sources(app: Application, local_origin: str | None) -> list[Source]:
    """Sources with a `path` (not just a `chart`) whose repoURL matches the
    local repo. Chart-only sources (Helm repo) are excluded outright: they
    never have any files to resolve locally."""
    return [
        source
        for source in app.sources
        if source.path is not None and is_local_repo_url(source.repo_url, local_origin)
    ]


def external_path_sources(app: Application, local_origin: str | None) -> list[Source]:
    """Sources with a `path` that points to a Git repo different from the
    local repo (see DESIGN.md "Mono-repo v1 scope"): the "out of scope
    v1" counterpart to `local_path_sources`. An Application can have
    sources in both lists at once (mixed external chart + local manifests
    pattern): each rule must report these sources as `info` individually,
    without skipping verification of its local sources."""
    return [
        source
        for source in app.sources
        if source.path is not None and not is_local_repo_url(source.repo_url, local_origin)
    ]


def is_revision_resolvable(repo_root: Path, revision: str) -> bool:
    """Shallow-clone safeguard (see DESIGN.md "The unverifiable severity"):
    a revision not fetched locally must never be treated as "path
    missing"; only as unverifiable. Used by `phantom-target` and
    `broken-values-ref`."""
    if not _is_safe_revision(revision):
        return False
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
    )
    return result.returncode == 0


_RESOLVED_COMMIT_CACHE: dict[tuple[Path, str], str | None] = {}


def _resolve_commit(repo_root: Path, revision: str) -> str | None:
    # `revision_matches_checkout` (via `coverage._resolved_source_root`)
    # runs once per local source, per rule, with N local-source rules
    # and M sources sharing the same `targetRevision` (`HEAD`, usually),
    # that's N*M identical `git rev-parse` calls for the exact same
    # answer. Confirmed to matter for real: 50 Applications, all
    # `targetRevision: HEAD`, cost 702 of them (~58s on Windows, where
    # subprocess spawn itself dominates). Cleared once per CLI
    # invocation (`clear_caches`), not just kept for the process
    # lifetime: the repo's HEAD can genuinely move between two
    # `argocd-source-lint` runs sharing a process only in tests
    # (`CliRunner.invoke` twice against the same mutated repo), never
    # in real usage (one process per run).
    if not _is_safe_revision(revision):
        return None
    cache_key = (repo_root, revision)
    if cache_key in _RESOLVED_COMMIT_CACHE:
        return _RESOLVED_COMMIT_CACHE[cache_key]

    result = subprocess.run(
        ["git", "rev-parse", f"{revision}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    resolved = result.stdout.strip() if result.returncode == 0 else None
    _RESOLVED_COMMIT_CACHE[cache_key] = resolved
    return resolved


def clear_caches() -> None:
    """Resets every module-level cache here (`_resolve_commit`,
    `materialize_revision`, `tree_paths_at_revision`); called once at
    the start of `cli.lint`, so each CLI invocation starts clean rather
    than these persisting for the life of the process (see
    `_resolve_commit`)."""
    _RESOLVED_COMMIT_CACHE.clear()
    _REVISION_SNAPSHOT_CACHE.clear()
    _FULL_TREE_CACHE.clear()


def revision_matches_checkout(repo_root: Path, revision: str) -> bool | None:
    """Whether `revision` resolves to the same commit as the current
    checkout (`HEAD`); used by `revision-mismatch` (see DESIGN.md
    "orphan-source, missing-ignore-diff and double-coverage read the
    working tree, not `targetRevision`"). `None` when `revision` isn't
    resolvable at all: that's already `phantom-target`'s/
    `broken-values-ref`'s shallow-clone case (`is_revision_resolvable`),
    not a mismatch to report a second time."""
    target = _resolve_commit(repo_root, revision)
    if target is None:
        return None
    head = _resolve_commit(repo_root, "HEAD")
    return head is not None and head == target


_REVISION_SNAPSHOT_CACHE: dict[tuple[Path, str], Path | None] = {}


def materialize_revision(repo_root: Path, revision: str) -> Path | None:
    """A full extraction of `repo_root` as it existed at `revision` into a
    throwaway directory, so filesystem-based logic elsewhere (coverage
    computation, the ApplicationSet `git` generator's own `directories`/
    `files` discovery) can be reused completely unchanged against it,
    including cross-directory Kustomize references, which a partial
    extraction of just one source's own `path` would silently fail to
    resolve. Reused across every caller pinned to the same revision
    within this run; cleaned up at process exit. `None` if `revision`
    doesn't produce a tree (shouldn't happen: callers only reach this
    after `revision_matches_checkout` confirmed it resolves, or
    `is_revision_resolvable` directly), or if the archive itself can't
    be safely extracted (see `filter="data"` below)."""
    if not _is_safe_revision(revision):
        return None
    cache_key = (repo_root, revision)
    if cache_key in _REVISION_SNAPSHOT_CACHE:
        return _REVISION_SNAPSHOT_CACHE[cache_key]

    result = subprocess.run(
        ["git", "archive", revision],
        cwd=repo_root,
        capture_output=True,
    )
    snapshot_root: Path | None = None
    if result.returncode == 0 and result.stdout:
        # `.resolve()`: on Windows, `tempfile.mkdtemp()` can return a
        # short (8.3) path form that a file's own `.resolve()` inside it
        # never reproduces, breaking `relative_to()` below on a textual
        # mismatch despite being the same directory.
        tmp_root = Path(tempfile.mkdtemp(prefix="argocd-source-lint-")).resolve()
        atexit.register(shutil.rmtree, tmp_root, ignore_errors=True)
        try:
            with tarfile.open(fileobj=BytesIO(result.stdout)) as tar:
                # `filter="data"` (tar-slip hardening, PEP 706) rejects a
                # `../`-style entry or an absolute path instead of writing
                # outside `tmp_root`; confirmed for real, not assumed:
                # a crafted entry raises `OutsideDestinationError` rather
                # than extracting. That's still an unhandled exception
                # without this `try`, so a tar stream this tool doesn't
                # control (git's own tree, however unlikely to contain
                # such a path in practice) would crash the whole CLI
                # instead of just leaving this one revision unresolved.
                tar.extractall(tmp_root, filter="data")
                snapshot_root = tmp_root
        except tarfile.TarError:
            pass

    _REVISION_SNAPSHOT_CACHE[cache_key] = snapshot_root
    return snapshot_root


def list_tree_paths(repo_root: Path, revision: str, pathspec: str) -> list[str]:
    """File paths under `pathspec` at `revision`. Assumes `revision` is
    already known to be resolvable (`is_revision_resolvable`); otherwise
    returns an empty list instead of failing loudly."""
    if not _is_safe_revision(revision):
        return []
    result = subprocess.run(
        ["git", "ls-tree", "-r", revision, "--name-only", "--", pathspec],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


_FULL_TREE_CACHE: dict[tuple[Path, str], frozenset[str]] = {}


def tree_paths_at_revision(repo_root: Path, revision: str) -> frozenset[str]:
    """Every file path at `revision`, one `git ls-tree` of the whole tree
    per `(repo_root, revision)`, cached the same way and for the same
    reason as `_resolve_commit`. Callers that used to ask `list_tree_paths`
    the same revision-scoped question once per distinct path (`phantom-
    target`: one `git ls-tree` per Application, even though most share
    `targetRevision: HEAD`) get it for the cost of one call total instead.
    Cleared by `clear_caches`."""
    cache_key = (repo_root, revision)
    if cache_key not in _FULL_TREE_CACHE:
        _FULL_TREE_CACHE[cache_key] = frozenset(list_tree_paths(repo_root, revision, "."))
    return _FULL_TREE_CACHE[cache_key]


def path_has_tracked_files(repo_root: Path, revision: str, pathspec: str) -> bool:
    """Whether `pathspec` (a file, or a directory containing at least one
    tracked file) exists at `revision`: the same question
    `list_tree_paths(repo_root, revision, pathspec)` answers via its own
    `git ls-tree` call, backed instead by `tree_paths_at_revision`'s
    single whole-tree call. A literal path match/prefix check, same as
    git's own pathspec matching for a plain (non-glob) path; a
    `pathspec` containing glob metacharacters (`*`, `?`, `[`), not a
    real ArgoCD `source.path` value, would not match here the way
    `git ls-tree` itself would expand it."""
    pathspec = pathspec.strip("/") or "."
    paths = tree_paths_at_revision(repo_root, revision)
    if pathspec == ".":
        return bool(paths)
    prefix = pathspec + "/"
    return any(path == pathspec or path.startswith(prefix) for path in paths)
