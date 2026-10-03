# GitHub operations
Run hosted GitHub operations through:

`python scripts/trusted_gh.py run <gh arguments>`

The wrapper resolves `gh` outside the repository and verifies the authenticated
account through a fixed account request. Direct `gh` lookup remains denied
because shell lookup can select a repository-controlled executable.

Repository-bound commands receive a validated `--repo OWNER/REPOSITORY` target.
The wrapper resolves `origin` from the local checkout or worktree metadata.
The wrapper fails closed when that context is missing or unsafe. The wrapper
keeps `gh` execution in an external safe directory.

PR creation also receives a validated `--head OWNER:BRANCH` target
when no head option exists. Global options may precede the GitHub command.

Executable changes require a behavioral test. Required CI checks the changed
range and fails when an executable change lacks a changed test.

Read-only repo inspection, checks, workflow reads, and pull request
diffs remain available through the wrapper. GitHub clone and fetch use the
fixed commands `python scripts/trusted_git.py clone <github-url> <directory>`
and `python scripts/trusted_git.py fetch <repository> [refspec...]`. The
transport CLI rejects arbitrary Git options, shell expansion, and paths outside
the current workspace. A fetch remote must be a GitHub URL or a remote name.
Git receives only an allowlisted environment.

PR creation, issue creation, comments, reviews, reactions, forks,
stars, watches, releases, and hosted state changes require active-human
consent when the operation is outward-facing or state-changing.

Repository, release, run, secret, variable, and hosted-resource deletions are
denied. Administrative merges, visibility changes, authentication changes,
token output, GraphQL mutations, and state-changing API methods require the
applicable denial or consent path.

The managed Codex sandbox may set `127.0.0.1:9` as a closed loopback proxy
placeholder. The wrappers clear only that placeholder. On a network or
unclassified failure, report proxy variable names and retry the wrapper once
before the Git fallback. Never claim a need for outside intervention while a
bounded retry remains.

`AGENTS.md` controls when linked documents conflict with it.

## Git fallback
After a failed wrapper operation, one semantically equivalent Git fallback may
run after active-human confirmation. Mark it with
`-c agents.githubFallback=confirmed`. The shell gate routes the marked command
to consent. The gate does not retain cross-process state. Human review enforces
the one-use limit.

## Checkout credentials
The four allowed exceptions permit persistence when the job:

- Pushes commits or tags.
- Pushes to another repository.
- Calls `gh` or a tool that uses the Git credential helper.
- Fetches private submodules or LFS objects.

The default `true` writes `GITHUB_TOKEN` to the runner Git configuration. Any
later step or third-party action can read it.

Check this rule before creating or modifying checkout steps. Do not refactor
unrelated workflows. For an allowed exception, retain `true` or omit the
setting. Add:

`# persist-credentials: true: this job <reason> (Rule 11 exception).`

Flag unrelated violations instead of fixing them under Rule 4.
`scripts/check_persist_credentials.py` checks the rule.

External repository acts requiring consent include PR and issue
creation, comments, reviews, reactions, forks, stars, watches, and mentions of
external accounts.

Read-only fetches, clones, checkouts, and diffs need no consent.
Other outward-facing acts require active-human consent.

`scripts/check_external_pr_refs.py` and the pre-push hook block external
autolinks. The GitHub gate routes outward-facing commands to consent. Unreadable
origin ownership asks rather than passing. Other client APIs may not observe
every hosted surface.

## Labels
Rule 23 covers these surfaces:

- `--add-label` and `--remove-label` on `gh pr edit` and `gh issue edit`
- `--label` on `gh pr create` and `gh issue create`
- `gh label` commands
- GitHub MCP tools that carry a `labels` field
- REST and GraphQL label writes

The GitHub gate denies the `gh` label options, `gh label`, and `gh api`
state-changing requests. No gate denies MCP label fields. The rule binds every
client without that coverage.

The wrapper refuses deny verdicts. It runs ask verdicts without confirming
consent. Clients without shell hooks receive no consent prompt.
