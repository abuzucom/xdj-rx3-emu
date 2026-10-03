# PR quality review

This repository runs the `abuzucom/euler` quality review on each pull request.
Euler `docs/pr-quality-review.md` describes the architecture and trust
boundary.

## Wiring

- `.github/workflows/quality-review-pr.yml` mirrors the euler caller. The
  workflow triggers on completion of the `ci` workflow. The trigger runs
  default-branch code. The review starts once the caller reaches `main`.
- The `review` job calls `quality-review.yml` and loads `QUALITY.md` from
  `abuzucom/euler` at commit `b23947068a328c19215f8ade3db7bd2b4bcb05cc`.
  The `uses:` pin and `quality_ref` name the same commit.
- Euler runs the euler `ci/` scripts from its own checkout. This repository
  copies no euler file.
- The job maps only `OLLAMA_API_KEY` to `MODEL_API_KEY`. The euler profile
  uses Ollama.
- `fail_on_block: true`. A `BLOCK` or `NEEDS-HUMAN` verdict fails the
  `quality-review` check.
- Fork pull requests receive a skipped `quality-review` check and no secret.
- `exclude_paths` stays empty. The prescan covers every changed file.

## Tests

`tests/test_quality_review_wiring.py` checks the trigger, the pins, the
secret mapping, the fork skip, the action pins, and the read-only default
permissions.

## Upgrade

Change the `uses:` pin, `quality_ref`, and `PINNED_COMMIT` in the test to the
same new euler commit in one pull request.
