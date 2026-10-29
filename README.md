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

## Optional Opik tracing and outcome tracking

The controllers can export metadata-only traces to a self-hosted Opik instance or Opik Cloud. This is optional and disabled by default; no Opik service or SDK is required to run the loops.

Configure `.env` after you have an Opik endpoint reachable from the controller containers:

```dotenv
OPIK_ENABLED=true
OPIK_URL_OVERRIDE=http://opik.your-lan:5173/api
OPIK_PROJECT_NAME=codex-loops
```

For Opik Cloud use `OPIK_URL_OVERRIDE=https://www.comet.com/opik/api`, plus your `OPIK_API_KEY` and `OPIK_WORKSPACE`. Keep credentials in your local `.env`. Rebuild and recreate the controllers (`docker compose up -d --build implement review merge`); recreate the optional `improve` service too if enabled. An address such as `localhost:5173` inside a controller refers to that container, not the Docker host. This change connects to Opik; it does not deploy the Opik server.

Each model job emits a trace containing its role, configured model (or `<cli-default>`), duration, prompt byte count and SHA-256 hash, execution outcome, and validated decision/outcome enums. The controller source fingerprint identifies the implementation and its embedded prompts; the full prompt hash is task-specific, not a standalone prompt-template version. Jobs with an active issue/PR include their issue, PR and bounded attempt number. Current head/base SHAs are included when the workflow observation provides them.

Workflow changes emit separate traces, including CI failure, waiting for review, ready to merge, retry exhaustion, merged, and closed. Job and workflow traces share a thread per repository/issue, so repair/review rounds can be inspected together. Polls that observe the same state do not create duplicates. Improvement jobs are repository-wide rather than tied to one issue.

Numeric feedback scores distinguish execution success, the implementer's reported completion, the model review verdict, observed CI readiness/failure and confirmed merge/closure. A model's successful execution is not evidence that tests passed. A model approval is not a GitHub approval: the existing controllers still submit and verify the actual review. Merging is not deployment or proof that a change has no defects. No tokens, monetary costs or test pass counts are invented from prompt size or wall time.

Raw prompts, issue titles/bodies, source code, summaries, review findings, CI logs, exception messages and credential values are **not exported**. Free-text reasons and next actions remain on the runner/GitHub. Opik configuration and keys stay in controllers; job containers receive no Opik settings. Disable export with `OPIK_ENABLED=false` and recreate controllers.

A SQLite outbox in the existing runner database preserves pending exports across restarts. Each iteration attempts at most 50 traces, then their numeric scores, using `OPIK_TIMEOUT_SECONDS` per HTTP request (default 2, capped at 10). Unavailable endpoints, authentication errors or invalid configuration do not change job results or GitHub merge gates. Failed exports keep stable UUIDv7 IDs for retry; a lost acknowledgement can resend an existing trace/score. Export is best-effort, not exactly-once delivery. Redirects are rejected rather than forwarding credentials or traces to another URL.

The queue holds at most `OPIK_MAX_PENDING` pending traces (default 10,000). When full, new telemetry is dropped with a warning so runner disk usage cannot grow indefinitely from Opik alone. Enabled workflow history is collected in bounded batches; existing history from before first enablement is not backfilled. A slow endpoint can add up to two HTTP timeouts to a poll. A hard crash during a model job may leave that job without a completed trace. Pending rows retain their original project name, but delivery uses the currently configured endpoint/workspace: drain or clear the pending queue before switching destinations. Disabled tracing retains pending rows without transmitting them.

This first integration provides measurements and failure examples. It does not add automatic prompt optimization or benchmark replay. Those require curated tasks, independent acceptance tests, and held-out comparisons before proposing prompt changes through normal PR review.

API references: [trace batch ingestion](https://www.comet.com/docs/opik/reference/rest-api/traces/create-traces), [trace feedback scores](https://www.comet.com/docs/opik/reference/rest-api/traces/score-batch-of-traces), and [REST configuration](https://www.comet.com/docs/opik/reference/rest-api/overview).

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
| `needs_human_approval` | High-risk merge policy is waiting for exact-revision human approval |
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

## Evidence-based reviews and bounded feedback

Queue one outcome per issue with **one to five** explicit acceptance criteria:

```markdown
## Outcome
Exports survive a worker restart.

## Acceptance criteria
- Restarting a worker preserves queued exports.
- Retrying an export does not create a duplicate file.
```

`agent:ready` issues without this section stop for clarification before a model job. The title is the outcome when `## Outcome` is omitted. Goals over `MAX_GOAL_CHARS` (default 8000), more than five criteria, `agent:large`, or `## Size` set to `L` require splitting. The agent also checks whether the goal has one outcome. A publish exceeding `MAX_CHANGE_LINES` (default 300 added/deleted application lines across the cumulative PR diff against its base, excluding common test paths and lockfiles) stops for a human split decision before pushing. Binary changes still require review and the risk policy.

The reviewer publishes a review pack containing each criterion's `pass`, `fail` or `unclear` result, concrete evidence, tests/checks, risk assessment, stable finding IDs and recent review/response history. An approval requires every criterion to pass and every blocking finding to be resolved; minor findings remain visible. The report is stored in SQLite and bound to the issue title/body fingerprint, PR head, base and actual GitHub review ID. A source change, base change, goal edit or new implementation response requires fresh independent review. Existing approvals without a stored structured report must be renewed after upgrading. Preserve the shared state database across controller restarts.

Repairs respond to every current blocking finding with `fixed`, `disputed` or `needs_decision` and evidence. The reviewer independently adjudicates those responses; an implementation response never approves itself. A blocker cannot silently disappear or be downgraded. A second consecutive dispute about the same finding, or any `needs_decision`, holds the goal with one question and a recommendation. Findings and responses are retained across restarts, including response-only disputes with no source commit. `agent:resume` resets the dispute cycle and forces fresh independent review before further repair. Automatic retries cannot overwrite a persisted response to the same review record.

The improvement loop receives structured review-round, blocker and recent dispute counts alongside its existing evidence. It still produces proposals under the existing daily/open-issue limits; policy changes follow the ordinary review and merge gates.

## High-risk merge approval

Auth, secrets, payments, migrations, infrastructure and other built-in sensitive file paths require human approval for automatic merging. The reviewer or an `agent:high-risk` label on either issue or PR can also raise risk. `HIGH_RISK_PATHS` adds comma-separated glob patterns; it cannot remove built-in classifications. Once recorded, raised risk persists for the PR across subsequent reviews, revisions and goal edits. A low-risk label or later low-risk model verdict cannot lower it.

Set `RISK_APPROVERS` to comma-separated **human GitHub logins**. The approver must also currently have repository write, maintain or admin permission and must differ from `REVIEWER_LOGIN`. After inspecting the change, that person posts the exact command displayed in the PR review pack (or progress action):

```text
/agent approve-risk <full-head-sha> <full-base-sha> <goal-fingerprint>
```

An empty allowlist blocks high-risk automatic merges. Edited goals and changed head/base revisions invalidate the command. CI, independent approval, absence of change requests and branch protection remain required. The runner records `needs_human_approval` while waiting; the exact command unblocks that gate without `agent:resume`. These are separately read GitHub resources, so retain the server-side protections described above; issue/comment changes cannot be checked atomically with the merge API.

## Comparing CI failures with the base

Optionally set `BASELINE_TEST_COMMAND` to a trusted deployment-configured test command, for example `python -m pytest --junitxml=/out/tests.xml`. Build an agent image containing the project's test tools and dependencies first. The command must produce JUnit XML at `/out/tests.xml`; project commands are not automatically inferred from issue text.

When repairing failed CI, the controller runs that same command on both exact revisions in separate disposable containers, with fresh result directories, resource/time limits, and no Codex, GitHub or Opik credential mounts/environment. `BASELINE_TIMEOUT_SECONDS` defaults to 600 per revision. Results, including timeouts or unknown comparisons, are cached per head/base and command/image fingerprint to bound repeated work. Pin the image reference when reproducibility matters.

Test identity uses suite/class/name. New failing IDs are regressions. Matching failures are classified as baseline-only only if every executed base test also ran on the head. Missing, malformed, inconsistent or skipped-away evidence stays unknown; a passing reproduction does not clear failing GitHub CI.

Reproduced base failures create a separate `agent:proposal` repair issue, deduplicated by base revision and command/image fingerprint, with recovery after a lost create response. Mixed failures keep the current repair focused on regressions. Baseline-only failures hold the current goal for a maintainer decision about queuing that separate issue. They never waive CI, autoqueue baseline work or claim to identify the introducing commit. After the base is fixed, resume the original goal to obtain fresh review and CI.
