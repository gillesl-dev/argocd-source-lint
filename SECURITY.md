# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:

https://github.com/gillesl-dev/argocd-source-lint/security/advisories/new

Do not open a public issue for a vulnerability. GitHub's advisory flow keeps the report private
until a fix is available.

## Supported versions

Only the latest tagged release is supported. There is no LTS or backport policy.

## Trust model

The repository being analyzed is untrusted input. That includes paths, revisions, glob patterns,
names and other manifest values.

Rules:

- Paths derived from repository content must stay inside the repository or its materialized
  revision snapshot.
- Repository-controlled values passed to `git` or another subprocess must never be interpreted as
  command-line options.
- Repository-controlled input must not reach algorithms with adversarially unbounded worst-case
  cost.
- Repository-controlled strings must be escaped before rendering as markup, HTML, XML, or similar
  output.

See [DESIGN.md](./DESIGN.md) for the issues that led to these rules and their fixes.

`.argocd-lint.yaml` and `.argocd-lint-baseline.yaml` are themselves repository content. An
untrusted pull request can modify policy or baseline data in the same change that introduces a
finding.

Protect both files with `CODEOWNERS` or branch protection when analyzing untrusted forks.

## Out of scope

The following are not vulnerabilities in `argocd-source-lint`:

- repository misconfigurations correctly reported by the linter;
- resource exhaustion caused only by a legitimately very large repository without adversarial input.
