# Docker Codex loops for GitHub

A reusable runner that turns labeled GitHub issues into pull requests, independently reviews them, fixes feedback/CI failures, and optionally merges them. An opt-in fourth loop uses previous results to propose improvements. GitHub calls these PRs; they are the equivalent of GitLab MRs.

One implementation agent owns planning, coding, debugging and validation for each job. The reviewer runs in a separate disposable context and should use a different model where practical. Merging uses ordinary controller code, with no model call. Extra agents are reserved for independent investigation or isolated parallel work, rather than permanent planning/testing/documentation roles. Each model job still starts fresh; this does not introduce session resumption across repairs.

## The four loops

| Service | Reads | Produces |
| --- | --- | --- |
| `implement` | Open issues labeled `agent:ready`; tracked PR review decisions, inline comments, failed CI checks and annotations | Branch `agent/issue-N`, PR, subsequent repair commits |
| `review` | Issue goal, compact changed-file manifest, full current source and full base source on disk | A GitHub `APPROVE` or `REQUEST_CHANGES` review tied to both head and base SHAs |
| `merge` | Reviews, check runs, commit statuses and mergeability | A squash merge only when every gate passes |
| `improve` (opt-in) | Last ten tracked PRs, review feedback, CI failures, fix attempt counts, recent job metrics, current source | At most one evidence-backed improvement issue per UTC day |

All services poll independently. A shared file lock serializes controller work, including Codex jobs, to prevent collisions on one runner. SQLite stores issue-to-PR links and attempt counts across restarts. This is a single-host, single-stack starter, not a distributed worker queue. Use separate WORK_ROOT paths for different repositories.

## Start

Requires a Linux Docker host, Docker Compose v2, GitHub tokens, and either a Codex CLI sign-in or an OpenAI API key. The default `AUTH_MODE=codex` uses a dedicated saved Codex sign-in; it does not reuse this chat or the browser session automatically.

1. Copy this directory onto a dedicated runner and `cd` into it.
2. `cp .env.example .env` and `chmod 600 .env`.
3. Set `GITHUB_REPOSITORY`, `REVIEWER_LOGIN`, token values. Choose authentication as described below; `OPENAI_API_KEY` is optional in Codex sign-in mode.
4. Resolve a published CLI version with `npm view @openai/codex version`; put that exact version in `CODEX_VERSION`. Set `IMPLEMENT_MODEL` and `REVIEW_MODEL` independently; prefer a different reviewer model. Overrides must be available through your pinned Codex CLI and account; this version does not launch other vendors' CLIs. Blank values use the CLI default. `IMPROVE_MODEL` optionally selects a cheaper model for proposals and falls back to `REVIEW_MODEL`. Extend `Dockerfile.agent` for project-specific tools such as Rust, Java or pnpm.
5. Create the absolute `WORK_ROOT` directory on the Docker host. It must have the same absolute path inside controllers. Do not use a remote Docker daemon or Docker Desktop path translation with this setup.
6. Set `REQUIRED_CHECKS` to your exact CI job/check names, e.g. `test,lint`. Branch protection must require those same checks.
7. Create these labels in the target repository: `agent:ready`, `agent:paused`, `agent:proposal`, `agent:improvement`.
8. Build and start:

```sh
docker compose --profile build build agent-image
docker compose build implement
# One-time sign-in for AUTH_MODE=codex:
docker compose --profile auth run --rm login
# Complete the device-login link and code in your own browser.
docker compose --profile auth run --rm login login status

docker compose up -d implement review merge
docker compose logs -f
```

Self-improvement is excluded from the default startup. To enable it, set `IMPROVE_GITHUB_TOKEN` and run `docker compose --profile improve up -d improve`. Stop an already running improvement service with `docker compose --profile improve stop improve`; profiles do not stop containers from an older deployment automatically.

The labels can be created with GitHub's web UI or GitHub CLI:

```sh
gh label create agent:ready --repo OWNER/REPO --color 0E8A16
gh label create agent:paused --repo OWNER/REPO --color B60205
gh label create agent:resume --repo OWNER/REPO --color 0E8A16
gh label create agent:proposal --repo OWNER/REPO --color D4C5F9
gh label create agent:improvement --repo OWNER/REPO --color 1D76DB
```

Create an issue with a concrete goal, acceptance criteria and useful test commands. Add `agent:ready`. The implementer opens a PR; the reviewer either approves it or requests specific changes. Fixes trigger another review on the new commit. Failed checks supply their output, links and annotations to the repair job, which can reproduce failures locally. Raw GitHub Actions log archives are not fetched in this version.

For a first run keep `AUTO_MERGE=false`. After configuring the branch rules below, set `AUTO_MERGE=true` and recreate the merge service with `docker compose up -d merge`. This is a real unattended merge capability once enabled.

## Codex credentials or API key

`AUTH_MODE=codex` uses your ChatGPT/Codex account instead of an API key. The one-time `login` service runs `codex login --device-auth`; enable device-code sign-in in your ChatGPT security settings if needed. Complete the printed sign-in link and code yourself. Never paste credentials, the device code or `auth.json` into GitHub issues or chat.

The dedicated Docker volume named by `CODEX_AUTH_VOLUME` stores the CLI auth cache. Disposable implementation, review and improvement jobs mount that same volume writable so Codex can persist token refreshes for the next job. The global controller lock serializes these jobs. Sign-in usage counts against your account's Codex allowance and can hit plan limits; it is not unlimited. Availability depends on your plan and workspace settings.

If device login is unavailable, stop the loops and authenticate locally with file-based storage (`codex -c 'cli_auth_credentials_store="file"' login`). Transfer only that dedicated login's `auth.json` securely into the volume, owned by UID 1000 with mode 600. Do not mount your entire personal Codex home directory. A local OS keychain login is not automatically available inside Docker. The device-login path avoids copying credentials altogether.

For usage-based API billing set `AUTH_MODE=api` and `OPENAI_API_KEY` in `.env`, then recreate `implement`, `review` and `improve`. API mode does not mount the account credential volume. Authentication never silently falls back between modes.

For renewal or logout, stop active controllers and jobs first, then run:

```sh
docker compose stop
docker compose --profile auth run --rm login login status
docker compose --profile auth run --rm login login --device-auth
# To revoke the saved runner login instead:
# docker compose --profile auth run --rm login logout
```

Keep the volume between restarts; `docker compose down -v` deletes the saved login. Protect volume backups as credentials. Do not run another stack or desktop CLI concurrently against this credential cache; use a distinct dedicated login/volume per runner. GitHub tokens are still required separately. Official OpenAI guidance recommends API keys as the default for programmatic CI/CD; saved account auth is an advanced option for trusted runners.

## GitHub identities and permissions

The PR author and reviewer **must be distinct GitHub identities**. Separate PATs belonging to the same person are not distinct identities. Prefer dedicated bots or GitHub Apps; short-lived App tokens need an external refresh/restart mechanism because this starter reads static tokens from environment variables.

Scope tokens to only the target repository. All need metadata read; additional permissions are:

| Variable | Repository permissions |
| --- | --- |
| `IMPLEMENT_GITHUB_TOKEN` | Contents write, Pull requests write, Issues write, Checks read, Commit statuses read |
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

The local merge gate requires the configured bot's approval on the current head and base SHA pair, no latest `CHANGES_REQUESTED` decision from any reviewer, all configured check names present and successful, every other observed check/status also successful, and clean mergeability. Reviews include a controller-generated `codex-review` marker identifying that pair. Base changes invalidate review, even if the PR head did not change; pre-upgrade reviews without the marker must be renewed. Missing, queued, skipped, neutral, unknown or failing checks block merges. If the same name appears in multiple check sources, every occurrence must pass. A human change request on an older commit continues to block until that person approves or an authorized maintainer dismisses it. Comments alone never clear a change request.

## Workflow state and visible progress

`workflow.py` and the controllers in this repository own the state definitions, transitions, retries and escalation rules. The target repository supplies goals, acceptance criteria, project instructions and CI. The runner's SQLite database stores current workflow state and append-only transition history. No workflow source files are copied into target repositories.

Each observed transition records the goal issue, optional PR, state, reason, next owner, next action, revision context where available, and UTC timestamp. Repeated identical observations preserve the original timestamp instead of adding duplicate events. The implementation controller maintains one progress comment on the goal issue and, once created, one on the work PR. It edits those comments as progress changes. This requires **Issues write** on the implementation token, including when upgrading an existing runner. Review and merge identities only update local state; the implementation service publishes those updates on its next poll. Model-job containers still have no GitHub credentials.

| State | Meaning / next owner |
| --- | --- |
| `queued` | Explicitly resumed; implementation will re-evaluate current conditions |
| `implementing`, `repair_pending`, `repairing` | Implementation owns the active or upcoming work |
| `waiting_for_ci` | Required checks are missing, pending or not successful; CI or a maintainer must investigate |
| `ci_failed` | Completed checks failed; diagnostics need investigation |
| `waiting_for_review`, `reviewing` | The independent reviewer owns the next step |
| `changes_requested` | Implementation must address findings or ask for a human decision |
| `needs_clarification` | A maintainer must answer specific missing requirements |
| `needs_human_decision` | A maintainer must resolve a disputed finding, operational blocker or outstanding older change request |
| `retry_pending`, `retry_exhausted` | A bounded attempt failed; the controller retries, or a maintainer must intervene once exhausted |
| `paused` | Work is explicitly paused or no longer eligible |
| `waiting_for_mergeability`, `ready_to_merge` | GitHub requirements must be checked; the merge service or a maintainer owns the next action |
| `merged`, `closed` | Terminal PR outcome; deployment is tracked separately |

Implementation and repair jobs return structured outcomes: `completed`, `needs_clarification`, or `needs_human_decision`. A stopping outcome never publishes the job's partial source changes. A clarification hold resumes when the source issue's title/body changes; comments and timestamp changes alone do not resume it. A human-decision hold requires an explicit resume. Both types block further repair, independent review and automatic merging until cleared.

To resume a hold or exhausted attempt cycle, add **`agent:resume`** to the source issue or work PR. The implementation controller consumes the label and resets only that goal's implementation, repair and review attempt counters and repair fingerprints. The source issue must remain eligible (`agent:ready` before initial implementation); remove `agent:paused` separately. Resuming does not approve anything or bypass CI, reviews, revision freshness or branch protection. For an older human change request, the original reviewer must still approve or an authorized maintainer must dismiss it.

Comment updates are retried after API failures and do not interrupt implementation. A marker persisted before comment creation lets the runner recover a successful POST whose response was lost, rather than creating another comment. Updating the same state does not publish another comment. Review progress may remain at `waiting_for_review` during a job because the single-host lock serializes controllers; local history still records the review interval.

To inspect a goal's history and observed wall time by state, run from a checkout on the runner (adjust the path and issue number):

```python
from pathlib import Path
from loop import State

state = State(Path('/opt/codex-loops/work'))
print(state.workflow.history(123))
print(state.workflow.durations(123))
state.db.close()
```

Durations reflect controller observations and polling, including time waiting between handoffs; they are not exact CI execution timings. Terminal states do not keep accumulating delivery time. Recent PR evidence sent to self-improvement includes transition history and state durations alongside model-job metrics. These observations can identify whether the actual bottleneck is coding, CI, review or a human decision before adding more automation.

## Self improvement

The opt-in `improve` loop looks for recurring defects, missing tests and build/tooling friction. Clean history with no change requests, failed checks or repair attempts skips model inference entirely. Each proposal includes linked evidence, a root cause, a bounded change and measurable acceptance criteria. It does not invent a task when there is insufficient evidence. It caps the backlog at three open improvement issues by default.

SQLite's `model_jobs` table records role, configured model, prompt bytes, elapsed seconds and success/failure for each model invocation, including failures. It stores no prompt or response content. The improvement job receives aggregates from the latest 100 invocations alongside PR/CI outcomes. Prompt bytes and elapsed time are cost proxies, not billed tokens or money; an invocation's success means valid output, not that the code was correct. Blank model overrides are recorded as `<cli-default>`. Existing state databases gain this table automatically. To inspect metrics on the runner:

```sh
docker compose run --rm --no-deps --entrypoint python merge -c 'import os; from pathlib import Path; from loop import State; print(State(Path(os.environ["WORK_ROOT"])).metrics())'
```

By default it creates `agent:improvement` + `agent:proposal` issues. Add `agent:ready` to approve implementation. Set `AUTO_QUEUE_IMPROVEMENTS=true` and recreate `improve` to queue new proposals automatically. Those issues go through the same independent review and CI gates as other work. The loop never edits its own running image or deploys a new controller.

Model suggestions cannot change merge policy. The publisher rejects `.github/`, `.codex/` and root `AGENTS.md` changes. Changes to orchestration/prompts or policy should be reviewed and deployed manually. This version learns by creating repository improvements; it does not silently rewrite prompts or perform model fine-tuning. Evaluating prompt variants against a held-out benchmark would be a separate extension.

## Controls, recovery and limits

- Add `agent:paused` to a PR to stop new review, repair and merge actions for it. The controllers recheck this before publishing results. Add it to a queued issue to stop initial implementation.
- `docker compose stop` pauses all controllers. Already spawned job containers may continue: inspect `docker ps --filter name=codex-job-` and stop those jobs separately if needed. A timeout normally removes its job container automatically.
- Initial implementation and reviews for each head/base pair each allow three attempts. `MAX_FIX_ATTEMPTS` limits repairs across the lifetime of a PR, even across new commits. Counters persist in `WORK_ROOT/state.sqlite`. Exhausted work is left open and reported in controller logs for manual intervention; it is never auto-accepted.
- New head or base commits invalidate approval. Dismissed bot reviews are eligible for re-review. Repairs only respond to change requests tied to the current head, avoiding repeated repairs while waiting for re-review.
- Deterministic branch names, PR lookup, creation-only branch pushes, and force-with-lease repair pushes prevent duplicate PRs and accidental overwriting of concurrently updated branches. Recovery covers a successful push followed by a failed PR creation.
- Existing unrelated PRs and fork PRs are not adopted. Closing a tracked PR does not automatically start another PR for that issue.
- CI reruns on the same commit are reread; latest commit statuses supersede older failures. A missing pipeline remains blocked. This starter fixes completed failures, not queued jobs or infrastructure outages; inspect those manually.
- One model job times out after `JOB_TIMEOUT_SECONDS` (default 30 minutes), with 2 CPUs and 4 GiB RAM. These bound execution, not dollar spend; set project-level API budgets/rate limits in API mode, or monitor your Codex plan allowance in account mode.
- `WORK_ROOT/job-*/output/` retains model logs and summaries. The reviewer gets source read-only and can compare `/repo` to `/base`. Jobs have no GitHub token, no Docker socket, and no controller state mount. Logs and source may contain sensitive repository data; retain/prune them according to your needs. No automatic disk cleanup is included.
- Agents run in disposable containers with dropped capabilities and no host networking. Their writable source has no controller Git metadata. GitHub pushes/reviews/merges happen only in trusted controllers. Model credentials and outbound network are available in jobs: the dedicated saved sign-in in Codex mode, or a limited project key in API mode. Docker containers are not a hardened hostile-code sandbox. Controllers with the Docker socket have host-level power: use a dedicated runner, not your everyday workstation or production host. Network egress restrictions and microVM isolation are deployment extensions.
- A hard host/controller crash can leave orphan job containers. Stop those before restarting work. The file lock is intentionally local and does not coordinate separate runner hosts.

## Verification

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile loop.py
docker compose config --quiet
```

The automated suite exercises review/check gates, pagination, durable attempt limits, merge controller decisions, SHA race rejection, real local Git publication, persisted workflow history and durations, clarification/human-decision holds, explicit resume, structured implementation output, progress-comment recovery, and failure isolation. Docker builds, Codex inference and GitHub side effects need a configured integration trial on the runner. No tokens or live repository mutations are included in the test suite.

Official references:

- [Codex authentication, device login and credential storage](https://learn.chatgpt.com/docs/auth)
- [Codex non-interactive execution and structured output](https://learn.chatgpt.com/docs/non-interactive-mode)
- [GitHub pull request reviews](https://docs.github.com/en/rest/pulls/reviews)
- [GitHub merge API and expected SHA](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)
- [GitHub check runs](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference)
