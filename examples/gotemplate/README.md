# goTemplate example

An ApplicationSet with `goTemplate: true`: a `matrix` of a git `files` generator (one
`config.yaml` per environment) and a `list` of applications. Its template uses fields of
the config files (`.cluster.server`), the git `path` map (`.path.basename`), `normalize`,
`if eq` and `printf`.

It generates four Applications: `guestbook-dev`, `redis-dev`, `guestbook-prod` and
`redis-prod`. The last one points at `apps/prod/redis`, which does not exist, so ArgoCD would
create it and fail to sync it. Run the linter from this folder to see it:

```bash
git clone https://github.com/gillesl-dev/argocd-source-lint.git
cd argocd-source-lint/examples/gotemplate
uvx argocd-source-lint .
```

```text
| error | phantom-target | redis-prod | bootstrap/platform-appset.yaml | path `apps/prod/redis` not found at revision `HEAD` — phantom target. |
```

`.argocd-lint.yaml` lists the repository's URL under `repo_urls`, so the Applications'
`repoURL` counts as this checkout from a fork or an SSH clone too.
