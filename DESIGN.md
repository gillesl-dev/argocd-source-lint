# Design notes

Background for the non-obvious decisions referenced by the code. If a comment says
"see DESIGN.md", this is where the reasoning lives.

## Mono-repo v1 scope

The tool verifies `Application` sources whose `repoURL` matches the checkout detected
with `git remote get-url origin`. That keeps v1 local: filesystem and Git access only,
with no cluster credentials or network fetches.

External sources are still reported as `info`. Sources are classified independently
through `git_context.local_path_sources`/`external_path_sources`, so mixed
`spec.sources` entries still get full checks for their local parts.

External-repository cloning and caching by `repoURL` + `targetRevision` is a future
extension.

## Requiring `git` on PATH, loudly

Rules depend on Git for revision lookups, tree listings and snapshots. A missing binary
used to look like a repository with no `origin`, causing local sources to be
misclassified as external and producing misleading coverage findings.

This was reproduced with `python:3.11-slim`. The CLI now checks
`git_context.is_git_available()` at startup and exits with code `2` if Git is missing.
`templates/lint.yml` installs Git as well.

## `directory.recurse`/`include`/`exclude` semantics

Coverage in `coverage.py` follows ArgoCD's directory behavior:

- `directory.recurse` defaults to `false`;
- `directory.include`/`exclude` uses case-sensitive glob matching through
  `fnmatch.fnmatchcase`;
- `.argocd-lint.yaml` `exclude_paths` uses the same case-sensitive behavior;
- directories containing `Chart.yaml` are opaque because Helm rendering is outside v1;
- Kustomize directories are parsed for local resources, bases, components, CRDs,
  patches and generator file references.

Remote Kustomize references are skipped because they cannot be verified from the
checkout.

## SARIF vs GitLab Code Quality

SARIF and GitLab Code Quality are different schemas. GitHub code scanning consumes
SARIF; GitLab's widget consumes a CodeClimate-derived format. Separate reporters keep
each output aligned with the platform that reads it.

## Keeping the reporters aligned with each format's real-world conventions

SARIF now uses the stable `sarif-2.1.0` schema, includes `partialFingerprints`, and
shares `stable_fingerprint` with GitLab Code Quality. The fingerprint is based on rule,
file, Application and message. SARIF metadata also includes `fullDescription`,
`helpUri` and `defaultConfiguration.level`.

JUnit needed a different fix: GitLab drops duplicate testcase names after the first.
`junit.py` now appends `(#N)` to repeated names so every finding remains visible.

GitLab Code Quality itself already matched the fields GitLab processes:
`description`, `check_name`, `fingerprint`, `severity`, `location.path`, and
`location.lines.begin`.

## The JUnit reporter's severity mapping

JUnit has no warning/info level. Each finding becomes one testcase:

- `error` and `unverifiable` become `<failure>`;
- `warning` and `info` become `<skipped>`.

This mirrors the tool's default blocking semantics while keeping non-blocking findings
visibly distinct. The reporter does not consult `unverifiable_blocks_ci`: severity-to-JUnit
mapping stays fixed, like the SARIF and GitLab Code Quality reporters, even when policy changes
whether `unverifiable` blocks the CLI.

## The `unverifiable` severity

A shallow checkout may not contain a referenced branch or tag. In that case the tool
must distinguish "target missing" from "cannot verify with this checkout."

`unverifiable` blocks by default (`unverifiable_blocks_ci: true`) so an incomplete CI
checkout does not look like a clean result.

## The baseline file

`.argocd-lint-baseline.yaml` supports adoption on repositories with known legacy
findings. `--write-baseline` records `rule_id`, `file`, `application` and `message`;
later runs suppress exact matches while keeping new findings visible.

The file stays human-readable instead of reusing reporter hashes. Stale entries are
reported but do not fail CI.

## YAML alias bombs

A densely aliased YAML document can be tiny on disk but expand exponentially when
walked or stringified. A 724-byte params file reproduced this and hung the linter.

`is_within_budget` rejects documents whose fully expanded size exceeds 1,000,000 nodes.
The calculation is memoized by object identity, so the guard itself stays cheap.
ApplicationSet params files apply the same budget explicitly.

## A directory symlink/junction cycle

A directory link back to an ancestor can make recursive discovery run forever. This was
reproduced on Windows with a junction: `Path.is_symlink()` returns `False` for that case, and
`os.walk(..., followlinks=False)` still follows it because the junction is not reported as a
regular symlink.

`fsutil.walk_tree` resolves each directory once and never descends into the same
physical directory twice.

## Caching `git rev-parse` for the life of one CLI run

With 50 Applications all using `targetRevision: HEAD`, 702 of 754 Git subprocesses were
identical `git rev-parse` calls, costing about 58 seconds on Windows.

`git_context._resolve_commit` now caches by `(repo_root, revision)`, reducing those
702 calls to 1 and the measured run to about 5 seconds. `clear_caches` resets state at
the start of each CLI invocation.

## Sharing one discovery pass across Application/ApplicationSet/AppProject

Manifest, ApplicationSet and AppProject discovery used to walk and parse the repository
separately. With 2,000 plain manifests, the three passes cost about 5 seconds.

`fsutil.discover_documents` now performs one cached pass per `repo_root`, and callers
filter the shared results by `kind`.

## `GeneratorContext`

ApplicationSet resolver functions repeatedly passed the same five values:
`repo_root`, `local_origin`, `appset_name`, `source_file` and `severity`.

A frozen, slotted `GeneratorContext` dataclass now bundles them. It is internal and has
no behavioral effect.

## Caching an Application's own covered content

Several rules re-read the same covered files. In one profile, 40 Applications with
15 covered files each cost about 4.9 seconds across only three rules.

`fsutil.load_yaml_documents` now caches by resolved file path, and
`coverage.covered_files_for_source` caches by the source directory plus its directory
options. Both are reset by `clear_caches`.

## Batching `git ls-tree` by revision, not by path

`phantom-target` used to run one `git ls-tree` per Application path. With 40 distinct
paths sharing one revision, subprocess startup alone cost about 2.3 seconds.

`tree_paths_at_revision` now lists the full tree once per revision, and
`path_has_tracked_files` answers path checks from memory.

## Guarding against a rule nobody wired up

`cli.RULES` is explicit, but a new `Rule` subclass could previously be unit-tested and
still never be registered in the CLI.

`test_every_rule_class_is_registered_in_cli_rules` imports every rule module and checks
that discovered subclasses match `cli.RULES`.

## A `targetRevision` can turn into a live network call

Revisions come from repository-controlled YAML. A value beginning with `-` is parsed by
Git as an option. This was reproduced with:

```text
--remote=https://<host>/x
```

Passed to `git archive`, it caused a real outbound connection attempt and created an
SSRF path from the CI job.

`git_context._is_safe_revision` rejects `-`-prefixed revisions before any Git
subprocess. `test_flag_like_revision_is_rejected_without_ever_calling_git` verifies
that `subprocess.run` is never reached.

## A Kustomize overlay can read outside the repo

A source path such as `../outside-secret` could escape `base_root`.
`covered_documents_for_application` could then read YAML from outside the checkout.

Kustomize references had the same issue one level deeper. The fix threads `base_root`
through the resolution chain and checks every resolved path with
`Path.is_relative_to` before stat, traversal or read. Legitimate `../` references that
remain inside the repository still work.

## `match_glob`'s regex translation was a ReDoS

The old glob matcher translated patterns into a backtracking regex. A pattern shaped
like `*a*a*a...*a!` with roughly 40 repetitions against a non-matching string of `a`s
hung indefinitely.

`match_glob` now tokenizes literals, `*`, `**` and `?` and uses dynamic programming,
giving `O(len(pattern) x len(candidate))` behavior. The regression test uses the
original hanging input.

## The follow-up audit: three more, on request

A second security pass found three additional issues.

**Hostile tar streams.** PEP 706 `filter="data"` correctly rejects `../` archive
entries, but the resulting `tarfile.TarError` was not caught. Rejection now leaves the
revision unverifiable instead of crashing the CLI.

**Rich markup injection.** Repo-controlled table fields were interpreted as Rich
markup. A crafted Application name such as `[link=https://evil.example]click[/link]`
rendered as a clickable link. Repo-controlled cells are now escaped with
`rich.markup.escape()`. The severity cell remains unescaped because it is built only from
the tool's closed `Severity` enum and internal style map, never repository content.

**Unbounded matrix expansion.** Two `list` generators with 5,000 elements each produced
25,000,000 combinations in about 7 seconds. `_MAX_MATRIX_COMBINATIONS = 10_000` is now
checked before building the Cartesian product.

The same audit confirmed safe YAML tag handling, automatic XML escaping, JSON reporter
escaping, and no dependency issue from `pip-audit` at that point in time.

`.argocd-lint.yaml` and `.argocd-lint-baseline.yaml` remain repository-controlled
policy files. Repositories evaluating untrusted forks should protect them with
`CODEOWNERS`/branch protection.

## `Finding.line`

`ruamel.yaml` round-trip mode preserves `.lc` metadata used for source and field
locations. Findings point to the most useful block: source mapping, matching
`valueFiles` entry, or `selfHeal: true`.

`orphan-source` deliberately has no line because the whole file is the finding.

## ApplicationSet generators

ApplicationSets are expanded into ordinary `Application` objects and run through the
same rules.

Supported locally:

- `list`;
- `git.directories` and `git.files`;
- `matrix`, limited to two children and 10,000 combinations;
- `merge`, using `mergeKeys` to overlay matching base entries.

A `merge` without `mergeKeys` is `unresolvable-generator`. If the base generator is
unresolvable, there are no known keys to merge and the result cannot be resolved; if a later
child is unresolvable, the known base entries remain valid and only that child's overrides are
missing. Generators needing external state (`clusters`, `scmProvider`, `pullRequest`, `plugin`,
external Git repositories, `goTemplate: true`) are also reported as
`unresolvable-generator`. Generator-level `selector` filtering is not approximated.

Generated Applications have `Finding.line = None`; `Finding.file` points to the
ApplicationSet.

## `double-coverage`

`double-coverage` reports a file owned by more than one different Application. Two
sources inside the same Application are not flagged because they still belong to one
reconciliation loop.

## `targetRevision` drift

Coverage rules must inspect the source at its declared `targetRevision`. Otherwise an
Application pinned to an older tag could contain manifests no longer present on `main`
and the linter would miss them.

When the revision differs from the checkout, `materialize_revision` extracts a full
`git archive` snapshot. Full extraction is necessary because Kustomize references may
reach outside the source's own `path`.

`covered_files_for_application` returns repo-relative identities; content-reading rules
use `covered_documents_for_application` so they read from the correct working tree or
snapshot.

Missing revisions remain `unverifiable`. `revision_mismatch` separately reports a
source pinned away from the checkout as `info`.

ApplicationSet Git generators use the same snapshot mechanism. The three-state result
of `revision_matches_checkout` must preserve "unknown" separately from "different" so
an unfetched revision is not mistaken for HEAD. In particular, `None is False` evaluates
to `False` in Python, so a naive `if result is False` branch does not catch the unknown
case and can silently fall through as if the revision matched the checkout.

## `project-scope-violation`

This rule compares an Application with its `AppProject` using repository data only.

ArgoCD applies different matching semantics:

- `sourceRepos`: bare `*` matches everything; other patterns use segment-aware matching
  after repository URL normalization;
- destinations use unbounded, case-sensitive glob matching;
- `destination.name` cannot be safely compared with `destination.server` without live
  cluster data, so mismatched forms produce `info`;
- negated project entries are reported as `info` instead of reimplementing ArgoCD's
  non-trivial negation behavior.

Missing `"default"` is treated as ArgoCD's permissive built-in project. A missing named
project is reported as `info`.

## `hpa-selfheal-conflict`

An HPA and ArgoCD self-heal can fight over `spec.replicas`. `ignoreDifferences` alone
is not enough: `RespectIgnoreDifferences=true` is also required during sync.

The rule resolves the target from `scaleTargetRef`, so Helm-rendered workloads remain
detectable. A small kind-to-group map handles common workloads when `apiVersion` is
missing; unknown custom scalable resources are skipped.

## `sync-validation-disabled`

`Validate=false` disables Kubernetes API schema validation during sync. It can be
intentional, but it can also hide an invalid manifest.

The rule is `info` by default because the option is explicit.

## `duplicate-application-name`

ArgoCD identifies an Application by `(namespace, name)`. Two manifests using the same
pair can overwrite or fight each other.

The rule runs after plain and ApplicationSet-generated Applications are combined.
Namespace defaults to `"argocd"` when omitted.

## `malformed-ignore-diff-pointer`

`ignoreDifferences[].jsonPointers` follows
[RFC 6901](https://www.rfc-editor.org/rfc/rfc6901): pointers must begin with `/`.
A value such as `spec.replicas` doesn't match the intended field.

The rule checks only this objective syntax requirement.

## `malformed-ignore-diff-jq-expression`

A common mistake is to paste a JSON-pointer-style value such as `/spec/replicas` into
`jqPathExpressions`; valid jq expressions begin with `.`.

The rule checks that narrow syntax property without trying to reproduce jq evaluation.
ArgoCD's `NewIgnoreNormalizer` aborts construction of the entire
`ignoreDifferences` normalizer on a parse failure, so one malformed expression can
invalidate otherwise valid entries.

## `unknown-sync-option`

ArgoCD reads `syncOptions` as literal `Key=Value` strings. A misspelled or incorrectly
cased key is simply ignored.

The Application-level key set comes from the official sync-options documentation.
Only keys are validated because accepted values are not documented consistently enough
to avoid false positives.

Per-resource `argocd.argoproj.io/sync-options` annotations use their own supported key
set.

## `unknown-resource-hook`

`argocd.argoproj.io/hook` and `argocd.argoproj.io/hook-delete-policy` are unvalidated
annotation strings, so misspellings can silently fall through.

Accepted hook values are `PreSync`, `Sync`, `Skip`, `PostSync`, `SyncFail`,
`PreDelete` and `PostDelete`; delete policies are `HookSucceeded`, `HookFailed` and
`BeforeHookCreation`.

This rule was treated with lower confidence during prioritization because ArgoCD documents the
accepted values clearly, but the exact runtime behavior of an unknown hook value is less explicit
than the behavior of an unknown sync option.

## ArgoCD version awareness (`argocd_versions.py`)

Both rules above originally treated their closed sets as always valid, regardless of
which ArgoCD version a user's cluster actually runs. That's wrong in one concrete,
verified direction: a value can be correctly spelled and still be a silent no-op simply
because it's newer than the user's ArgoCD. `Prune`/`Delete` as Application-level
`syncOptions` keys are the clearest case -- they only work from ArgoCD 3.4.0 onward; on
any older version ArgoCD silently ignores them there (they only existed as the
per-resource annotation before that).

`.argocd-lint.yaml`'s optional `argocd_version` setting addresses this: when declared,
`argocd_versions.is_known` narrows each table to the subset actually available as of
that version. Left unset, both rules keep accepting every value ArgoCD has ever
recognized -- the original, version-blind behavior, unchanged.

**Sourcing method, exactly, because this is the kind of claim this project refuses to
estimate:** a full clone of `argoproj/argo-cd`, then for each of the 24 values `git log
--pickaxe-regex -S'\bValue\b'` against `docs/user-guide/sync-options.md` (sync option
keys) or the hook-docs file's full rename chain (`docs/resoure_hooks.md` →
`docs/resource_hooks.md` → `docs/user-guide/resource_hooks.md` →
`docs/user-guide/sync-waves.md`, hook/delete-policy values) to find the commit that
first introduced that exact string, then `git tag --contains <commit>` filtered to
`vX.Y.Z` tags, sorted, to find the earliest release containing it. Plain-word
pickaxe search was unusable for short keys (`Prune` is a substring of
`PrunePropagationPolicy`); `--pickaxe-regex` with a `\b`-bounded pattern fixed that.
`git tag --contains --merged origin/master` returned nothing useful because this repo's
tag history is dominated by release-branch cherry-picks, not a linear path to master —
plain `git tag --contains` (no `--merged`) is what actually works here.

Two values needed the commit itself inspected, not just the docs: `Prune` and `Delete`
were valid as the per-resource annotation since 1.1.0/2.7.0, but the Application-level
form is a distinct, later code change (`argoproj/argo-cd#23370`,
"feat: add Prune and Delete as application level sync option", first in `v3.4.0`) --
confirmed by reading the diff, not inferred from the docs commit alone.
`ClientSideApplyMigration` is a rename, not a new feature: the capability shipped in
`v3.1.0` as `DisableClientSideApplyMigration=true`, renamed to its current spelling in
`v3.3.0`. `CreateNamespace`'s code shipped in `v1.7.0` but wasn't documented in
`sync-options.md` until `v2.5.0` -- the table uses the real (earlier) code date, not the
docs date, in that one case; `Replace`'s per-resource form has the same kind of lag and
is handled the same way.

This table is accurate as of the research date (2026-10-08) and is a manual snapshot,
not something CI re-verifies against upstream on every run -- the same caveat that
already applies to the rest of this tool's closed vocabularies (see `unknown-sync-option`
above).

## `malformed-sync-wave`

ArgoCD parses `argocd.argoproj.io/sync-wave` with Go's `strconv.Atoi`. Invalid values
fall back to wave `0`.

The rule checks whether the annotation is a valid signed integer.

## `broken-values-ref`'s plain `valueFiles` check

`broken-values-ref` checks both cross-source `$ref/path.yaml` entries and plain Helm
`valueFiles`.

Plain entries resolve relative to the source chart path and use the same revision-aware
Git tree checks. `helm.ignoreMissingValueFiles: true` disables the check for that
source, matching ArgoCD behavior.

## Extension points

- `loader.ApplicationDiscovery` allows a future rendered discovery implementation to
  feed the existing rule/reporting pipeline.
- `rules/known-operators.yaml` keeps `missing-ignore-diff` signatures data-driven;
  `name_from_each` covers one-resource-per-entry operators.
- Explicitly configured resource names are not encoded as derived-name signatures.
- Flux `Kustomization`/`HelmRelease` support is a natural future adapter.
