from __future__ import annotations

from argocd_source_lint import applicationset
from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.models import Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.phantom_target import PhantomTargetRule


def _discover(repo_root):
    local_origin = get_origin_url(repo_root)
    return applicationset.discover(repo_root, local_origin, Severity.INFO)


def test_list_generator_expands_one_application_per_element(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appsets/list-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: list-appset
spec:
  generators:
    - list:
        elements:
          - env: dev
            path: apps/dev
          - env: prod
            path: apps/prod
  template:
    metadata:
      name: 'myapp-{{env}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{{path}}'
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert {app.name for app in applications} == {"myapp-dev", "myapp-prod"}
    paths = {app.sources[0].path for app in applications}
    assert paths == {"apps/dev", "apps/prod"}


def test_git_directories_generator_matches_local_directories(git_repo):
    repo_root = git_repo(
        {
            "apps/foo/deployment.yaml": "kind: Deployment\n",
            "apps/bar/deployment.yaml": "kind: Deployment\n",
            "bootstrap/appsets/dirs-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: dirs-appset
spec:
  generators:
    - git:
        repoURL: https://example.invalid/repo.git
        revision: HEAD
        directories:
          - path: apps/*
  template:
    metadata:
      name: '{{path.basename}}-app'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{{path}}'
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert {app.name for app in applications} == {"foo-app", "bar-app"}
    assert {app.sources[0].path for app in applications} == {"apps/foo", "apps/bar"}


def test_git_directories_exclude_entry_removes_a_match(git_repo):
    repo_root = git_repo(
        {
            "apps/foo/deployment.yaml": "kind: Deployment\n",
            "apps/baz/deployment.yaml": "kind: Deployment\n",
            "bootstrap/appsets/dirs-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: dirs-appset
spec:
  generators:
    - git:
        repoURL: https://example.invalid/repo.git
        revision: HEAD
        directories:
          - path: apps/*
          - path: apps/baz
            exclude: true
  template:
    metadata:
      name: '{{path.basename}}-app'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{{path}}'
""",
        }
    )

    applications, _findings = _discover(repo_root)

    assert {app.name for app in applications} == {"foo-app"}


def test_git_files_generator_reads_and_flattens_json_params(git_repo):
    repo_root = git_repo(
        {
            "clusters/prod.json": '{"cluster": {"name": "prod"}}',
            "bootstrap/appsets/files-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: files-appset
spec:
  generators:
    - git:
        repoURL: https://example.invalid/repo.git
        revision: HEAD
        files:
          - path: clusters/*.json
  template:
    metadata:
      name: 'app-{{cluster.name}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: 'manifests/{{cluster.name}}'
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert len(applications) == 1
    assert applications[0].name == "app-prod"
    assert applications[0].sources[0].path == "manifests/prod"


def test_git_files_generator_ignores_a_params_file_that_is_a_yaml_alias_bomb(git_repo):
    """A tiny "billion laughs" params file (each anchor aliases the
    previous one twice) must never reach `_flatten_params`' `str()` on
    the fully-expanded value: confirmed to hang for real before
    `fsutil.is_within_budget` existed."""
    layers = 30
    lines = ['a0: &a0 ["x"]']
    for i in range(1, layers):
        lines.append(f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]")
    lines.append("cluster:")
    lines.append("  name: prod")
    lines.append(f"  bomb: *a{layers - 1}")
    bomb_json = "\n".join(lines) + "\n"

    repo_root = git_repo(
        {
            "clusters/prod.json": bomb_json,
            "bootstrap/appsets/files-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: files-appset
spec:
  generators:
    - git:
        repoURL: https://example.invalid/repo.git
        revision: HEAD
        files:
          - path: clusters/*.json
  template:
    metadata:
      name: 'app-{{cluster.name}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: 'manifests/{{cluster.name}}'
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert applications == []  # the bomb file is treated as unparseable


def test_git_directories_generator_honors_a_pinned_revision(git_repo, git_tag, git_commit):
    """The generator's own `revision` is independent of the template's
    `targetRevision` (confirmed against the upstream Git generator docs):
    discovering from the checked-out working tree regardless of it
    would silently generate Applications for today's directory
    structure instead of the pinned one's."""
    repo_root = git_repo(
        {
            "apps/old/deployment.yaml": "kind: Deployment\n",
            "bootstrap/appsets/dirs-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: dirs-appset
spec:
  generators:
    - git:
        repoURL: https://example.invalid/repo.git
        revision: v1.0
        directories:
          - path: apps/*
  template:
    metadata:
      name: '{{path.basename}}-app'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{{path}}'
""",
        }
    )
    git_tag(repo_root, "v1.0")
    # HEAD now has `apps/new`, not `apps/old`: if the generator ignored
    # its own pinned `revision` and read the working tree, it would
    # generate `new-app` instead of the (correct) `old-app`.
    git_commit(
        repo_root,
        {"apps/old/deployment.yaml": None, "apps/new/deployment.yaml": "kind: Deployment\n"},
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert {app.name for app in applications} == {"old-app"}


def test_git_generator_with_unresolvable_revision_is_flagged_unverifiable(git_repo):
    repo_root = git_repo(
        {
            "apps/foo/deployment.yaml": "kind: Deployment\n",
            "bootstrap/appsets/dirs-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: dirs-appset
spec:
  generators:
    - git:
        repoURL: https://example.invalid/repo.git
        revision: does-not-exist-anywhere
        directories:
          - path: apps/*
  template:
    metadata:
      name: '{{path.basename}}-app'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{{path}}'
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert findings[0].rule_id == "unresolvable-generator"
    assert findings[0].severity == Severity.UNVERIFIABLE
    assert "does-not-exist-anywhere" in findings[0].message


def test_matrix_generator_combines_two_child_generators(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appsets/matrix-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: matrix-appset
spec:
  generators:
    - matrix:
        generators:
          - list:
              elements:
                - region: eu
          - list:
              elements:
                - env: dev
                - env: prod
  template:
    metadata:
      name: 'app-{{region}}-{{env}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/app
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert {app.name for app in applications} == {"app-eu-dev", "app-eu-prod"}


def test_matrix_generator_with_more_than_two_children_is_flagged_unresolvable(git_repo):
    """ArgoCD's real matrix generator only supports exactly 2 child
    generators and errors out on more (see DESIGN.md): this must not be
    silently treated as a 3-way cartesian product."""
    repo_root = git_repo(
        {
            "bootstrap/appsets/matrix-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: matrix-appset
spec:
  generators:
    - matrix:
        generators:
          - list:
              elements:
                - region: eu
          - list:
              elements:
                - env: dev
          - list:
              elements:
                - tier: web
  template:
    metadata:
      name: 'app-{{region}}-{{env}}-{{tier}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/app
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert findings[0].rule_id == "unresolvable-generator"
    assert "more than 2 child generators" in findings[0].message


def test_matrix_generator_over_the_combination_cap_is_flagged_instead_of_computed(git_repo):
    """Confirmed for real, not theoretical: two `list` generators of 5,000
    small elements each (individually well under the alias-bomb node
    budget, since that budget catches a densely *aliased* document, not a
    large but flat one) produced 25,000,000 combinations in ~7s for the
    combine step alone, before a single generated Application is even
    built or run through a rule. This must be flagged, not computed."""
    n = 200  # 200 x 200 = 40,000 > _MAX_MATRIX_COMBINATIONS (10,000)
    side_a = "\n".join(f"                - region: r{i}" for i in range(n))
    side_b = "\n".join(f"                - env: e{i}" for i in range(n))
    repo_root = git_repo(
        {
            "bootstrap/appsets/matrix-appset.yaml": f"""\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: matrix-appset
spec:
  generators:
    - matrix:
        generators:
          - list:
              elements:
{side_a}
          - list:
              elements:
{side_b}
  template:
    metadata:
      name: 'app-{{{{region}}}}-{{{{env}}}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/app
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert "40000 combinations" in findings[0].message
    assert "safety limit" in findings[0].message


def test_unresolvable_generator_produces_info_finding_and_no_applications(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appsets/clusters-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: clusters-appset
spec:
  generators:
    - clusters: {}
  template:
    metadata:
      name: '{{name}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/app
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO
    assert findings[0].application == "clusters-appset"
    assert "clusters" in findings[0].message
    assert "cluster/API access" in findings[0].message


def test_go_template_appset_is_flagged_and_skipped_entirely(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appsets/go-template-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: go-template-appset
spec:
  goTemplate: true
  generators:
    - list:
        elements:
          - env: dev
  template:
    metadata:
      name: 'myapp-{{.env}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/app
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert "goTemplate" in findings[0].message


def test_git_generator_on_external_repo_produces_info_finding(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appsets/external-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: external-appset
spec:
  generators:
    - git:
        repoURL: https://example.invalid/some-other-repo.git
        revision: HEAD
        directories:
          - path: apps/*
  template:
    metadata:
      name: '{{path.basename}}-app'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{{path}}'
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO
    assert "different repo" in findings[0].message


def test_list_generator_with_selector_is_flagged_unresolvable(git_repo):
    """A `selector` (label filter) changes which params ArgoCD keeps:
    evaluating it would require guessing at label matches, so it's
    flagged rather than expanded as if the selector weren't there."""
    repo_root = git_repo(
        {
            "bootstrap/appsets/list-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: list-appset
spec:
  generators:
    - list:
        elements:
          - env: dev
      selector:
        matchLabels:
          env: dev
  template:
    metadata:
      name: 'myapp-{{env}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/app
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO
    assert "selector" in findings[0].message


def test_matrix_child_generator_with_selector_is_flagged_unresolvable(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appsets/matrix-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: matrix-appset
spec:
  generators:
    - matrix:
        generators:
          - list:
              elements:
                - region: eu
            selector:
              matchLabels:
                region: eu
          - list:
              elements:
                - env: dev
  template:
    metadata:
      name: 'app-{{region}}-{{env}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/app
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert "selector" in findings[0].message


def test_merge_generator_overrides_matching_base_entry(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appsets/merge-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: merge-appset
spec:
  generators:
    - merge:
        mergeKeys:
          - server
        generators:
          - list:
              elements:
                - server: dev
                  path: manifests/base-dev
          - list:
              elements:
                - server: dev
                  path: manifests/override-dev
  template:
    metadata:
      name: 'app-{{server}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{{path}}'
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert len(applications) == 1
    assert applications[0].sources[0].path == "manifests/override-dev"


def test_merge_generator_keeps_unmatched_base_entry_unchanged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appsets/merge-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: merge-appset
spec:
  generators:
    - merge:
        mergeKeys:
          - server
        generators:
          - list:
              elements:
                - server: dev
                  path: manifests/dev
                - server: prod
                  path: manifests/prod
          - list:
              elements:
                - server: dev
                  path: manifests/override-dev
  template:
    metadata:
      name: 'app-{{server}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{{path}}'
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    paths = {app.name: app.sources[0].path for app in applications}
    assert paths == {"app-dev": "manifests/override-dev", "app-prod": "manifests/prod"}


def test_merge_generator_discards_non_matching_override_entry(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appsets/merge-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: merge-appset
spec:
  generators:
    - merge:
        mergeKeys:
          - server
        generators:
          - list:
              elements:
                - server: dev
                  path: manifests/dev
          - list:
              elements:
                - server: staging
                  path: manifests/staging
  template:
    metadata:
      name: 'app-{{server}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{{path}}'
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert {app.name for app in applications} == {"app-dev"}


def test_merge_generator_without_merge_keys_is_flagged_unresolvable(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appsets/merge-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: merge-appset
spec:
  generators:
    - merge:
        generators:
          - list:
              elements:
                - server: dev
  template:
    metadata:
      name: 'app-{{server}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/app
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert "mergeKeys" in findings[0].message


def test_merge_generator_with_unresolvable_base_produces_no_applications(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appsets/merge-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: merge-appset
spec:
  generators:
    - merge:
        mergeKeys:
          - server
        generators:
          - clusters: {}
          - list:
              elements:
                - server: dev
                  path: manifests/dev
  template:
    metadata:
      name: 'app-{{server}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{{path}}'
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert "clusters" in findings[0].message


def test_merge_generator_with_unresolvable_override_still_uses_base(git_repo):
    """The override generator being unresolvable doesn't hide the base
    entries: they're fully known, only the (flagged) override is not."""
    repo_root = git_repo(
        {
            "bootstrap/appsets/merge-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: merge-appset
spec:
  generators:
    - merge:
        mergeKeys:
          - server
        generators:
          - list:
              elements:
                - server: dev
                  path: manifests/dev
          - clusters: {}
  template:
    metadata:
      name: 'app-{{server}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{{path}}'
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert {app.name for app in applications} == {"app-dev"}
    assert len(findings) == 1
    assert "clusters" in findings[0].message


def test_matrix_generator_with_a_git_child_honors_its_pinned_revision(
    git_repo, git_tag, git_commit
):
    """Every existing matrix/merge test combines `list` generators only:
    nesting never exercised a `git` child's own revision handling
    (fixed in isolation for a bare `git` generator; this checks the fix
    actually reaches it through `_resolve_matrix`'s recursive dispatch,
    not just when `git` is the top-level generator)."""
    repo_root = git_repo(
        {
            "apps/old/deployment.yaml": "kind: Deployment\n",
            "bootstrap/appsets/matrix-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: matrix-appset
spec:
  generators:
    - matrix:
        generators:
          - git:
              repoURL: https://example.invalid/repo.git
              revision: v1.0
              directories:
                - path: apps/*
          - list:
              elements:
                - region: eu
                - region: us
  template:
    metadata:
      name: '{{path.basename}}-{{region}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: '{{path}}'
""",
        }
    )
    git_tag(repo_root, "v1.0")
    # HEAD now has `apps/new`, not `apps/old`: a matrix nested `git`
    # child reading the working tree instead of its own pinned
    # `revision` would generate `new-eu`/`new-us` instead.
    git_commit(
        repo_root,
        {"apps/old/deployment.yaml": None, "apps/new/deployment.yaml": "kind: Deployment\n"},
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert {app.name for app in applications} == {"old-eu", "old-us"}


def test_merge_generator_with_a_git_base_and_a_list_override(git_repo):
    """Every existing merge test uses `list` for both the base and the
    override: this checks merge-by-key still works when the base
    comes from a `git` generator's discovered params instead."""
    repo_root = git_repo(
        {
            "clusters/dev.json": '{"cluster": {"name": "dev"}}',
            "clusters/prod.json": '{"cluster": {"name": "prod"}}',
            "bootstrap/appsets/merge-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: merge-appset
spec:
  generators:
    - merge:
        mergeKeys:
          - cluster.name
        generators:
          - git:
              repoURL: https://example.invalid/repo.git
              revision: HEAD
              files:
                - path: clusters/*.json
          - list:
              elements:
                - cluster.name: prod
                  tier: critical
  template:
    metadata:
      name: 'app-{{cluster.name}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: 'manifests/{{cluster.name}}'
""",
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    by_name = {app.name: app for app in applications}
    assert set(by_name) == {"app-dev", "app-prod"}
    # The override only matches the "prod" base entry by mergeKeys.
    assert by_name["app-prod"].sources[0].path == "manifests/prod"
    assert by_name["app-dev"].sources[0].path == "manifests/dev"


def test_generated_application_is_checked_by_existing_rules(git_repo):
    """The whole point: a generated Application is a plain `Application`
    from the rules' point of view: no special-casing needed in
    phantom-target (or any other rule) to catch a real bug in it."""
    repo_root = git_repo(
        {
            "bootstrap/appsets/list-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: list-appset
spec:
  generators:
    - list:
        elements:
          - env: dev
  template:
    metadata:
      name: 'myapp-{{env}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: 'manifests/does-not-exist-{{env}}'
""",
        }
    )
    local_origin = get_origin_url(repo_root)
    applications, appset_findings = applicationset.discover(repo_root, local_origin, Severity.INFO)

    findings = appset_findings + PhantomTargetRule().check(
        applications, repo_root, Policy(), local_origin
    )

    assert len(findings) == 1
    assert findings[0].rule_id == "phantom-target"
    assert findings[0].application == "myapp-dev"
    assert "manifests/does-not-exist-dev" in findings[0].message
