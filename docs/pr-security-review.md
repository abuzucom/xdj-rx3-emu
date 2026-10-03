# PR security review

This repository runs the `abuzucom/foucault` security review on each pull
request. Foucault `docs/pr-security-review.md` describes the architecture and
trust boundary.

## Wiring

- `.github/workflows/security-review-pr.yml` is the `abuzucom/agents` copy of
  the foucault caller. The workflow triggers on completion of the
  `Immutable Compliance` workflow. The trigger runs default-branch code. The
  review starts once the caller reaches `main`.
- The `review` job calls `security-review.yml` and loads `AUDIT.md` from
  `abuzucom/foucault` at commit `62851df1ef177593adbb9e06b223f5a6dce66fc0`
  (release 3.3.10). The `uses:` pin and `audit_ref` name the same commit.
- `ci/build_pr_case.py`, `ci/run_model_command.py`, `ci/call_model.py`, and
  `ci/model_providers.json` match that commit byte for byte.
  `scripts/check_pr_review_response.py` arrives with the agents `scripts/`
  set.
- The job maps only `OLLAMA_API_KEY` to `MODEL_API_KEY`. The active profile
  is Ollama with `kimi-k2.7-code`.
- `fail_on_block: true`. Fork pull requests receive an explicit skip result
  and no secret.

## Tests

`tests/test_security_review_wiring.py` comes from `abuzucom/agents`. The test
checks the trigger, the pin, the secret mapping, the fork skip, and the
adapter files.

## Upgrade

The agents template owns the pin. Take a newer foucault pin together with the
agents test and `ci/` files that match it.
