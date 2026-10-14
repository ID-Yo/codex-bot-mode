# Repository Hygiene

Repository hygiene defines canonical procedures for maintaining clean, safe, and recoverable workspaces across Codex Bot Mode projects. It ensures completed work is fully integrated and closed out, prevents disk and worktree sprawl, and protects dirty, uncommitted, and user-owned state.

## Core Principles

- **Safety First**: Never perform silent, blanket, or recursive deletions. A branch or worktree must never be deleted if it contains unique uncommitted changes, unpushed commits, or dependent branches. Unknown or unattributable paths and processes are strictly preserved.
- **Preserve Unrelated Work**: Dirty checkouts, untracked user files, stashes, backups (`*.bak`, `*.old`, `.backup-*`), and needed ignored files (`.env`, secrets, local configuration, build caches) are strictly preserved.
- **Follow-Through Over Intent**: Dispatching a turn, pushing a branch, or opening a draft PR does not constitute task completion. An engineering task requiring code changes is complete only when verified, merged into the canonical authoritative remote branch, and closed in the issue tracker and local request ledger.
- **Separation of Concerns**: Monitor detects hygiene anomalies; Managers review and execute approved cleanup; Chief of Staff consolidates cross-project status. This procedure does not create, require, or authorize custom background daemons or autonomous destructive schedulers.

## Responsibilities by Role

### 1. Agent Monitor (Detection Only)
- **Observation Scope**: Inspects activity metadata, worktree registrations, and tracking states. Monitor *detects only* and never executes cleanup commands or file deletions.
- **Detection Targets**:
  - Tasks in `assigned` stage where an agent accepted work, produced an initial plan, but never started implementation and recorded no deliverable or blocker.
  - Merged pull requests where the remote or local branch was not closed out, or tracker issues remaining open for completed work.
  - Stale worktrees or untracked task directories remaining after issue closure.
  - Attributable temporary test artifacts or orphaned child processes.
- **Reporting**: Emits a single, deduplicated, evidence-backed notice to the authorized Chief of Staff using its `agent/STATE.md` ledger. Suppresses duplicate alerts across cycles when evidence is unchanged. Respects canonical pause states.

### 2. Project Manager (Execution & Lifecycle Reconciliation)
- **Task Follow-Through Verification**: Verifies that the next execution stage actually began after planning. If an agent in `assigned` stage is idle with only a plan and no deliverable or blocker (under pause, dependency, and external-review guards), prompts for follow-through or reassigns. If evidence is missing, reports `unknown` rather than assuming a stall.
- **Lifecycle Closeout**:
  - Confirms independent review approval (Code Reviewer) and acceptance verification (Verifier).
  - When a task requires a pull request, confirms the PR is merged into the canonical authoritative remote branch (e.g. `origin/<default-branch>`), not merely pushed. Local, documentation, or review-only tasks close under their own verified acceptance criteria without blanket GitHub PR requirements.
  - Closes the corresponding issue on the issue tracker with evidence links.
  - Closes out the request ledger entry in `.bot-mode/local/requests.json` (`state: answered`, `outcome: done`) with reply link.
- **Cleanup Execution**:
  - Before removing a local feature branch, inspects for unique unpushed commits (`git log <remote>/<default-branch>..HEAD` or `git log @{u}..HEAD`) and verifies no active dependent branches branch off it.
  - App-managed worktrees strictly require `mcp__codex_app__archive_worktree`. Raw `git worktree remove` is strictly ineligible for app-managed checkouts.
  - Needed ignored files (`.env`, secrets, local configuration, build caches) must be copied/preserved separately outside the worktree *before* archiving, because `archive_worktree` snapshots only tracked and non-ignored untracked files.
  - Ordinary `git worktree remove` is permitted only for clean, attributable, non-managed worktrees after validating zero uncommitted changes and zero unpushed commits.

### 3. Developer (Worktree & Process Hygiene)
- **Scoped Workspaces**: Performs feature and bugfix work in isolated branches or managed worktrees.
- **Cleanup Offer**: Upon finishing a coding task and pushing verified changes, offers to clean up and remove the feature branch or worktree used.
- **Process Cleanup Sanity Check**: After running commands that may leave background processes behind (such as `pytest`, `node`, `playwright`, `vitest`, or dev servers), performs an explicit check verifying PID ownership, start time, and that the process is no longer needed by active work before terminating it.
- **Intentional Process Distinction**: Distinguishes short-lived attributable task processes from intentional, persistent services (such as the running Office host on 24242, Desktop relay on 24243, or user-owned background daemons). Never terminates persistent host services.
- **Path Containment**: Before deleting temporary test outputs, verifies absolute path resolution and proves containment strictly within the approved task-specific workspace or temp directory. Unknown paths or processes are preserved.

### 4. Code Reviewer and Verifier (Boundary & Clean State Verification)
- **Code Reviewer**: Verifies that proposed diffs contain only minimal, task-scoped changes with zero unrelated modifications, format churn, or accidental credential/private path leaks.
- **Verifier**: Verifies that delivered artifacts meet acceptance criteria and that testing leaves clean working trees with zero leftover test artifacts or unmanaged processes.

### 5. Chief of Staff (Cross-Project Consolidation)
- **Consolidated Review**: Receives hygiene reports from the Agent Monitor across projects.
- **Coordination**: Notifies the responsible Project Manager of detected stalls or stale checkouts.

## Configurable Daily Hygiene Inspection

Projects may configure a daily hygiene inspection (Daily Hygiene Sweep), distinct from the Monitor's 20-minute execution heartbeat:
- **Cadence**: Run once daily during low-activity windows via the supported desktop application heartbeat or manual Chief of Staff inspection. This procedure does not create, require, or authorize custom background daemons or autonomous destructive schedulers.
- **Daily Inspection Scope**:
  1. Forgotten or unmerged pull requests.
  2. Tracker issues remaining open for work already merged or completed.
  3. Stale local feature branches, unmanaged task checkouts, or unarchived managed worktrees.
  4. Attributable orphaned temporary test artifacts and leftover task processes.
  5. Assigned execution tasks where planning completed but implementation never began.
- **Action Rule**: The daily inspection reports findings to the Chief of Staff and responsible Project Manager. Within recorded owner scope or standing authorization, Managers execute routine cleanup for items strictly matching the narrow allowlist without asking for repetitive confirmation for every attributable resource; ask the owner only when targets are unknown, ambiguous, or consequential. Curation is tracked as a separate task.

## Task Follow-Through and Closeout Checklist

Every engineering task requiring code modifications follows five verified closeout steps:
1. **Independent Verification**: Candidate SHA passes targeted automated checks and independent review/QA.
2. **Remote Ref Merge**: When required by the task, pull request is merged to the project's canonical authoritative remote branch; remote ref is fetched and verified.
3. **Tracker Closeout**: Corresponding issue in the tracker is closed with link to the merge commit.
4. **Ledger Resolution**: Local request ledger entry in `.bot-mode/local/requests.json` is updated to `answered`/`done` with reply link.
5. **Attributable Cleanup**: Local feature branch or worktree is cleaned up after confirming no unique unpushed commits remain. For app-managed worktrees, needed ignored files are preserved before calling `archive_worktree`.

## Narrow Allowlist for Attributable Cleanup

When cleaning up a workspace, only the following items are eligible for removal:
- **Merged Feature Branches**: Local git branches whose tips are fully merged into the canonical authoritative remote branch and have no dependent active branches.
- **Clean App-Managed Worktrees**: Archived strictly via `mcp__codex_app__archive_worktree` after preserving needed ignored files separately outside the checkout.
- **Clean Non-Managed Worktrees**: Removed via `git worktree remove` only after verifying absolute path containment, clean git status, zero unpushed commits, and no dependent branches.
- **Attributable Test Artifacts**: Ephemeral test outputs, coverage dumps (`.coverage`, `htmlcov`), and browser test traces created during the immediate task, verified inside the task root.
- **Terminated Task Processes**: Orphaned test runners (`pytest`, `node`, headless `chromium`) attributable to the current turn, verified by PID, freshness, and no-longer-needed status.

### Strictly Excluded from Cleanup
- Dirty checkouts with modified or staged files.
- Backups (`*.bak`, `*.old`, `.backup-*`) and stash entries.
- Unmerged branches or branches with unpushed commits against the authoritative remote ref.
- Ignored configuration and secret files (`.env`, local settings, private tokens).
- Unknown or unattributable paths and processes.
- Intentional host daemons (Office server on 24242, Desktop relay on 24243, Codex Desktop runtime).

## Concise Hygiene Audit Report Template

When recording a repository hygiene inspection, format findings using this concise template:

```markdown
### Repository Hygiene Audit: <Project Name>
- **Date/Time**: YYYY-MM-DDTHH:MM:SSZ
- **Auditor**: <Role>
- **Canonical Remote Branch**: <e.g. origin/main>
- **Merged Branches Removed**: <list of branches or none>
- **Active Branches Checked**: <list with unpushed/dependent status>
- **Managed Worktrees**: <clean / archived / preserved dirty>
- **Process Sanity**: <clean / terminated attributable PIDs>
- **Unreconciled Tasks / Stalls**: <none / task ID with plan-only blocker-free status>
- **Preserved Work**: <dirty files, backups, ignored settings confirmed intact>
```
