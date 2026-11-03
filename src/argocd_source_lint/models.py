from __future__ import annotations

from enum import Enum
from pathlib import Path
from string import Formatter

from pydantic import BaseModel, Field, model_validator


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    UNVERIFIABLE = "unverifiable"


class IgnoreDiffRule(BaseModel):
    group: str | None = None
    kind: str
    name: str | None = None
    namespace: str | None = None
    json_pointers: list[str] = Field(default_factory=list)
    jq_path_expressions: list[str] = Field(default_factory=list)
    managed_fields_managers: list[str] = Field(default_factory=list)


class Source(BaseModel):
    repo_url: str
    target_revision: str
    path: str | None = None
    chart: str | None = None
    ref: str | None = None
    helm_value_files: list[str] = Field(default_factory=list)
    # Line of each `helm_value_files` entry, None when unknown.
    helm_value_files_lines: list[int | None] = Field(default_factory=list)
    # When true, Argo CD skips missing valueFiles instead of failing, so
    # broken-values-ref has nothing to check.
    helm_ignore_missing_value_files: bool = False
    # `spec.source(s)[].directory`, for plain manifest directories only.
    # Defaults match Argo CD: no recursion, no filter.
    directory_recurse: bool = False
    directory_include: str | None = None
    directory_exclude: str | None = None
    # "directory", "helm", "kustomize" or "plugin" when the source sets that
    # field, which overrides detection from the files (`ExplicitType` in Argo CD).
    explicit_type: str | None = None
    # 1-indexed start line of this source's mapping, None when unknown.
    line: int | None = None


class ExpectedIgnoreDiff(BaseModel):
    kind: str
    name_pattern: str


class KnownOperatorSignature(BaseModel):
    """An operator signature for missing-ignore-diff.

    Maps a trigger CRD to the derived resources that need `ignoreDifferences`.
    """

    crd_trigger: str
    name_from: str
    # Dotted path to a mapping, such as a Zalando `postgresql` resource's
    # `spec.users`. `expect_ignore_on` is then evaluated once per key, bound to
    # `{user}` (see DESIGN.md).
    name_from_each: str | None = None
    expect_ignore_on: list[ExpectedIgnoreDiff]

    @model_validator(mode="after")
    def _check_placeholders(self) -> KnownOperatorSignature:
        allowed = {"name", "user"} if self.name_from_each else {"name"}
        for expected in self.expect_ignore_on:
            try:
                fields = {field for _, field, _, _ in Formatter().parse(expected.name_pattern)}
            except ValueError as exc:
                raise ValueError(f"name_pattern {expected.name_pattern!r}: {exc}") from None
            unknown = sorted(f"{{{f}}}" for f in fields - allowed if f is not None)
            if unknown:
                raise ValueError(
                    f"name_pattern {expected.name_pattern!r} uses {', '.join(unknown)}; "
                    "only {name}, and {user} with name_from_each, are available"
                )
        return self


class Application(BaseModel):
    name: str
    namespace: str
    sources: list[Source]
    sync_policy_self_heal: bool = False
    # Line of the `selfHeal` key when it is true, None when unknown.
    self_heal_line: int | None = None
    ignore_differences: list[IgnoreDiffRule] = Field(default_factory=list)
    # Line of the `ignoreDifferences` key. Individual entry lines are not tracked.
    ignore_differences_line: int | None = None
    # `spec.syncPolicy.syncOptions` verbatim, such as "Validate=false".
    sync_options: list[str] = Field(default_factory=list)
    sync_options_line: int | None = None
    source_file: Path
    # `spec.project` defaults to "default", as in Argo CD.
    project: str = "default"
    project_line: int | None = None
    destination_server: str | None = None
    destination_name: str | None = None
    destination_namespace: str | None = None
    # From `argocd-source-lint/ignore-<rule-id>` annotations: rule id to reason.
    ignored_rules: dict[str, str] = Field(default_factory=dict)


class AppProjectDestination(BaseModel):
    server: str | None = None
    name: str | None = None
    namespace: str | None = None


class AppProject(BaseModel):
    """The source and destination scope an `AppProject` grants its Applications."""

    name: str
    source_repos: list[str] = Field(default_factory=list)
    destinations: list[AppProjectDestination] = Field(default_factory=list)
    source_file: Path


class Finding(BaseModel):
    rule_id: str
    severity: Severity
    application: str
    message: str
    file: Path
    line: int | None = None
    # Lets the table report an external source once instead of once per rule.
    # Excluded from `model_dump` so machine-readable reports do not include it.
    external_source: bool = Field(default=False, exclude=True)
    # For an ApplicationSet that is not fully expanded: the folders its
    # Applications may deploy from ("" for anywhere). Not reported either.
    unexpanded_paths: tuple[str, ...] = Field(default=(), exclude=True)
