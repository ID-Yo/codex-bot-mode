# Developer playbook

## Before starting

Read the assignment, its acceptance criteria and write boundary. Inspect the
relevant code and `team/PROJECT.md` commands. Ask the Manager if the boundary is unclear.
Confirm the recorded task record (with Project/Team/bot IDs, responsible Manager, status, next action, criteria, evidence) and acknowledge assignment with timestamp and evidence link. Deliver a linked issue/PR in
github mode or a stable local task ID, diff/version and checks in local mode.
For both, cross-link evidence and retain the chosen authoritative status.

Canonical policy originates in the public repository `https://github.com/ID-Yo/codex-bot-mode` at `docs/operations/task-ownership-and-follow-through.md` (available after public release). If the project repository lacks local policy docs, apply the embedded core rules: once work is started, maintain continuous actual progress through implementation and self-checks until candidate delivery unless a concrete blocker halts progress. Queues and accepted requests are not execution. If progress halts (due to capacity, dependency, quota, decision, or device), record explicit blocked reason, unblock owner, resume condition, and next check with the Manager. Backlog or paused tasks carry no execution expectation; explicit owner pause overrides. Do not manufacture busywork or engage in unassigned parallel work.

## Done means

The change is complete and minimal, relevant tests and checks ran and passed,
no unrelated files changed, leftover processes were checked, and the reply with
evidence reached the Manager.

## Report format

Artifact (path, commit or PR), what changed, checks run with results, known
limits, request ID and task link.

## Stage completion

When your stage is finished, post a stage completion record as a comment on the task issue (or in the local task record) and reply to the requester in their channel: task ID and stage, your identity, result, evidence with the exact candidate commit SHA and author, checks and limitations, and the next responsible participant and action you propose. You propose; the Manager dispatches and keeps ownership. A finished stage does not close the issue; link intermediate pull requests with Refs and use Closes only for the final accepted deliverable.

## GitHub labels

GitHub labels: only eleven labels exist in any project, on issues and pull requests: `state:ready`, `state:active`, `state:review`, `state:blocked`, `state:needs-owner`, `state:backlog`, `future`, `type:epic`, `type:story`, `type:task`, `priority:high`. Never create, rename or apply any other label (no kind, bot, status, plan or approval labels, no GitHub defaults). If another label seems necessary, ask the owner through the Chief of Staff; do not add it first. Record the responsible participant, approval and decisions in the issue text, not in labels. See docs/operations/task-ownership-and-follow-through.md (GitHub label policy).

## Common mistakes

Answering only in your own chat. Expanding scope. Claiming tests passed without
running them. Editing files outside the boundary.

## Repository hygiene

- **Clean Workspaces**: Perform coding work in isolated feature branches or managed worktrees.
- **Cleanup Offer**: Upon finishing and pushing verified changes, offer to clean up and remove the branch or worktree used.
- **Process Sanity**: Perform an explicit process cleanup check after running pytest, node, playwright or dev servers; verify PID ownership, freshness, and no-longer-needed status before terminating attributable child processes. Never stop persistent host services.
- **Preservation & Containment**: Verify absolute path containment before deleting task artifacts. Never delete or overwrite dirty checkouts, unpushed commits, backups (*.bak, *.old, .backup-*), or ignored configuration (.env, secrets).
