# Evidence-based reviews

Accepted scope: implement the six Speck-inspired ideas discussed for this Docker/GitHub/Codex runner. This extends the existing independent implement/review/merge/improve loops and optional Opik outcome tracking; it does not replace the runner or install Speck.

1. Publish an evidence-based review pack per reviewed PR: each acceptance criterion is pass/fail/unclear with evidence/tests, plus risks and review history. Approval requires all criteria pass and no open blockers. Bind stored approval to exact head, base, goal fingerprint and GitHub review ID; preserve existing CI, change-request and branch-protection gates.
2. Track stable structured finding IDs. Implementation responds fixed/disputed/needs_decision with evidence; the independent reviewer must adjudicate responses and preserve unresolved blockers. Responses alone cannot authorize merge.
3. Two consecutive disputes on the same finding escalate to a human hold with one question and recommendation; needs_decision escalates immediately. Persist history and allow explicit resume without bypassing gates.
4. Check small-task readiness before inference: one outcome and 1–5 explicit acceptance criteria; require splitting oversized goals or application diffs. Use conservative configurable defaults, and keep meaningful project tests rather than brittle tests of prose.
5. Optionally compare failed tests on exact head and base in isolated credential-free containers. Use identified JUnit failures, keep unknown evidence unknown, cache bounded runs, open a separate deduplicated baseline repair proposal, and keep current repair focused on regressions. Baseline failures never waive required CI; baseline-only results require maintainer intervention.
6. High-risk automatic merges require allowlisted human approval for the exact head/base/goal. Risk can be raised by sensitive paths, reviewer or labels and cannot be lowered by an agent. Retain independent review and CI requirements.

Non-goals: distributed scheduling, automatic deployment, changing authentication, bypassing server protection, automatically enabling Opik or rewriting the project architecture.

Migration: add explicit criteria to existing queued issues; renew approvals using structured reports. Persist shared SQLite state; new tables are created on startup. Auto-merge remains opt-in; baseline reproduction is disabled unless configured. This change is stacked on the unmerged Opik branch.
