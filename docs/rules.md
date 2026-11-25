# Rules

Each section is named after the rule ID that appears in reports.
`argocd-source-lint --list-rules` prints the same list with default severities. Severities
can be changed per rule in `.argocd-lint.yaml`. The reasoning behind each rule, with its
ArgoCD sources, is in [DESIGN.md](../DESIGN.md).

- [Suppressing a finding](#suppressing-a-finding)
- [orphan-source](#orphan-source)
- [broken-values-ref](#broken-values-ref)
- [missing-ignore-diff](#missing-ignore-diff)
- [phantom-target](#phantom-target)
- [unresolvable-generator](#unresolvable-generator)
- [double-coverage](#double-coverage)
- [revision-mismatch](#revision-mismatch)
- [project-scope-violation](#project-scope-violation)
- [hpa-selfheal-conflict](#hpa-selfheal-conflict)
- [sync-validation-disabled](#sync-validation-disabled)
- [duplicate-application-name](#duplicate-application-name)
- [malformed-ignore-diff-pointer](#malformed-ignore-diff-pointer)
- [malformed-ignore-diff-jq-expression](#malformed-ignore-diff-jq-expression)
- [unknown-sync-option](#unknown-sync-option)
- [unknown-resource-hook](#unknown-resource-hook)
- [malformed-sync-wave](#malformed-sync-wave)
- [floating-target-revision](#floating-target-revision)
- [missing-prune-protection](#missing-prune-protection)

## Suppressing a finding

For one Application, annotate it with the rule ID and the reason:

```yaml
metadata:
  annotations:
    argocd-source-lint/ignore-phantom-target: "path is created by a Job at sync time"
```

The reason is required: an annotation with an empty value suppresses nothing, and the
output says so, as it does for an annotation naming an unknown rule. On an ApplicationSet,
put the annotation in `spec.template.metadata.annotations`. Findings that belong to no
single Application (`orphan-source`, `double-coverage`) can't be suppressed this way.

Other options:

- `exclude_paths` in `.argocd-lint.yaml` removes files from `orphan-source`.
- A file containing the text `argocd-lint:ignore`, or `+argocd:skip-file-rendering`, which
  ArgoCD itself skips, is never reported by `orphan-source`.
- A baseline (`--write-baseline`) accepts every current finding, so only new ones block.
- `--skip <rule>` leaves a rule out of one run, for example in a pipeline that checks only
  part of the repository.
- Setting a rule's severity to `info` in `.argocd-lint.yaml` keeps it in reports without
  blocking CI.

## orphan-source

Default severity: `error`.

A YAML file tracked by Git under one of the `scan_roots` folders that no Application source
covers. ArgoCD never applies it, so a change to it has no effect, and ArgoCD doesn't report
it. Typical causes: a source `path` that was renamed, a `directory.include` that misses the
file, or a leftover from a removed component. Untracked and gitignored files are not
checked, since they never reach ArgoCD.

Fix: point a source at the file, delete it, or add it to `exclude_paths`. The rule does
nothing until `scan_roots` is set.

A file in a folder that an ApplicationSet the tool can't expand may deploy from (see
`unresolvable-generator`) is reported as `info` instead.

## broken-values-ref

Default severity: `error`.

A Helm `valueFiles` entry that points to a file that doesn't exist, either relative to the
chart path or through a `$ref` to another source of the same Application.

Fix: correct the path or the `ref` name. Entries of a source with
`helm.ignoreMissingValueFiles: true` are not checked, since ArgoCD skips them too. Plain
entries are relative to the source path, or to the repository root when they start with `/`;
remote `https://` files are not checked. A glob such as `values-*.yaml` is reported only when
it matches no file; patterns with `[...]` are not checked. ArgoCD's build variables, such as
`$ARGOCD_APP_NAME`, are replaced by their value; an entry using `$ARGOCD_APP_REVISION` is not
checked.

## missing-ignore-diff

Default severity: `warning`.

A resource from a known operator (CNPG, cert-manager, Elastic ECK, RabbitMQ, Strimzi,
Zalando Postgres Operator, Keycloak Operator, External Secrets Operator) in an Application
with `selfHeal: true`, without an `ignoreDifferences` entry for the objects that operator
generates or updates. Self-heal then keeps resetting them to the state in Git.

Fix: add an `ignoreDifferences` entry for each resource named in the messages. For a CNPG
`Cluster` named `pg`, which creates the Secrets `pg-app` and `pg-ca`:

```yaml
spec:
  ignoreDifferences:
    - kind: Secret
      name: pg-app
      jsonPointers:
        - /data
    - kind: Secret
      name: pg-ca
      jsonPointers:
        - /data
```

An entry counts when it has no `group`, its `kind` is the resource's kind or `*`, its `name`
is omitted or the resource's name, and its `namespace` is omitted or the `metadata.namespace`
of the custom resource. Leaving out `name` covers every Secret of the Application.

Other operators can be added with `known_operators` in `.argocd-lint.yaml`. Each entry names
the custom resource that triggers the check, where its name is read, and the core resources
the operator derives from that name:

```yaml
known_operators:
  - crd_trigger: example.com/Database   # <group>/<Kind>
    name_from: metadata.name             # dotted path to the name
    expect_ignore_on:
      - kind: Secret
        name_pattern: "{name}-credentials"
```

`name_from_each` can name a mapping in the same resource, such as `spec.users`; each key is
then available as `{user}` in `name_pattern`.

## phantom-target

Default severity: `error`.

A source `path` with no tracked files at its `targetRevision`. A typo or a directory moved
by a refactor still looks valid in the Application manifest.

A revision that isn't in the checkout at all is reported as `unverifiable` instead, because
the path can't be checked. In CI, fetch the branches and tags your Applications target.

Fix: correct the `path`, or commit the directory it names.

## unresolvable-generator

Default severity: `info`.

An ApplicationSet whose Applications can't be generated from the checkout, for example: a
`clusters`, `scmProvider`, `pullRequest` or `plugin` generator, `templatePatch`, a
generator-level `selector`, a git generator on another repository or on a revision missing
from the clone, or a template placeholder no generator sets. The Applications it would
generate are not checked by the other rules, and `orphan-source` reports the files they may
deploy as `info`.

It also reports an ApplicationSet that ArgoCD itself can't expand, which then generates no
Application at all: a git `files` params file that isn't an object or a list of objects, a
`list` element value that isn't a string (classic templates), two `matrix` children setting
one parameter to different values (classic templates), a `merge` with fewer than two children
or with two entries of one child sharing a merge key, and with `goTemplate: true` a template
whose rendering fails (an undefined function, a field of a string, `eq` between a number and
an integer literal) or an unknown `goTemplateOptions` entry. These messages start with
"Argo CD can't expand this ApplicationSet".

With `goTemplate: true`, the template is rendered as ArgoCD renders it, field by field, for
the fields of ArgoCD's Application type only. Supported: all of Go's `text/template` except
`define`, `template` and `block`, and these functions:

- Go: `and`, `or`, `not`, `eq`, `ne`, `index`, `printf` (verbs `s d v q t f`, flags `-`
  and `0`);
- sprig: `default`, `lower`, `upper`, `replace`, `trimPrefix`, `trimSuffix`, `quote`, `cat`,
  `indent`, `base`, `dig`, `toJson`, `mustToPrettyJson`;
- ArgoCD: `toYaml`, `normalize`, `slugify`.

What this tool can't render the way ArgoCD does is reported with "not supported by this tool",
for the whole ApplicationSet, never part of it: another function, a template printing
`<no value>`, `printf` output that Go prints as an error, `missingkey=zero`, numbers that are
not decimal, `lower` or `upper` of non-ASCII text, characters from recent Unicode versions in
`quote`, `printf %q` or `toYaml` keys, `toYaml` output it can't predict (long strings folded
outside a map, multiline strings in a list, timestamps), and `list` elements Kubernetes may
read differently in YAML 1.1 (unquoted `yes`, `y` as a key, `0755`, a date; quoted strings are fine).

An ApplicationSet comes from the repository, which may not be trusted, so its expansion has
bounds. When one is reached, the ApplicationSet gets this finding and none of its
Applications is generated, never a partial set:

| Bound | Value |
| --- | --- |
| Applications per ApplicationSet | 10,000 |
| Combinations of a `matrix` | 10,000 |
| Entries of a `matrix` first child whose parameters the second child uses | 10,000 |
| Git revisions used by the git generators of one ApplicationSet | 20 |
| Size of one git `files` params file | 1 MiB |
| Size of all the files one git `files` generator matches | 16 MiB |
| `[`/`{` nesting in a params file or `elementsYaml` | 250 levels |
| Length of one Go template string (`goTemplate: true`) | 64 KiB |
| Output of one rendered string, or of one template function call | 256 KiB |
| Output of all the strings one ApplicationSet renders | 16 MiB |
| `range` iterations while rendering one string | 10,000 |
| Nesting of `if`, `with`, `range` and parentheses in one string | 100 levels |
| Template operations of one ApplicationSet (a function call counts 10, plus its size) | 1,000,000 |

Fix: nothing to fix when the generator needs the cluster or an external service (`clusters`,
`scmProvider`, `pullRequest`, `plugin`). For a placeholder without a value, a missing
revision or a generator ArgoCD can't expand, the message says what is wrong.

## double-coverage

Default severity: `error`.

The same resource deployed by two different Applications to the same cluster and
namespace. Each Application syncs it on its own loop, so the resource can flip between
their versions.

Files under a source whose `kustomization.yaml` sets `namePrefix`, `nameSuffix` or
`namespace` are not compared, since the rendered resources get other names. With
`argocd_instances` set, Applications of different instances are not compared either.

The same file deployed to different clusters or namespaces is not reported. A resource's
own `metadata.namespace` takes precedence over the destination namespace, as in ArgoCD.
When the target can't be determined, the resource is skipped rather than reported.

Fix: keep the file in one Application's source, for example by moving the shared part into
a Kustomize component each overlay includes.

## revision-mismatch

Default severity: `info`.

A source whose `targetRevision` points to another commit than the checked-out one. The
other rules then read that source from a snapshot of its own revision, not from the
working tree. This is informational, to explain why a change in the current branch doesn't
affect that Application.

To make the Application follow the branch being changed, point `targetRevision` at it.

## project-scope-violation

Default severity: `error`.

An Application source repository or destination that its `AppProject` doesn't allow
through `sourceRepos` or `destinations`. ArgoCD refuses to sync it, which shows up only
once the Application exists.

Cases that can't be decided from the repository are reported as `info`: a project not
found in the repository, several projects with the same name, negated patterns, or a
destination given by `name` while the project lists `server`, or the other way around.

Matching follows ArgoCD: `sourceRepos` compares normalized URLs, which keep their scheme, so
an `https://` pattern doesn't allow the `git@` URL of the same repository. A destination
entry without `namespace` only allows an Application without one.

Fix: add the repository or destination to the AppProject, or move the Application to a
project that allows it.

## hpa-selfheal-conflict

Default severity: `warning`.

A HorizontalPodAutoscaler targeting a workload of an Application with self-heal enabled,
without both an `ignoreDifferences` entry for `spec.replicas` and the
`RespectIgnoreDifferences=true` sync option. ArgoCD keeps restoring the replica count from
Git while the HPA scales it.

A workload that sets no `spec.replicas` in Git is not reported: there is no count to
restore, and it is the setup the Kubernetes HPA documentation recommends.

Fix: remove `spec.replicas` from the workload, or add both:

```yaml
spec:
  ignoreDifferences:
    - group: apps
      kind: Deployment
      jsonPointers:
        - /spec/replicas
  syncPolicy:
    syncOptions:
      - RespectIgnoreDifferences=true
```

## sync-validation-disabled

Default severity: `info`.

The `Validate=false` sync option, which skips Kubernetes schema validation during sync, so
an invalid manifest is applied instead of rejected. It can be intentional, hence the `info`
default.

Fix: remove the option once the manifests it was added for validate.

## duplicate-application-name

Default severity: `error`.

Two or more Applications with the same namespace and name. ArgoCD identifies an
Application by that pair, so the manifests overwrite each other in the cluster.

With several ArgoCD instances in one repository, list their folders under `argocd_instances`
so names are only compared within each instance.

Applications generated by an ApplicationSet live in the ApplicationSet's namespace. Names
the Kubernetes API would refuse, such as an unfilled `<CLUSTER_NAME>-app` template, are
not compared.

Fix: rename one of them, or set `metadata.namespace` if they belong to different
Application namespaces.

## malformed-ignore-diff-pointer

Default severity: `warning`.

An `ignoreDifferences.jsonPointers` entry that doesn't start with `/`, as RFC 6901
requires. `spec.replicas` matches nothing; `/spec/replicas` is the pointer form.

Fix: write the pointer with a leading `/` and `/` between keys.

## malformed-ignore-diff-jq-expression

Default severity: `warning`.

An `ignoreDifferences.jqPathExpressions` entry written as a JSON pointer, such as
`/spec/replicas` pasted into the wrong field, or as a path without its leading `.`, such as
`spec.replicas`. Other jq expressions, such as `(.a[] | select(.b)).c` or
`getpath(["spec", "replicas"])`, are accepted. ArgoCD fails to
build the whole `ignoreDifferences` normalizer when one expression doesn't parse, so the
other entries stop working too.

Fix: move a JSON pointer to `jsonPointers`, or write the path as `.spec.replicas`.

## unknown-sync-option

Default severity: `warning`.

A sync option key, at Application level or in the `argocd.argoproj.io/sync-options`
annotation, that ArgoCD doesn't recognize. Keys are case-sensitive and an unknown one is
ignored without an error. With `argocd_version` set, a key newer than that version is
reported too.

Fix: use the spelling from the
[sync options documentation](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/),
or upgrade ArgoCD for a newer key.

## unknown-resource-hook

Default severity: `warning`.

An `argocd.argoproj.io/hook` or `argocd.argoproj.io/hook-delete-policy` value that ArgoCD
doesn't recognize. Accepted hooks are `PreSync`, `Sync`, `Skip`, `PostSync`, `SyncFail`,
`PreDelete` and `PostDelete`; accepted policies are `HookSucceeded`, `HookFailed` and
`BeforeHookCreation`. A resource whose only hook value is unknown is still treated as a
hook, but for no phase, so ArgoCD never applies it.

Fix: use one of the values above, with the same case.

## malformed-sync-wave

Default severity: `warning`.

An `argocd.argoproj.io/sync-wave` value that isn't an integer. ArgoCD falls back to wave
`0`, so the resource is applied in a different order than intended.

Fix: write the wave as a quoted integer, such as `"-1"` or `"5"`.

## floating-target-revision

Default severity: `warning`.

A `targetRevision` that is a semver range: `*`, `1.2.*`, `~1.2`, `^1.2`, `>=1.2.0 <1.3.0`,
`1.2 - 1.4` or `1.2 || 2.0`, and for a chart also `1.2.x`. ArgoCD resolves it against the
highest matching chart version or Git tag, so the deployed content can change with no commit
in this repository. Exact versions (`1.2.3`, `=1.2.3`), branch names and `HEAD` are never
reported.

Fix: pin an exact version or tag.

## missing-prune-protection

Default severity: `warning`.

A Namespace or a CustomResourceDefinition deployed by an Application with
`syncPolicy.automated.prune: true`, without `Prune=false` or `Prune=confirm` in its
`argocd.argoproj.io/sync-options` annotation. If the resource leaves the rendered manifests,
for example after a file is deleted or a `kustomization.yaml` line removed, ArgoCD deletes
it at the next sync. Kubernetes then deletes what it holds: every object in the Namespace,
including Secrets and volumes created by hand, or every custom resource of the CRD's kind in
the cluster.

What stops the prune:

| Setting | Stops the prune |
| --- | --- |
| `Prune=false` on the resource | yes, the resource is left in place |
| `Prune=confirm` on the resource | yes, the sync waits until someone confirms |
| `Prune=false` or `Prune=confirm` in the Application's `syncOptions` | from ArgoCD 3.4.0 only, and only for resources with no `Prune` option of their own |
| `Delete=false`, `helm.sh/resource-policy: keep` | no, they only apply when the Application itself is deleted |
| `PruneLast=true` | no, it only moves the deletion to the end of the sync |

Option names and values are case-sensitive: `Prune=False` protects nothing. When
`argocd_version` in `.argocd-lint.yaml` is older than 3.4.0, an Application-level `Prune`
option is not counted; when it is unset, it is.

Only resources written in Git as they are deployed are checked: the files of a directory
source, and those a `kustomization.yaml` lists under `resources`, followed into nested
kustomizations. A kustomization's `commonAnnotations` replaces the resource's own
annotation, as Kustomize does. Not checked, and not reported:

- Helm charts, config management plugins and Jsonnet, which this tool does not render. This
  includes the `crds/` folder of a chart.
- The resources of a kustomization that uses `patches`, `patchesStrategicMerge`,
  `patchesJson6902`, `components`, `transformers`, `replacements`, `generators` or
  `helmCharts`, including those of the kustomizations it lists, and every resource of a
  source whose Application sets `kustomize.commonAnnotations`, `patches` or `components`:
  the rendered annotation may differ from the file.
- Sources in another Git repository. Unlike other rules, this one adds no `info` for them:
  `orphan-source` and `phantom-target` already report each of them.
- A protection added on the cluster only, for example with `kubectl annotate`. ArgoCD reads
  the live object, so it does protect, but this tool can't see it: write it in Git, or
  suppress the finding.

The finding points to the resource's file and line. To suppress it, put the
`argocd-source-lint/ignore-missing-prune-protection` annotation on the Application, or in
an ApplicationSet's template.

Fix:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: team
  annotations:
    argocd.argoproj.io/sync-options: Prune=false
```

`Prune=false,Delete=false` also keeps the Namespace when the Application itself is deleted.
