# Docker Codex loops for GitHub

A reusable runner that turns labeled GitHub issues into pull requests, independently reviews them, fixes feedback/CI failures, and optionally merges them. A fourth loop uses previous results to propose improvements. GitHub calls these PRs; they are the equivalent of GitLab MRs.

## The four loops

| Service | Reads | Produces |
| --- | --- | --- |
| `implement` | Open issues labeled `agent:ready`; tracked PR review decisions, inline comments, failed CI checks and annotations | Branch `agent/issue-N`, PR, subsequent repair commits |
| `review` | Issue goal, changed files, full current source and full base source | A GitHub `APPROVE` or `REQUEST_CHANGES` review tied to the exact head SHA |
| `merge` | Reviews, check runs, commit statuses and mergeability | A squash merge only when every gate passes |
| `improve` | Last ten tracked PRs, review feedback, CI failures, fix attempt counts, current source | At most one evidence-backed improvement issue per UTC day |

All services poll independently. A shared file lock serializes controller work, including Codex jobs, to prevent collisions on one runner. SQLite stores issue-to-PR links and attempt counts across restarts. This is a single-host, single-stack starter, not a distributed worker queue. Use separate WORK_ROOT paths for different repositories.

## Start

Requires a Linux Docker host, Docker Compose v2, GitHub tokens, and an OpenAI API key. API usage is billed to that key. There is no dependency on the ChatGPT browser session.

1. Copy this directory onto a dedicated runner and `cd` into it.
2. `cp .env.example .env` and `chmod 600 .env`.
3. Set `GITHUB_REPOSITORY`, `REVIEWER_LOGIN`, token values and `OPENAI_API_KEY`.
4. Resolve a published CLI version with `npm view @openai/codex version`; put that exact version in `CODEX_VERSION`. Models are optional overrides; otherwise the pinned CLI chooses its default. Extend `Dockerfile.agent` for project-specific tools such as Rust, Java or pnpm.
5. Create the absolute `WORK_ROOT` directory on the Docker host. It must have the same absolute path inside controllers. Do not use a remote Docker daemon or Docker Desktop path translation with this setup.
6. Set `REQUIRED_CHECKS` to your exact CI job/check names, e.g. `test,lint`. Branch protection must require those same checks.
7. Create these labels in the target repository: `agent:ready`, `agent:paused`, `agent:proposal`, `agent:improvement`.
8. Build and start:

```sh
docker compose --profile build build agent-image
docker compose build implement
docker compose up -d implement review merge improve
docker compose logs -f
```

The labels can be created with GitHub's web UI or GitHub CLI:

```sh
gh label create agent:ready --repo OWNER/REPO --color 0E8A16
gh label create agent:paused --repo OWNER/REPO --color B60205
gh label create agent:proposal --repo OWNER/REPO --color D4C5F9
gh label create agent:improvement --repo OWNER/REPO --color 1D76DB
```

Create an issue with a concrete goal, acceptance criteria and useful test commands. Add `agent:ready`. The implementer opens a PR; the reviewer either approves it or requests specific changes. Fixes trigger another review on the new commit. Failed checks supply their output, links and annotations to the repair job, which can reproduce failures locally. Raw GitHub Actions log archives are not fetched in this version.

For a first run keep `AUTO_MERGE=false`. After configuring the branch rules below, set `AUTO_MERGE=true` and recreate the merge service with `docker compose up -d merge`. This is a real unattended merge capability once enabled.

## GitHub identities and permissions

The PR author and reviewer **must be distinct GitHub identities**. Separate PATs belonging to the same person are not distinct identities. Prefer dedicated bots or GitHub Apps; short-lived App tokens need an external refresh/restart mechanism because this starter reads static tokens from environment variables.

Scope tokens to only the target repository. All need metadata read; additional permissions are:

| Variable | Repository permissions |
| --- | --- |
| `IMPLEMENT_GITHUB_TOKEN` | Contents write, Pull requests write, Issues read, Checks read, Commit statuses read |
| `REVIEW_GITHUB_TOKEN` | Contents read, Pull requests write, Issues read |
| `MERGE_GITHUB_TOKEN` | Contents write, Pull requests write, Issues read, Checks read, Commit statuses read |
| `IMPROVE_GITHUB_TOKEN` | Contents read, Pull requests read, Issues write, Checks read, Commit statuses read |

The merge token must not bypass branch protection or rulesets. Review is submitted with the reviewer token; `REVIEWER_LOGIN` must match that identity exactly. Allow squash merging on the repository.

Configure and enforce server-side rules for the base branch before enabling merges:

- Require pull requests and at least one approving review; dismiss stale approvals when commits change.
- Require your CI checks and require the branch to be up to date before merging.
- Require review conversations to be resolved.
- Apply rules to the merge bot; give it no admin/ruleset bypass.

These rules are essential: the GitHub merge endpoint can bind to the head SHA, but reads of reviews, CI and base branch are separate API calls. Server-side enforcement closes the intervening race. The runner does not automatically provision or audit branch protection.

The local merge gate requires the configured bot's approval on the current SHA, no latest `CHANGES_REQUESTED` decision from any reviewer, all configured check names present and successful, every other observed check/status also successful, and clean mergeability. Missing, queued, skipped, neutral, unknown or failing checks block merges. If the same name appears in multiple check sources, every occurrence must pass. A human change request on an older commit continues to block until that person approves or an authorized maintainer dismisses it. Comments alone never clear a change request.

## Self improvement

The `improve` loop looks for recurring defects, missing tests and build/tooling friction. Each proposal includes linked evidence, a root cause, a bounded change and measurable acceptance criteria. It does not invent a task when there is insufficient evidence. It caps the backlog at three open improvement issues by default.

By default it creates `agent:improvement` + `agent:proposal` issues. Add `agent:ready` to approve implementation. Set `AUTO_QUEUE_IMPROVEMENTS=true` and recreate `improve` to queue new proposals automatically. Those issues go through the same independent review and CI gates as other work. The loop never edits its own running image or deploys a new controller.

Model suggestions cannot change merge policy. The publisher rejects `.github/`, `.codex/` and root `AGENTS.md` changes. Changes to orchestration/prompts or policy should be reviewed and deployed manually. This version learns by creating repository improvements; it does not silently rewrite prompts or perform model fine-tuning. Evaluating prompt variants against a held-out benchmark would be a separate extension.

## Controls, recovery and limits

- Add `agent:paused` to a PR to stop new review, repair and merge actions for it. The controllers recheck this before publishing results. Add it to a queued issue to stop initial implementation.
- `docker compose stop` pauses all controllers. Already spawned job containers may continue: inspect `docker ps --filter name=codex-job-` and stop those jobs separately if needed. A timeout normally removes its job container automatically.
- Initial implementation and per-commit reviews each allow three attempts. `MAX_FIX_ATTEMPTS` limits repairs across the lifetime of a PR, even across new commits. Counters persist in `WORK_ROOT/state.sqlite`. Exhausted work is left open and reported in controller logs for manual intervention; it is never auto-accepted.
- New commits invalidate approval. Repairs only respond to change requests tied to the current head, avoiding repeated repairs while waiting for re-review.
- Deterministic branch names, PR lookup, creation-only branch pushes, and force-with-lease repair pushes prevent duplicate PRs and accidental overwriting of concurrently updated branches. Recovery covers a successful push followed by a failed PR creation.
- Existing unrelated PRs and fork PRs are not adopted. Closing a tracked PR does not automatically start another PR for that issue.
- CI reruns on the same commit are reread; latest commit statuses supersede older failures. A missing pipeline remains blocked. This starter fixes completed failures, not queued jobs or infrastructure outages; inspect those manually.
- One model job times out after `JOB_TIMEOUT_SECONDS` (default 30 minutes), with 2 CPUs and 4 GiB RAM. These bound execution, not dollar spend; set project-level API budgets/rate limits separately.
- `WORK_ROOT/job-*/output/` retains model logs and summaries. The reviewer gets source read-only and can compare `/repo` to `/base`. Jobs have no GitHub token, no Docker socket, and no controller state mount. Logs and source may contain sensitive repository data; retain/prune them according to your needs. No automatic disk cleanup is included.
- Agents run in disposable containers with dropped capabilities and no host networking. Their writable source has no controller Git metadata. GitHub pushes/reviews/merges happen only in trusted controllers. OpenAI credentials and outbound network are available in jobs; use a limited project key. Docker containers are not a hardened hostile-code sandbox. Controllers with the Docker socket have host-level power: use a dedicated runner, not your everyday workstation or production host. Network egress restrictions and microVM isolation are deployment extensions.
- A hard host/controller crash can leave orphan job containers. Stop those before restarting work. The file lock is intentionally local and does not coordinate separate runner hosts.

## Verification

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile loop.py
docker compose config --quiet
```

The automated suite exercises review/check gates, pagination, durable attempt limits, merge controller decisions, SHA race rejection, and real local Git publication. Docker builds, Codex inference and GitHub side effects need a configured integration trial on the runner. No tokens or live repository mutations are included in the package.

Official references:

- [Codex non-interactive execution and structured output](https://learn.chatgpt.com/docs/non-interactive-mode)
- [GitHub pull request reviews](https://docs.github.com/en/rest/pulls/reviews)
- [GitHub merge API and expected SHA](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)
- [GitHub check runs](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference)
