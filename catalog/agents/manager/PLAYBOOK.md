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
Every incoming agent verdict must be handled in the same turn: start the next executor or record a blocker. A review handoff without the exact candidate SHA and author recorded on the task is invalid.

## Stage handoff and ready work

- Every finished stage arrives as a stage completion record on the task issue (or local record) plus the executor's reply: stage, executor, result, evidence with exact candidate SHA and author, checks and limitations, proposed next responsible and action. In the same turn dispatch the next scoped request and record its request ID, delivery ID and acknowledgment on the task, or record a blocker issue. A stage finish never closes the issue.
- Approval to start is a recorded act: start_approved plus who, when and scope (a start:approved label only with a reference to the owner instruction or standing delegation). Backlog or a ready label is not approval. In the same processing turn in which you find approved ready work and an eligible idle executor (appointed for the role and scope, no owner pause or single-writer conflict, activity evidence fresh within one audit interval), dispatch it; otherwise record the reason (capacity, dependency, authorization, quota, device), unblock owner, resume condition and next check. Check this at every completed turn and every Monitor signal. Unknown activity is not idle; owner pause, single writers and permissions override.
- Intermediate pull requests link the issue with Refs; Closes only on the final accepted deliverable. A request reply with outcome done closes that request, not the issue. Keep every open work issue labelled with its state and a responsible participant.
- These are operating rules; the Monitor does not yet detect unlabelled open issues, an open issue whose last request is done, or ready work without a dispatch.

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
Closing an issue includes closing its linked request record in `.bot-mode/requests.py` in the same turn. A `partial` reply to your request keeps the Monitor signalling until you accept it with its follow-up: `requests.py accept <id> --by <you> --link <follow-up>`.
- **Safe Cleanup**: Within recorded owner scope or standing authorization, execute routine cleanup for items strictly matching the narrow allowlist without asking for repetitive confirmation for every attributable resource; ask only when targets are unknown, ambiguous, or consequential. Freshly fetch the canonical authoritative remote ref, verify unique commits, and check configured tracking (local, github, or both; preserve if upstream is absent). App-managed worktrees strictly require `archive_worktree` (preserving needed ignored files separately outside first). Clean non-managed worktrees use `git worktree remove` after path containment checks. Preserve dirty checkouts, backups, and ignored configuration.
- **Process Sanity**: Distinguish attributable short-lived processes from persistent host services (Office on 24242, Desktop relay on 24243). Verify PID ownership, freshness, and no-longer-needed status before stopping.
