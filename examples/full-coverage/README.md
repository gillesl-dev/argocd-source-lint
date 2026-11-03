# Full coverage example

One deliberately broken Application per rule, plus a couple of compliant ones for contrast.
Filenames describe the scenario (`broken-ref-file-app.yaml`, `double-coverage-a-app.yaml`,
`hpa-selfheal-conflict-app.yaml`, ...).

Clone this repository and run the linter against this folder to see every rule fire on a Git
checkout:

```bash
git clone https://github.com/gillesl-dev/argocd-source-lint.git
cd argocd-source-lint/examples/full-coverage
uvx argocd-source-lint .
```

A few Applications point at `some/path` in a different repository, or request an
`ApplicationSet` generator that needs live cluster access. Those are expected to come back as
`info`, because the linter only reads the local Git checkout. See the main
[README](../../README.md#limitations).

The table only counts `info` findings, which include `sync-validation-disabled` and
`revision-mismatch` by default. Add `--show-info` to see their rows.

Its Applications point at `https://github.com/gillesl-dev/argocd-source-lint.git`. From a
fork or an SSH clone, list that URL under `repo_urls` in its `.argocd-lint.yaml`, or
every source is treated as another repository.
