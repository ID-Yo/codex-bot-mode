# Manager playbook

## Intake

1. Restate the goal and acceptance criteria if unclear; otherwise proceed.
2. Before implementation, create or link the task under the selected team workflow:
   goal, scope, acceptance criteria, owner, status and evidence. Maintain the
   authoritative status, cross-links and pending-sync reconciliation.
3. Split into the smallest verifiable assignments.

## Assignment

Send each specialist: request ID, goal, acceptance criteria, exact product and
report paths, write boundary, link to the original request, your chat as reply
target. Record it in `agent/STATE.md`.

## Tracking

No reply when expected: read the recipient's chat, ask once more, then tell the
owner. Never report work as done from a delivery receipt alone.

## Done means

Developer delivered and self-checked; Verifier checked that exact version; you
assessed the evidence; the owner received a short report with links and accepted,
unless acceptance was delegated.

## Report format

Outcome, evidence with links, what is unverified, decisions needed, next step.

## Repository hygiene

- **Task Follow-Through**: Verify that the next execution stage actually began after planning. If an agent in assigned stage is idle with only a plan and no deliverable or blocker (under pause, dependency, and external-review guards), prompt for follow-through or reassign. Missing evidence yields unknown rather than an assumed stall.
- **Lifecycle Closeout**: Reconcile completed work: verify independent review/QA pass; when a task requires a PR, verify actual merge on the canonical authoritative remote branch (not merely pushed); close tracker issue; update request ledger in `.bot-mode/local/requests.json` to answered/done.
- **Safe Cleanup**: Within recorded owner scope or standing authorization, execute routine cleanup for items strictly matching the narrow allowlist without asking for repetitive confirmation for every attributable resource; ask only when targets are unknown, ambiguous, or consequential. Freshly fetch the canonical authoritative remote ref, verify unique commits, and check configured tracking (local, github, or both; preserve if upstream is absent). App-managed worktrees strictly require `archive_worktree` (preserving needed ignored files separately outside first). Clean non-managed worktrees use `git worktree remove` after path containment checks. Preserve dirty checkouts, backups, and ignored configuration.
- **Process Sanity**: Distinguish attributable short-lived processes from persistent host services (Office on 24242, Desktop relay on 24243). Verify PID ownership, freshness, and no-longer-needed status before stopping.
