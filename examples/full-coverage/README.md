# Full coverage example

One deliberately broken Application per rule, plus a couple of compliant ones for contrast.
Filenames describe the scenario (`broken-ref-file-app.yaml`, `double-coverage-a-app.yaml`,
`hpa-selfheal-conflict-app.yaml`, ...).

Clone this repository and run the linter against this folder to see every rule fire on a real
checkout, instead of reading a static table:

```bash
git clone https://github.com/gillesl-dev/argocd-source-lint.git
cd argocd-source-lint/examples/full-coverage
uvx argocd-source-lint .
```

A few Applications point at `some/path` in a different repository, or request an
`ApplicationSet` generator that needs live cluster access. Those are expected to come back as
`info`: this tool only reads the local Git checkout, see the main [README](../../README.md#limitations).

Since 1.3.0 the table only counts `info` findings, which includes `sync-validation-disabled`
and `revision-mismatch` by default. Add `--show-info` to see their rows.
