from __future__ import annotations

from pathlib import Path

from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.missing_ignore_diff import MissingIgnoreDiffRule


def _run_missing_ignore_diff(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    return MissingIgnoreDiffRule().check(applications, repo_root, policy, local_origin)


def test_good_repo_has_no_findings(fixture_repo):
    repo_root = fixture_repo("good_repo")

    findings = _run_missing_ignore_diff(repo_root)

    assert findings == []


def test_bad_fixture_flags_both_expected_secrets(fixture_repo):
    repo_root = fixture_repo("bad_missing_ignore_diff")

    findings = _run_missing_ignore_diff(repo_root)

    assert len(findings) == 2
    assert all(f.rule_id == "missing-ignore-diff" for f in findings)
    assert all(f.severity == Severity.WARNING for f in findings)  # never error, it's heuristic
    messages = {f.message for f in findings}
    assert any("pg-cluster-app" in m for m in messages)
    assert any("pg-cluster-ca" in m for m in messages)
    assert all(f.line == 13 for f in findings)  # the `selfHeal: true` line


def test_pinned_target_revision_is_checked_against_that_revision_not_head(
    git_repo, git_tag, git_commit
):
    """Removing the risky manifest from HEAD must not hide the finding for an
    Application still pinned to a revision where it is live (see DESIGN.md
    "targetRevision drift")."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/pg-prod-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: pg-prod
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: v1.0
    path: manifests/postgres
  syncPolicy:
    automated:
      selfHeal: true
""",
            "manifests/postgres/cluster.yaml": """\
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-prod
""",
        }
    )
    git_tag(repo_root, "v1.0")
    # HEAD moves on: an unrelated refactor removes the Cluster manifest
    # from manifests/postgres/, but pg-prod (a stable prod app) still
    # targets v1.0, unchanged.
    git_commit(
        repo_root,
        {
            "manifests/postgres/cluster.yaml": None,
            "manifests/postgres/README.md": "moved elsewhere\n",
        },
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert len(findings) == 2
    assert all(f.rule_id == "missing-ignore-diff" for f in findings)
    messages = {f.message for f in findings}
    assert any("pg-prod-app" in m for m in messages)
    assert any("pg-prod-ca" in m for m in messages)


def test_matching_ignore_diff_by_exact_name_suppresses_finding(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/pg-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: pg-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/pg
  syncPolicy:
    automated:
      selfHeal: true
  ignoreDifferences:
    - kind: Secret
      name: pg-cluster-app
    - kind: Secret
      name: pg-cluster-ca
""",
            "infrastructure/pg/cluster.yaml": """\
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-cluster
  namespace: postgres-prod
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert findings == []


def test_nameless_ignore_diff_covers_broadly(git_repo):
    """An `ignoreDifferences` entry with no `name` (a common real-world
    pattern) covers every instance of that `kind`, not just one specific
    name."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/pg-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: pg-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/pg
  syncPolicy:
    automated:
      selfHeal: true
  ignoreDifferences:
    - kind: Secret
      namespace: postgres-prod
""",
            "infrastructure/pg/cluster.yaml": """\
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-cluster
  namespace: postgres-prod
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert findings == []


def test_ignore_diff_with_mismatched_namespace_does_not_suppress_finding(git_repo):
    """Otherwise an `ignoreDifferences` entry meant for an unrelated Secret with the
    same name in another namespace would mask the finding."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/pg-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: pg-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/pg
  syncPolicy:
    automated:
      selfHeal: true
  ignoreDifferences:
    - kind: Secret
      name: pg-cluster-app
      namespace: unrelated-namespace
""",
            "infrastructure/pg/cluster.yaml": """\
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-cluster
  namespace: postgres-prod
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    messages = {f.message for f in findings}
    assert any("pg-cluster-app" in m for m in messages)


def test_self_heal_disabled_skips_the_check_entirely(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/pg-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: pg-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/pg
""",
            "infrastructure/pg/cluster.yaml": """\
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-cluster
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert findings == []


def test_cert_manager_signature_resolves_name_from_spec_secret_name(git_repo):
    """`name_from` is not limited to `metadata.name`: cert-manager derives the
    Secret name from `spec.secretName`."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/tls-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: tls-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/tls
  syncPolicy:
    automated:
      selfHeal: true
""",
            "infrastructure/tls/certificate.yaml": """\
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: some-cert
spec:
  secretName: some-cert-tls
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert len(findings) == 1
    assert "some-cert-tls" in findings[0].message


def test_external_source_without_self_heal_produces_no_finding_at_all(git_repo):
    """Without selfHeal the risk cannot occur, so the rule says nothing about an
    external source, not even an `info` "out of scope"."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/external-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: external-app
spec:
  source:
    repoURL: https://example.invalid/some-other-repo.git
    targetRevision: HEAD
    path: never/checked
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert findings == []


def test_multi_repo_application_is_flagged_info_and_not_checked(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/external-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: external-app
spec:
  source:
    repoURL: https://example.invalid/some-other-repo.git
    targetRevision: HEAD
    path: never/checked
  syncPolicy:
    automated:
      selfHeal: true
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO
    assert findings[0].application == "external-app"


def test_rule_severity_is_configurable_via_policy(fixture_repo):
    repo_root = fixture_repo("bad_missing_ignore_diff")
    (repo_root / ".argocd-lint.yaml").write_text(
        "rules:\n  missing-ignore-diff: error\n",
        encoding="utf-8",
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert len(findings) == 2
    assert all(f.severity == Severity.ERROR for f in findings)


def test_custom_known_operator_from_policy_is_merged_with_builtin_pack(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": """\
known_operators:
  - crd_trigger: acme.internal/WidgetCluster
    name_from: metadata.name
    expect_ignore_on:
      - kind: Secret
        name_pattern: "{name}-credentials"
""",
            "bootstrap/argocd-apps/widget-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: widget-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/widget
  syncPolicy:
    automated:
      selfHeal: true
""",
            "infrastructure/widget/cluster.yaml": """\
apiVersion: acme.internal/v1
kind: WidgetCluster
metadata:
  name: my-widget
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert len(findings) == 1
    assert "my-widget-credentials" in findings[0].message


def test_elastic_eck_cluster_flags_the_elastic_user_secret(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/logging-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: logging-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/logging
  syncPolicy:
    automated:
      selfHeal: true
""",
            "infrastructure/logging/es.yaml": """\
apiVersion: elasticsearch.k8s.elastic.co/v1
kind: Elasticsearch
metadata:
  name: quickstart
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert len(findings) == 1
    assert findings[0].rule_id == "missing-ignore-diff"
    assert "quickstart-es-elastic-user" in findings[0].message


def test_rabbitmq_cluster_flags_the_default_user_secret(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/mq-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: mq-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/mq
  syncPolicy:
    automated:
      selfHeal: true
""",
            "infrastructure/mq/cluster.yaml": """\
apiVersion: rabbitmq.com/v1beta1
kind: RabbitmqCluster
metadata:
  name: sample
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert len(findings) == 1
    assert "sample-default-user" in findings[0].message


def test_strimzi_kafka_user_flags_the_credentials_secret(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/kafka-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: kafka-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/kafka
  syncPolicy:
    automated:
      selfHeal: true
""",
            "infrastructure/kafka/user.yaml": """\
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaUser
metadata:
  name: bob
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert len(findings) == 1
    assert findings[0].message.endswith(
        "`Secret` `bob` — ArgoCD risks resetting this out-of-Git-managed field on every sync."
    )


def test_zalando_postgresql_flags_one_secret_per_user(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/pg-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: pg-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/postgres
  syncPolicy:
    automated:
      selfHeal: true
""",
            "infrastructure/postgres/cluster.yaml": """\
apiVersion: acid.zalan.do/v1
kind: postgresql
metadata:
  name: acid-minimal-cluster
spec:
  users:
    zalando: []
    foo_user: []
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert len(findings) == 2
    messages = {f.message for f in findings}
    assert any("zalando.acid-minimal-cluster.credentials" in m for m in messages)
    assert any("foo_user.acid-minimal-cluster.credentials" in m for m in messages)
    assert all("user `zalando`" in m or "user `foo_user`" in m for m in messages)


def test_zalando_postgresql_with_matching_ignore_diff_per_user_suppresses_finding(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/pg-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: pg-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/postgres
  syncPolicy:
    automated:
      selfHeal: true
  ignoreDifferences:
    - kind: Secret
      name: zalando.acid-minimal-cluster.credentials.postgresql.acid.zalan.do
""",
            "infrastructure/postgres/cluster.yaml": """\
apiVersion: acid.zalan.do/v1
kind: postgresql
metadata:
  name: acid-minimal-cluster
spec:
  users:
    zalando: []
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert findings == []


def test_zalando_postgresql_without_users_mapping_produces_no_finding(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/pg-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: pg-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/postgres
  syncPolicy:
    automated:
      selfHeal: true
""",
            "infrastructure/postgres/cluster.yaml": """\
apiVersion: acid.zalan.do/v1
kind: postgresql
metadata:
  name: acid-minimal-cluster
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert findings == []


def test_keycloak_operator_flags_the_initial_admin_secret(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/kc-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: kc-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/keycloak
  syncPolicy:
    automated:
      selfHeal: true
""",
            "infrastructure/keycloak/instance.yaml": """\
apiVersion: k8s.keycloak.org/v2alpha1
kind: Keycloak
metadata:
  name: example-kc
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert len(findings) == 1
    assert "example-kc-initial-admin" in findings[0].message


def test_external_secret_flags_its_default_target_secret(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/es-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: es-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: infrastructure/secrets
  syncPolicy:
    automated:
      selfHeal: true
""",
            "infrastructure/secrets/db-password.yaml": """\
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: db-password
""",
        }
    )

    findings = _run_missing_ignore_diff(repo_root)

    assert len(findings) == 1
    assert findings[0].message.endswith(
        "`Secret` `db-password` — ArgoCD risks resetting this out-of-Git-managed "
        "field on every sync."
    )
