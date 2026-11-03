# Design notes

Background for the non-obvious decisions in the code.

## Single-repository scope

The linter verifies `Application` sources whose `repoURL` matches the checkout detected
with `git remote get-url origin`, or listed in `repo_urls`. This keeps the linter local:
filesystem and Git access only, with no cluster credentials or network fetches.

External sources are reported as `info`. Sources are classified independently through
`git_context.local_path_sources`/`external_path_sources`, so the local entries of a
mixed `spec.sources` list still get full checks.

Cloning and caching external repositories by `repoURL` + `targetRevision` is a possible
future extension.

## Git is required

Rules depend on Git for revision lookups, tree listings and snapshots. Without it, every
repository would look like one with no `origin`, and every source would be classified as
external. The CLI checks `git_context.is_git_available()` at startup and exits with code
`2` if Git is missing. Slim images such as `python:3.11-slim` don't ship it, so
`templates/lint.yml` installs it when needed.

## Directory sources

Coverage in `coverage.py` follows ArgoCD's directory behavior:

- `directory.recurse` defaults to `false`;
- `directory.include`/`exclude` uses case-sensitive glob matching, with `{a,b}`
  alternatives anywhere in the pattern, as gobwas/glob does;
- `.yaml`, `.yml` and `.json` files are read; `.jsonnet` is evaluated by ArgoCD and is
  not;
- a file containing `+argocd:skip-file-rendering` is skipped, as ArgoCD does, and is not
  reported by `orphan-source` either;
- `.argocd-lint.yaml` `exclude_paths` uses the same case-sensitive matching.

A source that sets `directory`, `helm`, `kustomize` or `plugin` gets that type, whatever
the folder contains (`ApplicationSource.ExplicitType`). ArgoCD first drops a block that
sets none of its fields (`NormalizeSource`): `helm: {}` or `directory: {recurse: false}`
leave the type to detection, while a `directory` block with only `include` or `exclude`
keeps it.

Without a type, a folder with a `kustomization.yaml` is a Kustomize source, and a folder
with a `Chart.yaml` is opaque: Helm is not rendered, so its whole subtree counts as
covered. A `plugin` source is opaque the same way, since the plugin may read any file of
its folder.

Brace alternatives are expanded up to 256 patterns. A larger pattern comes from a
repository file and would grow exponentially, so it is not evaluated, and the file it
applies to is kept rather than reported.

## Kustomize and Helm

Kustomize overlays are not rendered, but a resource they compose is still a committed
YAML file. Content-based rules such as `missing-ignore-diff` and `hpa-selfheal-conflict`
read every file under an Application's covered directory, so they see a
Kustomize-managed resource like any other file.

`orphan-source` follows every local file a `kustomization.yaml` names: `resources`,
`bases`, `components`, `crds`, `patches`, `configurations`, `generators`, `transformers`,
`validators`, `replacements`, `openapi`, the `configMapGenerator`/`secretGenerator`
inputs, and `helmCharts` values files and charts under `chartHome`. Remote references are
skipped because they can't be verified from the checkout.

A Helm chart is different: a resource a chart template produces at `helm template` time
never exists as a file, so no rule can read it. A rendered discovery behind the same rule
pipeline would close that gap.

## Reporters

SARIF and GitLab Code Quality are different schemas: GitHub code scanning reads SARIF,
GitLab's widget reads a format derived from CodeClimate. Separate reporters keep each
output aligned with the platform that consumes it.

SARIF uses the `sarif-2.1.0` schema and includes `partialFingerprints`. It shares
`stable_fingerprint` with GitLab Code Quality, computed from rule, file, Application and
message. SARIF rule metadata also includes `fullDescription`, `helpUri` and
`defaultConfiguration.level`. The GitLab reporter emits the fields GitLab processes:
`description`, `check_name`, `fingerprint`, `severity`, `location.path` and
`location.lines.begin`.

JUnit has no warning or info level. `error` and `unverifiable` become `<failure>`,
`warning` and `info` become `<skipped>`. GitLab drops testcases whose name repeats an
earlier one, so `junit.py` appends `(#N)` to repeated names. Like the SARIF and GitLab
reporters, the mapping doesn't depend on `unverifiable_blocks_ci`.

## The `unverifiable` severity

A shallow checkout may not contain a referenced branch or tag. The linter must then
tell "target missing" apart from "cannot verify with this checkout".

`unverifiable` blocks by default (`unverifiable_blocks_ci: true`) so an incomplete CI
checkout does not pass as a clean result.

A revision is looked up as written first, then as `refs/remotes/origin/<revision>`. A CI
checkout of a pull request is detached, so a branch such as `main` usually exists only as
`origin/main`, which is also the branch ArgoCD reads. A local branch of the same name
takes precedence.

In a pull request, the question is what the change will do once merged. The branch the
change goes into, given by `--target-branch` or read from `GITHUB_BASE_REF` /
`CI_MERGE_REQUEST_TARGET_BRANCH_NAME`, therefore resolves to the checkout itself.
Otherwise an Application tracking `main` would be checked against `main` as it is, which
fails a pull request that adds an Application with its manifests and passes one that
breaks it.

## The baseline file

`.argocd-lint-baseline.yaml` helps adoption on repositories with known findings.
`--write-baseline` records `rule_id`, `file`, `application` and `message`. Later runs
suppress exact matches and keep new findings visible. The file stays human-readable
instead of reusing reporter hashes. Stale entries are reported but do not fail CI.

Because entries match the message, finding messages are not reworded within a major
version.

## Untrusted repository content

Everything the linter reads comes from the repository, which may be a fork's pull
request:

- A densely aliased YAML document can be tiny on disk and expand exponentially when
  walked. `is_within_budget` rejects documents whose expanded size exceeds 1,000,000
  nodes, memoized by object identity. ApplicationSet params files go through it too.
- A directory link back to an ancestor would make discovery loop. On Windows a junction
  is not reported as a symlink, so `os.walk(followlinks=False)` follows it.
  `fsutil.walk_tree` resolves each directory once and never descends into the same
  physical directory twice.
- Git parses a revision beginning with `-` as an option, and `--remote=<url>` passed to
  `git archive` would make a network connection from CI. `git_context._is_safe_revision`
  rejects such revisions before any Git subprocess runs.
- A source path or Kustomize reference such as `../outside` could read files outside the
  checkout. `base_root` is threaded through the resolution chain, and every resolved path
  is checked with `Path.is_relative_to` before any read.
- `git archive` snapshots are extracted with PEP 706 `filter="data"`, and a rejected
  archive leaves the revision `unverifiable` instead of crashing.
- Table cells are escaped with `rich.markup.escape()`, so an Application name can't
  inject terminal links or styles.
- Glob patterns are matched by dynamic programming in `match_glob`, `O(len(pattern) x
  len(candidate))`, not by a backtracking regex. Brace expansion is capped (see
  Directory sources), and a matrix generator is not expanded past 10,000 combinations.

`.argocd-lint.yaml` and `.argocd-lint-baseline.yaml` are repository-controlled policy
files. Repositories that lint untrusted forks should protect them with `CODEOWNERS` or
branch protection.

## Caching within one run

Git subprocesses dominate run time, especially on Windows. Within one CLI run:

- `git_context._resolve_commit` caches by `(repo_root, revision)`;
- `tree_paths_at_revision` lists the tree once per revision, and
  `path_has_tracked_files` answers path checks from it;
- `fsutil.discover_documents` parses the repository once for Applications,
  ApplicationSets and AppProjects;
- `fsutil.load_yaml_documents` caches by file, and `coverage.covered_files_for_source`
  by source directory and directory options.

`clear_caches` resets all of them at the start of each run, since `HEAD` can move between
runs in the same process.

## Rule registration

`cli.RULES` is an explicit list. A test imports every rule module and checks that the
`Rule` subclasses match it, so a new rule can't be tested and left out of the CLI.

## `Finding.line`

`ruamel.yaml` round-trip mode preserves the `.lc` metadata used for source and field
locations. Findings point to the most useful block: the source mapping, the matching
`valueFiles` entry, or `selfHeal: true`. `orphan-source` has no line because the whole
file is the finding.

## ApplicationSet generators

ApplicationSets are expanded into ordinary `Application` objects and run through the
same rules. Supported locally:

- `list`, including `elementsYaml`;
- `git.directories`, matched with Go's `path.Match` as in ArgoCD (`*` and `**` stay in
  one folder level), skipping hidden folders and everything under them;
- `git.files`, matched like `git ls-files` (`*` crosses `/`), or like doublestar when
  `new_git_file_globbing` is set to mirror the controller's
  `--enable-new-git-file-globbing`; `exclude: true` entries are removed, and `directories`
  takes precedence over `files`;
- `matrix`, limited to two children and 10,000 combinations, with the second child able
  to use the first one's parameters;
- `merge`, using `mergeKeys` to overlay matching base entries;
- a generator's own `template`, merged over the spec's.

A `merge` without `mergeKeys` is `unresolvable-generator`. If the base generator is
unresolvable, there are no known keys to merge on. If a later child is unresolvable, the
base entries stay valid and only that child's overrides are missing.

Generators that need external state are reported as `unresolvable-generator`: `clusters`,
`scmProvider`, `pullRequest`, `plugin`, external Git repositories and
`goTemplate: true`, as are `templatePatch` and generator-level `selector`. The files the
missing Applications may deploy are taken from the template's source paths, up to their
first placeholder, or from the git generators' paths, and `orphan-source` reports them as
`info` instead of `error`.

Generated Applications have `Finding.line = None`, and `Finding.file` points to the
ApplicationSet. Their namespace is always the ApplicationSet's own, whatever the template
says (`GenerateApplications` in `applicationset/controllers/template/template.go`).

## `double-coverage`

`double-coverage` reports a resource that more than one Application deploys to the same
place. Two sources inside the same Application are not flagged because they belong to
one reconciliation loop.

The same file deployed to two clusters or two namespaces is two distinct resources, as in
an ApplicationSet with one base and one Application per environment. A resource's target
is computed the way ArgoCD computes it:

- The cluster is `destination.server` or `destination.name`. The two forms are never
  compared with each other, because mapping a name to a URL needs live cluster data,
  `in-cluster` and `https://kubernetes.default.svc` included: the local cluster can be
  renamed through its own secret.
- The namespace is the resource's own `metadata.namespace` when set, otherwise
  `destination.namespace`.
- Cluster-scoped kinds are compared by cluster only. The list is limited to kinds the
  Kubernetes docs state are not namespaced: `Namespace`, `Node`, `PersistentVolume`,
  `StorageClass`, `ClusterRole`, `ClusterRoleBinding`, `PriorityClass` and
  `CustomResourceDefinition`. Any other kind, CRDs included, is treated as namespaced.

When the target can't be determined, the resource is skipped: a missing destination, a
value that still holds an unresolved `{{...}}`, or a URL compared with a name. Files of a
source whose `kustomization.yaml` sets `namePrefix`, `nameSuffix` or `namespace` are
skipped too, since the rendered names differ. A missed conflict is preferred over a
blocking false positive.

## `targetRevision` drift

Coverage rules inspect each source at its declared `targetRevision`. Otherwise an
Application pinned to an older tag could contain manifests no longer present on `main`,
and the linter would miss them.

When the revision differs from the checkout, `materialize_revision` extracts a full
`git archive` snapshot, since Kustomize references may reach outside the source's own
`path`. `covered_files_for_application` returns repo-relative identities, and
content-reading rules use `covered_documents_for_application` to read from the right
working tree or snapshot. ApplicationSet Git generators use the same snapshots.

`revision_matches_checkout` returns `True`, `False` or `None` for a revision that doesn't
resolve. The unknown case must never be read as "matches HEAD": `None is False` is
`False` in Python, so callers test for `None` explicitly.

## `project-scope-violation`

This rule compares an Application with its `AppProject` using repository data only, with
ArgoCD's matching:

- `sourceRepos`: bare `*` matches everything; other patterns are matched by `/` segment
  after `NormalizeGitURL`, which keeps the scheme, so an `https://` pattern doesn't allow
  the `git@` URL of the same repository;
- destinations use unbounded, case-sensitive glob matching with `{a,b}` alternatives; an
  entry without `namespace` only allows an empty namespace;
- `destination.name` can't be compared with `destination.server` without live cluster
  data, so mismatched forms produce `info`;
- negated project entries are reported as `info` instead of reimplementing ArgoCD's
  negation rules.

A missing `default` project is treated as ArgoCD's permissive built-in project. A
missing named project is reported as `info`. Several AppProjects with the same name in
the repository, for example one per folder in a template repository, are reported as
`info`, and the Application is not checked, since the repository alone doesn't say which
one ArgoCD loads.

## `hpa-selfheal-conflict`

An HPA and ArgoCD self-heal can fight over `spec.replicas`. `ignoreDifferences` alone is
not enough: `RespectIgnoreDifferences=true` is also required during sync. A workload that
sets no `spec.replicas` in Git is not reported, as the Kubernetes HPA documentation
recommends that setup.

The rule resolves the target from `scaleTargetRef`, so Helm-rendered workloads remain
detectable. A small kind-to-group map handles common workloads when `apiVersion` is
missing. Unknown custom scalable resources are skipped.

## `sync-validation-disabled`

`Validate=false` disables Kubernetes API schema validation during sync. It can be
intentional, so the rule is `info` by default.

## `duplicate-application-name`

ArgoCD identifies an Application by `(namespace, name)`, and the namespace defaults to
`argocd`. The rule runs after plain and ApplicationSet-generated Applications are
combined. Two ApplicationSets in different namespaces that generate the same name are
not duplicates, since generated Applications get their ApplicationSet's namespace.

A name the Kubernetes API server would refuse is not compared. A custom resource's name
is validated with `NameIsDNSSubdomain` (lowercase alphanumerics, `-` and `.`, at most 253
characters), so a template's unfilled `<CLUSTER_NAME>-app` or an unresolved `{{env}}`
names an Application that can never exist.

## `malformed-ignore-diff-pointer`

`ignoreDifferences[].jsonPointers` follows
[RFC 6901](https://www.rfc-editor.org/rfc/rfc6901): pointers must begin with `/`. A value
such as `spec.replicas` does not match the intended field. The rule checks only this.

## `malformed-ignore-diff-jq-expression`

ArgoCD compiles each `jqPathExpressions` entry as `del(<expression>)` with gojq, and
`NewIgnoreNormalizer` fails as a whole when one entry doesn't compile, so one bad entry
disables the other `ignoreDifferences` entries too.

Any jq path expression is valid, `(.a | .b)`, `getpath(...)` and `select(...) | .a`
included. The rule flags only an entry that starts with `/`, a JSON pointer pasted into
the wrong field, or with a name followed by `.` or `[` (`spec.replicas`, which jq reads as
a call to an undefined `spec` function). It does not parse jq.

## `unknown-sync-option`

ArgoCD reads `syncOptions` as literal `Key=Value` strings, so a misspelled or
incorrectly cased key is ignored. The Application-level key set comes from the
sync-options documentation; per-resource `argocd.argoproj.io/sync-options` annotations
have their own set. Only keys are validated, because accepted values are not documented
consistently enough to avoid false positives.

## `unknown-resource-hook`

`argocd.argoproj.io/hook` and `argocd.argoproj.io/hook-delete-policy` are free-form
annotation strings. Accepted hook values are `PreSync`, `Sync`, `Skip`, `PostSync`,
`SyncFail`, `PreDelete` and `PostDelete`; accepted delete policies are `HookSucceeded`,
`HookFailed` and `BeforeHookCreation`.

gitops-engine drops a hook value it doesn't know (`hook.Types`), but `hook.IsHook` stays
true as long as the annotation is present, so `syncPhases` returns no phase: a resource
whose only hook value is unknown is neither applied nor run.

## ArgoCD version awareness (`argocd_versions.py`)

A correctly spelled sync option or hook can still be a no-op when it is newer than the
cluster's ArgoCD. `Prune`/`Delete` as Application-level `syncOptions` keys are the
clearest case: they work from ArgoCD 3.4.0, and older versions ignore them at that level,
where only the per-resource annotation existed.

When `argocd_version` is set in `.argocd-lint.yaml`, `argocd_versions.is_known` narrows
each table to the values available in that version. Unset, both rules accept every value
ArgoCD has recognized in any version.

Each value's first release is the earliest `vX.Y.Z` tag containing the commit that
introduced it in the sync-options or hook docs, or in the code where the docs lagged:

- `Prune` and `Delete` have been valid as the per-resource annotation since
  1.1.0/2.7.0. The Application-level form is a separate code change
  (`argoproj/argo-cd#23370`, first in `v3.4.0`).
- `ClientSideApplyMigration` is a rename. The capability shipped in `v3.1.0` as
  `DisableClientSideApplyMigration=true` and took its current spelling in `v3.3.0`.
- `CreateNamespace` shipped in code in `v1.7.0` but was not documented in
  `sync-options.md` until `v2.5.0`. The table uses the code version. `Replace`'s
  per-resource form has the same lag and is handled the same way.

The tables reflect ArgoCD as of 2026-10-08. CI does not re-check them against upstream.

## `malformed-sync-wave`

ArgoCD parses `argocd.argoproj.io/sync-wave` with Go's `strconv.Atoi`. An invalid value
falls back to the `helm.sh/hook-weight` value when there is one, otherwise to wave `0`.
The rule checks that the annotation is a signed integer.

## `broken-values-ref`

The rule checks cross-source `$ref/path.yaml` entries and plain Helm `valueFiles`. Plain
entries resolve relative to the source path, or to the repository root when they start
with `/`, at the source's revision. ArgoCD's build variables (`$ARGOCD_APP_NAME` and the
others `newEnv` sets in `reposerver/repository/repository.go`) are substituted first; an
entry using the resolved commit (`$ARGOCD_APP_REVISION`) is not checked.
`helm.ignoreMissingValueFiles: true` disables the check for that source, as in ArgoCD.

## `floating-target-revision`

ArgoCD resolves a `targetRevision` that is a Masterminds semver constraint, and not an
exact version, to the highest matching chart version or Git tag (`MaxVersion` in
`util/versions/tags.go`). It does this for Git sources too, before falling back to a
branch of that name. The same Application can then deploy different content tomorrow
with no commit in this repository.

The rule parses the constraint the way `IsConstraint` does: `*`, `1.2.*`, `~1.2`, `^1.2`,
`>=1.2.0 <1.3.0`, `1.2 - 1.4` and `a || b` are ranges; `1.2.3`, `v1.2` and `=1.2.3` are
pins; a branch name such as `main`, `feature/a=b` or `x64-build` is neither, since
`parseConstraint` only accepts digits or a single `x`, `X` or `*` as a version part.
`1.x` is reported only for charts, since a Git branch can have that name. Terms need a
separator, as in Masterminds. Whitespace runs and the spaces around commas are collapsed
before matching, so each separator is one character. A value over 200 characters is not
treated as a range.

## `missing-ignore-diff`'s operator signatures

A `known-operators.yaml` entry produces a warning that tells the user to add an
`ignoreDifferences` rule for one exact Secret name. If `name_pattern` is wrong, the user
adds a rule that covers nothing, and the derived Secret stays exposed to a self-heal
conflict. A signature is therefore added only once the operator's own documentation
states the derived name directly.

## Extension points

- `rules/known-operators.yaml` keeps `missing-ignore-diff` signatures data-driven.
  `name_from_each` covers one-resource-per-entry operators.
- Explicitly configured resource names are not encoded as derived-name signatures.
- Flux `Kustomization`/`HelmRelease` support would fit as another discovery feeding the
  same rules.
