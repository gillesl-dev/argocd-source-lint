from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from ruamel.yaml import YAML

from argocd_source_lint.argocd_versions import parse_version
from argocd_source_lint.models import KnownOperatorSignature, Severity

DEFAULT_RULE_SEVERITIES: dict[str, Severity] = {
    "orphan-source": Severity.ERROR,
    "broken-values-ref": Severity.ERROR,
    "missing-ignore-diff": Severity.WARNING,
    "phantom-target": Severity.ERROR,
    "unresolvable-generator": Severity.INFO,
    "double-coverage": Severity.ERROR,
    "revision-mismatch": Severity.INFO,
    "project-scope-violation": Severity.ERROR,
    "hpa-selfheal-conflict": Severity.WARNING,
    "sync-validation-disabled": Severity.INFO,
    "duplicate-application-name": Severity.ERROR,
    "malformed-ignore-diff-pointer": Severity.WARNING,
    "malformed-ignore-diff-jq-expression": Severity.WARNING,
    "unknown-sync-option": Severity.WARNING,
    "unknown-resource-hook": Severity.WARNING,
    "malformed-sync-wave": Severity.WARNING,
    "floating-target-revision": Severity.WARNING,
}


# One line per rule, shared by --list-rules and the SARIF report.
RULE_DESCRIPTIONS: dict[str, str] = {
    "orphan-source": "A YAML file under scan_roots that no Application source covers.",
    "broken-values-ref": (
        "A Helm valueFiles entry, including $ref, that points to a file that doesn't exist."
    ),
    "missing-ignore-diff": (
        "A known operator resource under selfHeal without the ignoreDifferences its "
        "generated objects need, so ArgoCD keeps resetting them."
    ),
    "phantom-target": "A source path that doesn't exist at its targetRevision.",
    "unresolvable-generator": (
        "An ApplicationSet generator that needs live cluster or API access, or Go "
        "templating, so its Applications can't be checked from the checkout."
    ),
    "double-coverage": (
        "The same resource deployed by two Applications to the same cluster and namespace."
    ),
    "revision-mismatch": (
        "A source's targetRevision points to another commit than the checked-out one; "
        "it is checked against a snapshot of that revision."
    ),
    "project-scope-violation": (
        "An Application source or destination outside the sourceRepos or destinations "
        "allowed by its AppProject."
    ),
    "hpa-selfheal-conflict": (
        "An HPA and selfHeal both manage spec.replicas without ignoreDifferences and "
        "RespectIgnoreDifferences, so ArgoCD resets the replica count on every sync."
    ),
    "sync-validation-disabled": (
        "The Validate=false sync option, which lets an invalid manifest be applied."
    ),
    "duplicate-application-name": (
        "Two or more Applications with the same namespace and name, which ArgoCD "
        "treats as one object."
    ),
    "malformed-ignore-diff-pointer": (
        "An ignoreDifferences jsonPointers entry that doesn't start with / (RFC 6901), "
        "so the field is not ignored."
    ),
    "malformed-ignore-diff-jq-expression": (
        "An ignoreDifferences jqPathExpressions entry written as a JSON pointer or a path "
        "without its leading dot. One invalid expression disables the whole "
        "ignoreDifferences list of the Application."
    ),
    "unknown-sync-option": (
        "A sync option, at Application level or in the sync-options annotation, that "
        "ArgoCD doesn't recognize and ignores without an error."
    ),
    "unknown-resource-hook": (
        "A hook or hook-delete-policy annotation value that ArgoCD doesn't recognize. A "
        "resource whose only hook value is unknown is never applied."
    ),
    "malformed-sync-wave": (
        "A sync-wave annotation value that isn't an integer; ArgoCD falls back to wave 0."
    ),
    "floating-target-revision": (
        "A targetRevision written as a version range, resolved again at every sync, so "
        "the deployed content can change with no commit in the repository."
    ),
}

RULES_DOC_URL = "https://github.com/gillesl-dev/argocd-source-lint/blob/main/docs/rules.md"


def rule_docs_url(rule_id: str) -> str:
    return f"{RULES_DOC_URL}#{rule_id}"


def argocd_instance(source_file: Path, instances: list[str]) -> str:
    """The `argocd_instances` folder an Application manifest belongs to, or ""."""
    path = source_file.as_posix()
    matching = [f for f in instances if path.startswith(f.strip("/") + "/")]
    return max(matching, key=len, default="")


class Policy(BaseModel):
    scan_roots: list[str] = Field(default_factory=list)
    rules: dict[str, Severity] = Field(default_factory=lambda: dict(DEFAULT_RULE_SEVERITIES))
    unverifiable_blocks_ci: bool = True
    exclude_paths: list[str] = Field(default_factory=list)
    # Other URLs ArgoCD uses for this repository than the `origin` remote.
    repo_urls: list[str] = Field(default_factory=list)
    # Folders that each hold the Applications of a separate Argo CD instance.
    argocd_instances: list[str] = Field(default_factory=list)
    # Whether the ApplicationSet controller runs with
    # `--enable-new-git-file-globbing`, which changes how git `files` patterns match.
    new_git_file_globbing: bool = False
    # Added to the built-in `rules/known-operators.yaml` pack, never replacing it.
    known_operators: list[KnownOperatorSignature] = Field(default_factory=list)
    # Restricts unknown-sync-option and unknown-resource-hook to values this
    # Argo CD version recognizes. Unset, any value from any version is accepted.
    argocd_version: str | None = None

    @field_validator("argocd_version")
    @classmethod
    def _validate_argocd_version(cls, value: str | None) -> str | None:
        if value is not None:
            parse_version(value)  # pydantic reports its ValueError as a field error
        return value


def load_policy(repo_root: Path, filename: str = ".argocd-lint.yaml") -> Policy:
    """Load `.argocd-lint.yaml` from the repo root over the defaults.

    `rules` is merged key by key. Other fields present in the file replace the default.
    """
    policy_file = repo_root / filename
    if not policy_file.exists():
        return Policy()

    yaml = YAML(typ="safe")
    with policy_file.open("r", encoding="utf-8") as f:
        raw = yaml.load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError("expected a mapping of settings at the top level")

    merged = Policy().model_dump()
    for key in (
        "scan_roots",
        "unverifiable_blocks_ci",
        "exclude_paths",
        "repo_urls",
        "argocd_instances",
        "new_git_file_globbing",
        "known_operators",
        "argocd_version",
    ):
        if key in raw:
            merged[key] = raw[key]
    if "rules" in raw:
        rules = raw["rules"] or {}
        if not isinstance(rules, dict):
            raise ValueError("`rules` must map rule IDs to severities")
        unknown = sorted(set(rules) - set(DEFAULT_RULE_SEVERITIES))
        if unknown:
            # A misspelled rule ID would otherwise leave that rule at its default.
            raise ValueError(f"unknown rule(s) in `rules`: {', '.join(map(str, unknown))}")
        merged["rules"] = {**merged["rules"], **rules}

    return Policy.model_validate(merged)
