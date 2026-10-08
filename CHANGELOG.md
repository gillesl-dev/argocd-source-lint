# Changelog

All notable changes to this project are documented in this file, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.1.1] - 2026-10-08

### Fixed

- README's GitHub Action/GitLab CI/pre-commit examples still pinned `v1.0.2`, one version
  behind what `v1.1.0` actually shipped. The Python package itself is unchanged.

## [1.1.0] - 2026-10-08

### Added

- Added the optional `argocd_version` setting, narrowing `unknown-sync-option` and
  `unknown-resource-hook` to the sync options/hooks actually available on that ArgoCD
  version instead of every value ArgoCD has ever recognized. Every introduction version
  was traced to the real `argoproj/argo-cd` commit that shipped it, not estimated — see
  DESIGN.md. Omitting the setting keeps the previous, version-blind behavior.

### Fixed

- `Prune`/`Delete` were treated as always-valid Application-level `syncOptions` keys.
  They only became valid there in ArgoCD 3.4.0 (confirmed against the introducing code
  commit, not just docs) — before that, only the per-resource annotation form worked.
  Declaring an older `argocd_version` now catches this as a real silent no-op.
- An invalid `.argocd-lint.yaml` (e.g. a malformed `argocd_version`) now exits with a
  clear, field-by-field message and exit code 2 instead of a raw Python traceback.

## [1.0.2] - 2026-09-23

### Changed

- Rewrote README, DESIGN, CONTRIBUTING and SECURITY for tone and length. No
  functional change. Published so the condensed README is what PyPI/TestPyPI
  actually display — an already-published version's description can't be
  edited in place, only a new version's can.

## [1.0.1] - 2026-09-22

### Fixed

- Shortened the `action.yml` description to comply with GitHub Marketplace's 125-character limit.
  The Python package was unchanged and was not republished to PyPI.

## [1.0.0] - 2026-09-22

### Changed

- Public v1 release on PyPI and GitHub Marketplace, with no functional change from `0.1.36`.
- PyPI status changed from `Development Status :: 3 - Alpha` to `5 - Production/Stable`.

## [0.1.36] - 2026-09-22

### Security

- `materialize_revision` now handles hostile tar archives rejected by PEP 706 instead of crashing the CLI.
- Escaped repo-controlled values before rendering them with `rich`, preventing terminal markup injection.
- Capped ApplicationSet `matrix` expansion at 10,000 combinations. Two `list` generators with
  5,000 elements each previously produced 25,000,000 combinations in ~7s before any generated
  Application was built.
- Documented that `.argocd-lint.yaml` and `.argocd-lint-baseline.yaml` should be protected with
  `CODEOWNERS`/branch protection when analyzing untrusted forks.
- Audit also confirmed safe YAML loading, escaped structured reporters, and no known dependency
  vulnerabilities in the resolved environment.

## [0.1.35] - 2026-09-22

### Security

- Replaced the regex-based glob matcher with a dynamic-programming implementation to eliminate a
  repo-controlled ReDoS path. A pattern shaped like `*a*a*a...*a!` (~40 repetitions) against a
  non-matching string of `a`s could hang indefinitely. Matching is now
  `O(len(pattern) x len(candidate))`.

## [0.1.34] - 2026-09-22

### Security

- Prevented source paths such as `../outside-secret` from escaping the repository root. A crafted
  Application could make coverage rules crash and document-based rules read files reachable outside
  the checkout.
- Applied the same containment checks to Kustomize
  `resources`/`bases`/`components`/`crds`/`patches*`/generator references, which had an independent
  traversal path through tracked `kustomization.yaml` content.
- All resolved paths are now checked with `Path.is_relative_to` before they are read or traversed.

## [0.1.33] - 2026-09-22

### Security

- Reject Git revisions beginning with `-` before invoking Git. A repo-controlled value such as
  `--remote=https://<host>/x` was interpreted by `git archive` as an option rather than a revision,
  causing a real outbound connection attempt and exposing an SSRF path from the CI job.
- Rejected values use the existing `unverifiable` handling.

## [0.1.32] - 2026-09-22

### Added

- Added `malformed-ignore-diff-jq-expression` for invalid `ignoreDifferences.jqPathExpressions`.

### Fixed

- Added tests ensuring `DEFAULT_RULE_SEVERITIES` and SARIF rule metadata stay synchronized with the
  registered rule set.

## [0.1.31] - 2026-09-22

### Fixed

- Cached YAML document loading and covered-source discovery to avoid repeated parsing across rules.
  In the profiling case used for this fix, 40 Applications with 15 covered files each cost ~4.9s
  across only three rules before caching.
- Cached Git tree listings per `(repo_root, revision)` for `phantom-target` and `broken-values-ref`.
- Added a test ensuring every `Rule` subclass is registered in `cli.RULES`.

### Changed

- Promoted shared `$ref` parsing helpers to public internal helpers. No behavior change.

## [0.1.30] - 2026-09-21

### Changed

- Replaced repeated ApplicationSet generator resolver parameters with a shared `GeneratorContext`.
  Internal refactor only.

## [0.1.29] - 2026-09-21

### Fixed

- Repository YAML discovery now runs once and is shared by manifest, ApplicationSet, and AppProject
  discovery instead of walking and parsing the repository three times.

## [0.1.28] - 2026-09-21

### Fixed

- Cached Git revision resolution per `(repo_root, revision)`. A benchmark with 50 Applications sharing
  `HEAD` dropped from 702 `git rev-parse` calls to 1, reducing the measured Windows run from ~58s to ~5s.

## [0.1.27] - 2026-09-21

### Fixed

- Added a cycle-safe shared filesystem walker to prevent infinite traversal through directory
  symlinks/junctions and avoid double-counting the same physical directory.

## [0.1.26] - 2026-09-21

### Fixed

- Added an expanded-document size budget for YAML alias structures, preventing "billion laughs"-style
  inputs from hanging downstream processing.

## [0.1.25] - 2026-09-21

### Fixed

- The CLI now fails clearly when `git` is missing instead of producing misleading findings.
- GitLab CI installs `git` in `before_script` for images such as `python:3.11-slim`.

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
  instead of being silently ignored.

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
