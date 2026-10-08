from __future__ import annotations

import subprocess
from pathlib import Path

from argocd_source_lint.coverage import (
    covered_documents_for_application,
    covered_files_for_application,
    covered_files_for_kustomize_dir,
    match_directory_patterns,
)
from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery


def _write(root: Path, relative: str, content: str = "kind: Deployment\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_resources_entry_is_covered_and_the_rest_is_not(tmp_path):
    _write(tmp_path, "kustomization.yaml", "resources:\n  - deployment.yaml\n")
    referenced = _write(tmp_path, "deployment.yaml")
    _write(tmp_path, "orphan.yaml")

    covered = covered_files_for_kustomize_dir(tmp_path)

    assert referenced.resolve() in covered
    assert (tmp_path / "orphan.yaml").resolve() not in covered
    assert (tmp_path / "kustomization.yaml").resolve() in covered  # the file itself


def test_resources_entry_pointing_at_a_directory_recurses(tmp_path):
    _write(tmp_path, "kustomization.yaml", "resources:\n  - base\n")
    _write(tmp_path, "base/kustomization.yaml", "resources:\n  - deployment.yaml\n")
    referenced = _write(tmp_path, "base/deployment.yaml")

    covered = covered_files_for_kustomize_dir(tmp_path)

    assert referenced.resolve() in covered


def test_remote_resource_reference_is_silently_skipped(tmp_path):
    _write(
        tmp_path,
        "kustomization.yaml",
        "resources:\n  - deployment.yaml\n  - https://example.invalid/other.yaml\n",
    )
    referenced = _write(tmp_path, "deployment.yaml")

    covered = covered_files_for_kustomize_dir(tmp_path)

    assert covered == {tmp_path.resolve() / "kustomization.yaml", referenced.resolve()}


def test_bases_and_components_are_covered_like_resources(tmp_path):
    _write(
        tmp_path,
        "kustomization.yaml",
        "bases:\n  - base.yaml\ncomponents:\n  - component.yaml\n",
    )
    base = _write(tmp_path, "base.yaml")
    component = _write(tmp_path, "component.yaml")

    covered = covered_files_for_kustomize_dir(tmp_path)

    assert base.resolve() in covered
    assert component.resolve() in covered


def test_patches_strategic_merge_file_entry_is_covered_inline_entry_is_not_a_path(tmp_path):
    _write(
        tmp_path,
        "kustomization.yaml",
        "patchesStrategicMerge:\n"
        "  - patch.yaml\n"
        "  - |\n"
        "    apiVersion: apps/v1\n"
        "    kind: Deployment\n",
    )
    patch = _write(tmp_path, "patch.yaml")

    covered = covered_files_for_kustomize_dir(tmp_path)

    assert patch.resolve() in covered


def test_patches_and_patches_json6902_path_are_covered(tmp_path):
    _write(
        tmp_path,
        "kustomization.yaml",
        "patches:\n"
        "  - path: patch.yaml\n"
        "    target:\n"
        "      kind: Deployment\n"
        "patchesJson6902:\n"
        "  - path: json-patch.yaml\n"
        "    target:\n"
        "      kind: Service\n",
    )
    patch = _write(tmp_path, "patch.yaml")
    json_patch = _write(tmp_path, "json-patch.yaml")

    covered = covered_files_for_kustomize_dir(tmp_path)

    assert patch.resolve() in covered
    assert json_patch.resolve() in covered


def test_configmap_and_secret_generator_files_and_envs_are_covered(tmp_path):
    _write(
        tmp_path,
        "kustomization.yaml",
        "configMapGenerator:\n"
        "  - name: app-config\n"
        "    files:\n"
        "      - config.properties\n"
        "      - key=other.properties\n"
        "    envs:\n"
        "      - config.env\n"
        "secretGenerator:\n"
        "  - name: app-secret\n"
        "    envFile: secret.env\n",
    )
    config_file = _write(tmp_path, "config.properties")
    other_file = _write(tmp_path, "other.properties")
    config_env = _write(tmp_path, "config.env")
    secret_env = _write(tmp_path, "secret.env")

    covered = covered_files_for_kustomize_dir(tmp_path)

    assert config_file.resolve() in covered
    assert other_file.resolve() in covered
    assert config_env.resolve() in covered
    assert secret_env.resolve() in covered


def test_resources_cycle_does_not_infinite_loop(tmp_path):
    _write(tmp_path, "kustomization.yaml", "resources:\n  - loop\n")
    _write(tmp_path, "loop/kustomization.yaml", "resources:\n  - ..\n")

    covered = covered_files_for_kustomize_dir(tmp_path)

    assert (tmp_path / "kustomization.yaml").resolve() in covered


def test_kustomize_resources_entry_that_escapes_base_root_is_not_resolved(tmp_path):
    """A `resources:`/`bases:`/... entry comes from *tracked YAML
    content*, not ArgoCD's own schema; confirmed for real: without
    `base_root`, `../../../etc/passwd`-style entries were resolved and
    read exactly like a real reference, walking and leaking file
    content from anywhere on the host filesystem reachable from the
    repo, not just inside it (see DESIGN.md "A Kustomize overlay can
    read outside the repo")."""
    repo_root = tmp_path / "repo"
    outside = tmp_path / "outside-secret"
    leaked = _write(outside, "leaked.yaml", "kind: Secret\n")
    _write(repo_root, "kustomization.yaml", "resources:\n  - ../outside-secret/leaked.yaml\n")

    covered = covered_files_for_kustomize_dir(repo_root, repo_root.resolve())

    assert leaked.resolve() not in covered
    assert covered == {repo_root.resolve() / "kustomization.yaml"}


def test_kustomize_resources_entry_within_base_root_still_resolves(tmp_path):
    """Regression guard for the fix above: a legitimate `../` reference
    that stays inside the repo (a common Kustomize pattern, an overlay
    referencing a shared base a few levels up) must still resolve."""
    base = _write(tmp_path, "shared/base.yaml")
    _write(tmp_path, "overlays/prod/kustomization.yaml", "resources:\n  - ../../shared/base.yaml\n")

    covered = covered_files_for_kustomize_dir(tmp_path / "overlays" / "prod", tmp_path.resolve())

    assert base.resolve() in covered


def test_non_kustomize_directory_returns_empty_set(tmp_path):
    _write(tmp_path, "deployment.yaml")

    assert covered_files_for_kustomize_dir(tmp_path) == set()


def test_match_directory_patterns_is_case_sensitive():
    """ArgoCD's `directory.include`/`exclude` is matched by Go's
    `filepath.Match`, which never folds case on any platform. This must
    stay true regardless of the host OS running the linter, not silently
    over-match on Windows via `fnmatch`'s case-folding."""
    assert match_directory_patterns("deploy.yaml", "*.yaml") is True
    assert match_directory_patterns("Deploy.YAML", "*.yaml") is False


def test_match_directory_patterns_brace_alternatives_still_match():
    assert match_directory_patterns("app/deployment.yaml", "{*.yaml,app/*.yaml}") is True


def test_a_source_path_that_escapes_the_repo_covers_and_reads_nothing(tmp_path):
    """A source's own `path` is repo-controlled YAML too, same as a
    Kustomize `resources:` entry; confirmed for real: `path:
    ../outside-secret` used to walk and read a sibling directory's
    content entirely outside the repo, crashing
    `covered_files_for_application` (`Path.relative_to` on a path
    outside `base_root`) and silently leaking file content through
    `covered_documents_for_application` (no such check at all)."""
    repo_root = tmp_path / "repo"
    outside = tmp_path / "outside-secret"
    _write(outside, "leaked.yaml", "apiVersion: v1\nkind: Secret\nmetadata:\n  name: leaked\n")
    _write(
        repo_root,
        "app.yaml",
        "apiVersion: argoproj.io/v1alpha1\n"
        "kind: Application\n"
        "metadata:\n"
        "  name: app\n"
        "spec:\n"
        "  source:\n"
        "    repoURL: https://example.invalid/repo.git\n"
        "    targetRevision: HEAD\n"
        "    path: ../outside-secret\n",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/repo.git"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )

    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    app = applications[0]

    assert covered_files_for_application(app, repo_root, local_origin) == set()
    assert list(covered_documents_for_application(app, repo_root, local_origin)) == []
