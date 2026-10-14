# Contributing

## Commit hooks

Install [prek](https://prek.j178.dev/) and enable the repository's hooks:

```sh
uv tool install prek==0.5.5
prek install --allow-missing-config
prek run --all-files
```

The missing-config option allows older worktrees without this configuration to
keep working. Worktrees containing `.pre-commit-config.yaml` still run the checks.
The `pre-commit` hook checks the branch name, Python lint, and formatting. The
`commit-msg` hook checks Conventional Commits. Both Ruff hooks only check files;
they do not rewrite a commit's contents. Fix lint and formatting explicitly:

```sh
uvx ruff==0.16.8 check --fix src tests scripts/build-launcher.py scripts/verify-release.py scripts/verify-skill.py scripts/ci scripts/check_branch_name.py
uvx ruff==0.16.8 format src tests scripts/build-launcher.py scripts/verify-release.py scripts/verify-skill.py scripts/ci scripts/check_branch_name.py
prek run --all-files
```

The Ruff version and checked paths are pinned in
[the hook configuration](.pre-commit-config.yaml); the Python rules remain in
[pyproject.toml](pyproject.toml). CI uses these same hooks on all tracked files
within their configured paths. Package, integration, and release validation starts
after the quality checks pass; see [CI checks](docs/ci.md).

## Branches and commits

[cchk.toml](cchk.toml) defines the allowed branch and commit types. Work branches
use `feat/`, `fix/`, `release/`, or `chore/`, followed by lowercase ASCII letters
and digits separated by single hyphens. Release versions may also contain dots,
as in `release/v1.2.0-rc.1`; every separator must have an alphanumeric character on
both sides. The default branch `main` is the standalone exception.

Commit types are independent of branch types: for example,
`docs: clarify setup` is valid on `chore/update-docs`. Commit-check uses Conventional
Commits with its default subject length of 5–80 characters. Pull requests check
their commits and title so the title can be used for a Conventional Commit when
squash merging.

Local hooks can be bypassed. Configure `CI result` and the standalone `PR policy`
as required status checks to enforce source validation and PR title/commit policy.
The [CI guide](docs/ci.md) describes their triggers and gating behavior.
