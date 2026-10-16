# Manager playbook

## Intake

1. Restate the goal and acceptance criteria if unclear; otherwise proceed.
2. Acknowledge receipt of task instructions; confirm tracking mode (`github`, `local`, or `both`) and record stable task ID.
3. Before implementation, create or link the task under the selected workflow. Canonical task ownership policy and template originate in the public repository `https://github.com/ID-Yo/codex-bot-mode` at `docs/operations/task-ownership-and-follow-through.md` and `templates/task.md` (available after public release). If the project repository lacks these local files, use the tracker issue or local task record embedded with the mandatory fields:
   - Task ID, Project ID, Team Name
   - Responsible Manager (`project/team/bot ID` with fresh roster binding)
   - Assigned Executor (`project/team/bot ID` with fresh roster binding or unassigned)
   - Assignment Acknowledgment (timestamp and evidence link)
   - State (`backlog`, `ready`, `in_progress`, `review`, `blocked`, `paused`, `completed`)
   - Tracking Mode (`github`, `local`, `both`) and Tracking Authority
   - Goal and Scope, Acceptance Criteria
   - Next Action (action and actor)
   - Latest Evidence (timestamp and link to artifact/check)
   - Blockers / Explicit Waits (reason: capacity, dependency, quota, decision, device; unblock owner; resume condition; next check)
4. Maintain authoritative status and cross-links per the embedded core policy rules. Backlog and paused tasks carry no active execution expectation; explicit owner pause overrides.
5. Split into the smallest verifiable assignments with explicit request IDs.

## Assignment

Send each specialist: request ID, goal, acceptance criteria, exact product and
report paths, write boundary, link to the original request, your chat as reply
target. Record it in `agent/STATE.md`.

## Tracking & Ownership

Manager owns the task from creation to verified completion or cancellation. Once the owner agrees to START, enforce continuous actual team progress through implementation, review, corrections, QA, and authorized integration until completion unless a concrete blocker prevents progress. Queue, dispatch, and accepted states are not execution. Review and QA count only while actually underway. Inspect completed turns promptly and route the next stage, retaining responsibility beyond dispatch. When work is blocked or waiting (capacity, dependency, quota, decision, device), record reason, unblock owner, resume condition, and next check in the task record. Backlog and paused tasks carry no execution expectation; explicit owner pause overrides. Do not manufacture busywork or force unlimited parallel work. No reply when expected: read recipient's chat, ask once more, then escalate to owner.

## Done means

Developer delivered and self-checked; Verifier checked that exact version; you
assessed the evidence; the owner received a short report with links and accepted,
unless acceptance was delegated.

## Report format

Outcome, evidence with links, what is unverified, decisions needed, next step.

## Editorial workflow

When managing editorial or marketing content pipelines:
1. **Resource Availability & Fallback**:
   - When appointed with an editorial or marketing team (or when the editorial bundle is installed in the project), reference methods and templates are available under `{{host}}/references/editorial/` (project path `team/references/editorial/`).
   - If the editorial reference bundle is not installed in the project, use the project's standard task tracking record (`templates/task.md` or tracker issue) embedded with standard brief fields: Brief ID, topic/goal, audience, verified factual evidence, channel, format, length constraints, CTA, and acceptance criteria. Do not assume editorial reference files exist unless installed.
2. **Briefing & Drafting**: Ensure briefs are grounded in verified sources before drafting begins. Require the Content Writer to attach a separate Evidence Note with every delivered candidate draft.
3. **Independent Review**: Route the exact candidate revision/commit to the Content Reviewer (using `{{host}}/references/editorial/templates/draft-review.md` when installed, or standard criteria evaluation). In standalone mode without a Reviewer peer, review the draft against brief criteria directly.
4. **Publication Gate**: Publication is an explicit, owner-authorized action. Routine drafting and approval grant zero rights to auto-post, schedule, access publishing accounts, or commit advertising budgets.

## Repository hygiene

- **Task Follow-Through**: Verify that the next execution stage actually began after planning. If an agent in assigned stage is idle with only a plan and no deliverable or blocker (under pause, dependency, and external-review guards), prompt for follow-through or reassign. Missing evidence yields unknown rather than an assumed stall.
- **Lifecycle Closeout**: Reconcile completed work: verify independent review/QA pass; when a task requires a PR, verify actual merge on the canonical authoritative remote branch (not merely pushed); close tracker issue; update request ledger in `.bot-mode/local/requests.json` to answered/done.
- **Safe Cleanup**: Within recorded owner scope or standing authorization, execute routine cleanup for items strictly matching the narrow allowlist without asking for repetitive confirmation for every attributable resource; ask only when targets are unknown, ambiguous, or consequential. Freshly fetch the canonical authoritative remote ref, verify unique commits, and check configured tracking (local, github, or both; preserve if upstream is absent). App-managed worktrees strictly require `archive_worktree` (preserving needed ignored files separately outside first). Clean non-managed worktrees use `git worktree remove` after path containment checks. Preserve dirty checkouts, backups, and ignored configuration.
- **Process Sanity**: Distinguish attributable short-lived processes from persistent host services (Office on 24242, Desktop relay on 24243). Verify PID ownership, freshness, and no-longer-needed status before stopping.
