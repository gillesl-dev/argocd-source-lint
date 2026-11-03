# CLAUDE.md

Guide for coding agents working on this repository. Human contributors: see
[CONTRIBUTING.md](./CONTRIBUTING.md), which this file does not replace.

## Ground rules

- Don't commit, push, tag or rewrite history. Propose the command and wait for the maintainer.
  Commit messages have no trailer naming a tool or an agent.
- No network access at lint time, and no cluster access in any rule. A source in another Git
  repository is reported as not covered, never fetched (DESIGN.md "Single-repository scope").
- Check Argo CD behavior in the Argo CD source or docs before encoding it in a rule. Cite the
  function or doc page in a comment when the behavior is not obvious.
- Don't change the text of an existing finding message. Baselines match messages verbatim, so a
  reworded message makes every baselined finding reappear.

## Before handing back a change

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src --ignore-missing-imports
uv run pytest
```

All four must pass. Files are LF only (`.gitattributes`); on Windows, write test fixtures as
bytes rather than with `Path.write_text`.

## Where things go

| Change | Files |
|---|---|
| New rule | `rules/<rule>.py`, `RULES` in `cli.py`, `DEFAULT_RULE_SEVERITIES` and `RULE_DESCRIPTIONS` in `policy.py`, a section in `docs/rules.md`, tests (CONTRIBUTING "Adding a rule") |
| New operator signature | `rules/known-operators.yaml`, only with the operator's own docs as source |
| False positive fixed | a negative case in `tests/test_mutations.py`, plus a line under `[Unreleased]` in `CHANGELOG.md` |
| Version bump | `pyproject.toml`, `uv.lock`, `action.yml`, `templates/lint.yml`, README CI examples (`tests/test_release_versions.py` checks them) |

## Writing

- English, plain and short. State what the tool does, not how it was built.
- Comments explain a non-obvious reason only (CONTRIBUTING "Code style").
- `argocd-source-lint-plan-technique.md` is a private planning note: never quote it or link it.
