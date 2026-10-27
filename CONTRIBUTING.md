# Contributing to argocd-source-lint

See [DESIGN.md](./DESIGN.md) for the reasoning behind the mono-repo v1 scope and other
non-obvious implementation decisions.

## AI assistance

Parts of the code and documentation were written with an AI assistant (Claude Code). Commits
since `v1.2.0` carry an `Assisted-by` trailer; earlier commits carry no attribution trailer.
All changes are reviewed and merged by the maintainer.

If you use Claude Code on this repository, run `git config core.hooksPath .githooks` once per
clone. It normalizes AI attribution to that same trailer regardless of what the tool writes by
default.

On 2026-10-27 the commit history was rewritten once to make AI attribution consistent. Tags
`v1.2.1` and `v1.2.2` now point to new commits with identical content. If you cloned before
that date, run `git fetch --tags --force && git pull --rebase`; a plain `git pull` would merge
the old commits back in. Published PyPI releases are unaffected.

## Setup

```bash
uv sync --extra dev
uv run pytest
```

No cluster access or credentials are required. Tests run against small Git repositories created on
the fly in `tests/conftest.py`; filesystem mocks are not used.

CI (`.github/workflows/test.yml`) runs the full suite on every push and pull request across
Python 3.11-3.13 on Linux and Windows. Keep it green before requesting a review.

## Project structure

```text
src/argocd_source_lint/
├── cli.py           # Typer entrypoint
├── models.py        # Application, Source, Finding, Severity...
├── loader.py        # Application discovery and parsing
├── git_context.py   # Local repo detection, mono-repo classification, Git primitives
├── fsutil.py        # YAML discovery and multi-document parsing
├── coverage.py      # Coverage resolution for path sources
├── policy.py        # Loading and merging .argocd-lint.yaml
├── rules/           # One rule = one Rule class
└── reporters/       # table, json, sarif, gitlab-codequality, junit
```

## Adding a rule

1. Create `rules/my_rule.py` with a class inheriting from `rules.base.Rule`.

   Define a `rule_id` and implement:

   ```python
   check(applications, repo_root, policy, local_origin) -> list[Finding]
   ```

2. Use `git_context.local_path_sources` and `external_path_sources` to preserve the mono-repo v1
   behavior.

   Check local sources normally. External sources should produce an `info` finding through
   `rules.base.external_source_finding`.

   An Application may contain both local and external entries in `spec.sources`, so do not skip the
   whole Application just because one source is external.

3. Register the rule in `RULES` in `cli.py`.

4. Add tests:
   - use the shared `tests/fixtures/good_repo` for the compliant case;
   - add `tests/fixtures/bad_my_rule/` for the failing case;
   - cover edge cases with unit tests.

5. If possible, run the rule against a real GitOps repository before opening the PR. This is how
   several early coverage gaps were found, including `directory.include`/`exclude` and
   `$ref`/`helm.valueFiles`.

## Adding an operator to `missing-ignore-diff`

The operator signature pack in `rules/known-operators.yaml` is data-driven, so adding an operator
does not require Python changes.

```yaml
- crd_trigger: <group>/<Kind>
  name_from: metadata.name
  expect_ignore_on:
    - kind: Secret
      name_pattern: "{name}-suffix"
```

For operators that derive one resource per entry in a CRD mapping, use `name_from_each` with the
dotted path to that mapping. Each key becomes `{user}` alongside `{name}` in `name_pattern`.

See the Zalando Postgres Operator entry in `known-operators.yaml` for an example.

Only add a signature when the operator's own documentation confirms the derived resource name.
Do not infer naming behavior from examples or assumptions. See [DESIGN.md](./DESIGN.md) for the
reasoning behind this constraint.

## Code style

- Add comments only when they explain a non-obvious reason: a hidden constraint, verified ArgoCD
  behavior, or a workaround.
- Avoid introducing an abstraction until there is a second real use case. `coverage.py`, `fsutil.py`,
  and the Git helpers in `git_context.py` all followed that rule.
- Every rule must work without cluster access.
- Ruff handles linting and formatting, with configuration in `pyproject.toml`:

  ```bash
  uv run ruff check .
  uv run ruff format .
  ```

## Tests

Run the full test suite with:

```bash
uv run pytest -v
```

Each rule should have:

- a compliant case using the shared `good_repo` fixture;
- a failing `bad_<rule>/` fixture;
- inline tests with the `git_repo` fixture for edge cases that do not need a dedicated directory.
