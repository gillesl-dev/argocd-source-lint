# Changelog

All notable changes to this project are documented in this file, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.4.1] - 2026-11-04

### Fixed

- `orphan-source` reported files Git doesn't track, such as a gitignored local secret or a
  file not yet added. Only files in the Git index are checked now, so a local or pre-commit
  run gives the same result as CI.

### Changed

- `Development Status` is now `4 - Beta` on PyPI.
- The GitHub Action has a `v1` tag that follows the latest 1.x release.
- The project's own CI uses `actions/checkout@v7` and `astral-sh/setup-uv@v10.3.3`.

## [1.4.0] - 2026-11-04

### Added

- `repo_urls` setting: other URLs ArgoCD uses for this repository, such as an SSH host alias.
- `argocd_instances` setting: folders of separate ArgoCD instances, whose Application names
  and destinations are not compared with each other.
- `argocd-source-lint/ignore-<rule-id>: "<reason>"` annotation, to accept a finding for one
  Application or ApplicationSet template.
- `--list-rules`, `--only`, `--skip` and `--target-branch` options, and `--format github`.
- `docs/rules.md`, one section per rule, linked from SARIF reports.
- GitHub Action inputs `upload-sarif`, `category` and `args`.
- GitLab component inputs `args`, `image` and `job_name`.
- `new_git_file_globbing` setting, for an ApplicationSet controller started with
  `--enable-new-git-file-globbing`.

### Changed

- The GitHub Action and the GitLab component fail when no Application is found
  (`fail-on-empty`, `true` by default).
- The GitHub Action runs the tool in its own virtual environment, with
  `actions/setup-python@v6` and `github/codeql-action/upload-sarif@v4`.
- The GitHub Action's boolean inputs accept `True` and `TRUE`; any other value than true or
  false exits with code 2.
- On pull requests from forks and from Dependabot, a failed SARIF upload no longer fails the
  job.
- The GitLab component installs `git` only when the image lacks it.
- `orphan-source` reports as `info` the files an ApplicationSet it can't expand may deploy.
- `project-scope-violation` matches `sourceRepos` as ArgoCD does: an `https://` pattern no
  longer allows the `git@` URL of the same repository. A destination entry without
  `namespace` only allows an empty namespace, as in ArgoCD.
- The git `files` generator matches its patterns like `git ls-files`, ArgoCD's default, where
  `*` crosses `/`.
- Directory sources read `.json` files, and skip files marked `+argocd:skip-file-rendering`,
  as ArgoCD does.
- An Application tracking the target branch of a pull request is checked against the change,
  not against that branch.
- A branch missing locally is also looked up as `origin/<branch>`.
- `broken-values-ref` no longer checks remote `https://` values files.
- The table output is 200 columns wide when not on a terminal, unless `COLUMNS` is set.
- `--write-baseline` writes a reworded comment at the top of the file. Existing baselines
  load unchanged.
- `.githooks/` is removed. If you pointed `core.hooksPath` at it, run
  `git config --unset core.hooksPath`.

### Fixed

- `fail-on-error: True` in the GitHub Action passed the job on errors.
- With `fail-on-error: false`, a tool error (exit code 2) passed the GitHub job.
- The GitLab component failed on findings with `fail_on_error: false` and skipped the table.
- An invalid `.argocd-lint.yaml` or baseline crashed with exit code 1 instead of 2. This
  includes a `known_operators` `name_pattern` with a placeholder other than `{name}`, or
  `{user}` without `name_from_each`.
- An unknown rule ID or a `rules:` list in `.argocd-lint.yaml` was ignored or crashed.
- A manifest with a value of the wrong type, or an ApplicationSet template `spec` that is not
  a mapping, crashed the run.
- A YAML file with one invalid document was skipped as a whole without a notice.
- `phantom-target` and `broken-values-ref` ignored staged files in a pre-commit run.
- `phantom-target`: false finding for a path starting with `./` or with non-ASCII characters.
- `broken-values-ref`: false finding for globs, `./` and `../` paths, `/` paths, and
  ArgoCD build variables such as `$ARGOCD_APP_NAME`; `ignoreMissingValueFiles` was ignored
  for `$ref` entries.
- `orphan-source`: false finding for values files outside the chart, `plugin` sources, an
  explicit `directory` over a `kustomization.yaml`, braces inside `directory.include`, and
  files referenced by Kustomize `replacements`, `transformers`, `generators`, `validators`,
  `configurations`, `openapi` and `helmCharts`.
- ApplicationSets: `{{path}}` of the git `files` generator is the directory, as in ArgoCD;
  `{{path[n]}}`, `{{path.filename}}`, generator-level templates, matrix chaining,
  `list.elementsYaml` and `exclude` on `files` are supported; `directories` takes precedence
  over `files`; the `directories` generator no longer enters hidden folders, and `*` and `**`
  stay within one folder level.
- `double-coverage`: false finding for files renamed by a Kustomize `namePrefix`,
  `nameSuffix` or `namespace`.
- `project-scope-violation`: false finding for a negated destination field after the first
  one, and for `{a,b}` in a destination.
- `hpa-selfheal-conflict`: false finding for a workload without `spec.replicas` in Git.
- `malformed-ignore-diff-jq-expression`: false finding for valid expressions such as
  `(.a | .b)`, `getpath(...)` or `select(...) | .a`.
- `floating-target-revision` missed chart ranges such as `1.2.x`, `1.2 - 1.4` and `1.2 || 2.0`,
  and Git ranges such as `1.0.0 || 2.0.0`; it reported branch names with `=` or `<` in them
  and the pin `=1.2.3`.

## [1.3.1] - 2026-10-27

### Fixed

- The GitHub Action and the GitLab component installed the latest release from PyPI, whatever
  tag they were used at. Each tag now installs its own release (`@v1.3.1` runs 1.3.1). The
  Python package is unchanged.

## [1.3.0] - 2026-10-27

### Added

- `--fail-on-empty` exits with code 1 when no ArgoCD Application is found.
- `--show-info` lists `info` findings in the table output.

### Changed

- The table output counts `info` findings per rule instead of listing them, and counts a
  source in another repository once rather than once per rule. Other formats, baselines
  and exit codes are unchanged.
- When no Application is found, the table output prints a warning instead of "No issues
  detected"; other formats print it on stderr.
- The output says when `orphan-source` checked nothing because `scan_roots` is not set, and
  names any `scan_roots` entry that is not a directory.

### Security

- Control characters from the repository are shown as `\xNN` instead of being printed raw,
  so a name can't restyle or clear the terminal, and a JUnit report stays valid XML.

### Fixed

- `project-scope-violation` with several AppProjects of the same name checked against the
  last one read. It now reports `info`.
- `duplicate-application-name` no longer compares names the Kubernetes API would refuse,
  such as an unfilled `<CLUSTER_NAME>-app`.
- On Windows, reports written to stdout were encoded in the console code page instead of
  UTF-8.
- `double-coverage` flagged the same file deployed to different clusters or namespaces. It
  now compares each resource's cluster and namespace, as ArgoCD does.
- `duplicate-application-name` placed ApplicationSet-generated Applications in `argocd`
  instead of the ApplicationSet's namespace.

## [1.2.2] - 2026-10-26

### Added

- `examples/full-coverage`: one broken Application per rule.

## [1.2.1] - 2026-10-26

### Fixed

- The README's CI examples pinned `v1.1.1`. The Python package is unchanged.

## [1.2.0] - 2026-10-12

### Added

- `floating-target-revision` rule, for a `targetRevision` written as a version range
  (`1.2.*`, `>=1.2.0 <1.3.0`, `*`).

## [1.1.1] - 2026-10-08

### Fixed

- The README's CI examples pinned `v1.0.2`. The Python package is unchanged.

## [1.1.0] - 2026-10-08

### Added

- `argocd_version` setting, narrowing `unknown-sync-option` and `unknown-resource-hook` to
  the values that ArgoCD version recognizes.

### Fixed

- `Prune`/`Delete` were accepted as Application-level `syncOptions` keys on any version;
  they need ArgoCD 3.4.0.
- An invalid `.argocd-lint.yaml` exits with code 2 and a message per field instead of a
  traceback.

## [1.0.2] - 2026-09-23

### Changed

- Shorter README, DESIGN, CONTRIBUTING and SECURITY. No functional change.

## [1.0.1] - 2026-09-22

### Fixed

- The `action.yml` description fits GitHub Marketplace's 125-character limit. Not
  republished to PyPI.

## [1.0.0] - 2026-09-22

### Changed

- First stable release, with no functional change from `0.1.36`.

## [0.1.36] - 2026-09-22

### Security

- A tar archive rejected by PEP 706 leaves the revision `unverifiable` instead of crashing.
- Table cells are escaped, so repository content can't inject terminal markup.
- ApplicationSet `matrix` expansion is capped at 10,000 combinations.
- `.argocd-lint.yaml` and the baseline should be protected with `CODEOWNERS` when linting
  untrusted forks (documented).

## [0.1.35] - 2026-09-22

### Security

- Glob matching no longer uses a backtracking regex, which a crafted pattern could hang.

## [0.1.34] - 2026-09-22

### Security

- Source paths and Kustomize references can't read files outside the repository.

## [0.1.33] - 2026-09-22

### Security

- A Git revision beginning with `-` is rejected before Git runs, so it can't be read as an
  option such as `--remote=<url>`.

## [0.1.32] - 2026-09-22

### Added

- `malformed-ignore-diff-jq-expression` rule.

## [0.1.31] - 2026-09-22

### Fixed

- Parsed YAML files, covered files and Git tree listings are cached within a run.

## [0.1.30] - 2026-09-21

### Changed

- Internal refactor of the ApplicationSet generator resolvers.

## [0.1.29] - 2026-09-21

### Fixed

- The repository is parsed once for Applications, ApplicationSets and AppProjects.

## [0.1.28] - 2026-09-21

### Fixed

- Git revision lookups are cached within a run.

## [0.1.27] - 2026-09-21

### Fixed

- Discovery no longer loops on a directory symlink or Windows junction to an ancestor.

## [0.1.26] - 2026-09-21

### Fixed

- A YAML document whose aliases expand past 1,000,000 nodes is rejected.

## [0.1.25] - 2026-09-21

### Fixed

- The CLI exits with code 2 when `git` is missing.
- The GitLab template installs `git` for images such as `python:3.11-slim`.

## [0.1.24] - 2026-09-21

### Fixed

- ApplicationSet `git` generators now honor their own `revision` when resolving `directories` and
  `files`, using the same snapshot mechanism as Application `targetRevision`.
- Unresolvable revisions are reported as `unverifiable`.

## [0.1.23] - 2026-09-21

### Fixed

- SARIF now uses the stable `sarif-2.1.0` schema and includes `partialFingerprints`.
- JUnit testcase names are made unique when multiple findings share the same message.
- Corrected README guidance for PyPI, pre-commit, GitHub Actions, and GitLab CI.
- GitHub Actions documentation now includes checkout and SARIF upload permissions.
- Clarified that GitLab components do not need CI/CD Catalog publication.

### Added

- SARIF rules now include `fullDescription`, `helpUri`, and `defaultConfiguration.level`.

## [0.1.22] - 2026-09-21

### Fixed

- `directory.include`/`exclude` and `exclude_paths` now match case-sensitively on all platforms.

## [0.1.21] - 2026-09-21

### Fixed

- `broken-values-ref` now validates plain Helm `valueFiles` as well as cross-source `$ref` entries.

## [0.1.20] - 2026-09-21

### Added

- Added `malformed-sync-wave` for non-integer `argocd.argoproj.io/sync-wave` values.

## [0.1.19] - 2026-09-21

### Added

- Added Keycloak Operator and External Secrets Operator signatures to `known_operators`.

### Changed

- Renamed the GitLab CI placeholder to `<your-gitlab-group>`.

## [0.1.18] - 2026-09-21

### Added

- Added `unknown-resource-hook`.
- Added the Zalando Postgres Operator signature.

### Changed

- `unknown-sync-option` now also checks per-resource
  `argocd.argoproj.io/sync-options` annotations.

## [0.1.17] - 2026-09-21

### Added

- Added `unknown-sync-option` for unrecognized Application-level `syncOptions`.

## [0.1.16] - 2026-09-18

### Added

- Added `duplicate-application-name`.
- Added `malformed-ignore-diff-pointer` for invalid RFC 6901 JSON pointers.

## [0.1.15] - 2026-09-18

### Added

- Added `sync-validation-disabled` for `Validate=false`.

### Fixed

- Limited ApplicationSet `matrix` generators to 2 children, matching ArgoCD.

## [0.1.14] - 2026-09-18

### Added

- Added `hpa-selfheal-conflict` for HPA/ArgoCD `spec.replicas` conflicts without the required
  `ignoreDifferences` and `RespectIgnoreDifferences` configuration.

## [0.1.13] - 2026-09-18

### Added

- Added stale baseline entry reporting without affecting the exit code.
- Added the JUnit XML reporter (`--format junit`).

## [0.1.12] - 2026-09-18

### Added

- Added Elastic ECK, RabbitMQ Cluster Operator, and Strimzi signatures to `known_operators`.

## [0.1.11] - 2026-09-18

### Added

- Added `project-scope-violation` for Application sources or destinations outside their AppProject scope.

## [0.1.10] - 2026-09-18

### Added

- Added `revision-mismatch` (info) when a source's `targetRevision` differs from the checked-out revision.

### Fixed

- `orphan-source`, `missing-ignore-diff`, and `double-coverage` now inspect the source's own
  `targetRevision` instead of always using the working tree.

## [0.1.9] - 2026-09-18

### Added

- Added support for the ApplicationSet `merge` generator.

## [0.1.8] - 2026-09-15

### Fixed

- ApplicationSet generator-level `selector` filtering is now reported as `unresolvable-generator`
  instead of being ignored without notice.

## [0.1.7] - 2026-09-15

### Added

- Added `double-coverage` for files managed by more than one Application.

## [0.1.6] - 2026-09-15

Initial public release under the `gillesl-dev` GitHub/PyPI namespace.

### Added

- Core rules: `orphan-source`, `broken-values-ref`, `missing-ignore-diff`, `phantom-target`, and
  `unresolvable-generator`.

### Fixed

- The table reporter now displays a placeholder for `orphan-source` findings without an owning Application.
