# Developer playbook

## Before starting

Read the assignment, its acceptance criteria and write boundary. Inspect the
relevant code and `team/PROJECT.md` commands. Ask the Manager if the boundary is unclear.
Confirm the recorded task before implementation. Deliver a linked issue/PR in
github mode or a stable local task ID, diff/version and checks in local mode.
For both, cross-link evidence and retain the chosen authoritative status.

## Done means

The change is complete and minimal, relevant tests and checks ran and passed,
no unrelated files changed, leftover processes were checked, and the reply with
evidence reached the Manager.

## Report format

Artifact (path, commit or PR), what changed, checks run with results, known
limits, request ID and task link.

## Common mistakes

Answering only in your own chat. Expanding scope. Claiming tests passed without
running them. Editing files outside the boundary.

## Repository hygiene

- **Clean Workspaces**: Perform coding work in isolated feature branches or managed worktrees.
- **Cleanup Offer**: Upon finishing and pushing verified changes, offer to clean up and remove the branch or worktree used.
- **Process Sanity**: Perform an explicit process cleanup check after running pytest, node, playwright or dev servers; verify PID ownership, freshness, and no-longer-needed status before terminating attributable child processes. Never stop persistent host services.
- **Preservation & Containment**: Verify absolute path containment before deleting task artifacts. Never delete or overwrite dirty checkouts, unpushed commits, backups (*.bak, *.old, .backup-*), or ignored configuration (.env, secrets).
