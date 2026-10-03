# Template drift

This file records local differences against the templates this repository
adopted. `DRIFT.md` in `abuzucom/agents` holds the drift policy and assigns
this file to the adopting repository.

## Sources

| Source | Commit | Material |
|---|---|---|
| `abuzucom/agents` | `fd9da22881806ff1386c13eff4be26358914fe24` | Policy, checkers, hooks, tests, workflows, templates |
| `abuzucom/foucault` | `62851df1ef177593adbb9e06b223f5a6dce66fc0` (3.3.10) | PR security review, through the agents copy of the caller |
| `abuzucom/euler` | `b23947068a328c19215f8ade3db7bd2b4bcb05cc` | PR quality review |
| `abuzucom/rough` | `f00a91e5041a58f9e56c67539f1db4eeac6ca675` | Ruff baseline, Ruff 0.16.9 |

## Adoption scope

This repository took the full agents template. Every hook and checker that
`AGENTS.md` names is present.

### Taken

- `AGENTS.md`, `docs/agent-policy/`, and the eight synchronized copies that
  `scripts/sync.py` generates.
- Every file under `scripts/`, `hooks/`, and `tests/` from the template.
- `.claude/settings.json`, `.codex/config.toml`, `.codex/hooks.json`,
  `.gemini/settings.json`, `.agents/hooks.json`.
- `tools/hook-trace/sitecustomize.py`, `hook-coverage-baseline.json`,
  `shared-files.json`, `requirements-checkers.txt`, `.editorconfig`.
- `.github/workflows/sync-check.yml`, `agents-compliance.yml`,
  `agents-md-compliance.yml`, `gate-integrity.yml`,
  `immutable-conflict-check.yml`, and `security-review-pr.yml`.
- `ci/build_pr_case.py`, `ci/call_model.py`, `ci/model_providers.json`, and
  `ci/run_model_command.py`.
- `.github/PULL_REQUEST_TEMPLATE.md`, `.github/ISSUE_TEMPLATE.md`, and
  `plan/HANDOFF.md.example`.
- `SECURITY.md` and `CONTRIBUTING.md`, filled from the `.example` files.
- The rough files `ruff.toml`, `ruff.warn.toml`, and
  `scripts/check_ruff_configs.py`. Rough `requirements-dev.txt` became
  `requirements-ruff.txt`.
- No euler file. `quality-review-pr.yml` calls the euler reusable workflow at
  the pinned commit.

### Declined

- `DRIFT.md` and `adopters/`. Both belong to the template repository.
- The template `README.md`, `CHANGELOG.md`, `Makefile`, `LICENSE`,
  `docs/gate-threat-model.md`, `docs/pr-security-review.md`, and
  `docs/agent-policy/source-orientation.md` as verbatim copies.
- The AgentLint step in `sync-check.yml`. The step adds an unapproved
  third-party action.

## Local differences

Every taken file not listed here matches its source byte for byte.

- `docs/project-orientation.md` points to `docs/development.md`. The assembled
  policy has about 220 bytes free under its intended 64 KiB cap. The
  orientation detail lives in `docs/development.md`. The synchronized copies
  differ from the template because the orientation differs.
- `.claudeignore` drops the JavaScript entries and adds the firmware paths
  from `.gitignore`.
- `.gitattributes` adds `eol=lf` for `*.sh`, `*.py`, `*.c`, and `*.conf`.
  WSL runs those files from a Windows checkout.
- `.pre-commit-config.yaml` runs the agents local hooks, then the rough Ruff
  hooks.
- `Makefile` names this repository's prose in `PROSE_FILES`. The hedging
  check skips `README.md`. That file carries upstream prose.
- `.github/workflows/sync-check.yml` names this repository's prose files and
  drops the AgentLint step.
- `scripts/check_agent_prose_gate.py` sets `TARGET_FILES` to the
  `sync-check.yml` list. The template test requires the two lists to match.
- `ruff.toml` sets `target-version = "py311"` and excludes `ci/`, `hooks/`,
  `tests/`, `tools/`, and the agents scripts. Those copies stay byte-identical
  with the sources. The rough block tier reports 168 findings in the copies.
- `hook-coverage-baseline.json` is the template file, copied rather than
  regenerated. Every file under `hooks/` matches the template.

## Environment limitations

Sixteen template tests fail in the Claude Code cloud sandbox and in the
`abuzucom/agents` repository run in the same sandbox. The causes are the
injected Git proxy configuration and the root user. The failing suites are
`test_trusted_git_hardening`, `test_trusted_git_proxy`, and
`test_trusted_gh_failure_classification`, plus
`test_malformed_environment_config_vector_fails_closed` and
`test_match_carries_effective_git_context`. The full suite passed on the
GitHub-hosted Windows and macOS test jobs and the Linux hook coverage run.

## License boundary

The repository carries a split license.

Files inherited from the upstream fork stay MIT under `LICENSE`: `LICENSE`,
`README.md`, `.gitignore`, everything under `wsl/`, and everything under
`docs/screenshots/`. Later edits keep those files under MIT.

Every other path carries BSD-3-Clause, copyright ABUZUCOM LLC, under
`LICENSE.BSD-3-Clause`. The copied agents, foucault, euler, and rough material
keeps its BSD-3-Clause notice and conditions through that file.

## Template records

`adopters/xdj-rx3-emu.md` in `abuzucom/agents`, `abuzucom/foucault`, and
`abuzucom/euler` records this adoption in each source repository.
