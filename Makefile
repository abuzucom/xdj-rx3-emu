.PHONY: sync check lint test identity changelog

# Overridable so a platform without this name can supply its own:
#   make test PYTHON=py
PYTHON ?= python3

# README.md carries upstream prose in the original author's voice, so the
# hedging check skips it.
PROSE_FILES = AGENTS.md CHANGELOG.md docs/development.md \
	docs/project-orientation.md docs/template-drift.md \
	docs/pr-security-review.md docs/pr-quality-review.md \
	plan/HANDOFF.md.example SECURITY.md CONTRIBUTING.md \
	.github/PULL_REQUEST_TEMPLATE.md .github/ISSUE_TEMPLATE.md
SPELLING_FILES = README.md $(PROSE_FILES)

sync:
	$(PYTHON) scripts/sync.py

check:
	$(PYTHON) scripts/sync.py --check

changelog:
	$(PYTHON) scripts/check_changelog.py

lint:
	$(PYTHON) scripts/lint_style.py
	$(PYTHON) scripts/check_us_spelling.py $(SPELLING_FILES)
	$(PYTHON) scripts/check_english_only.py $(SPELLING_FILES)
	$(PYTHON) scripts/check_hedging.py $(PROSE_FILES)
	$(PYTHON) scripts/check_conflict_markers.py

test:
	$(PYTHON) scripts/run_tests.py

identity:
	$(PYTHON) scripts/check_git_identity.py --advise
