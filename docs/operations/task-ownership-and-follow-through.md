# Task Ownership and Follow-Through Policy

Canonical operational procedure for task recording, lifecycle progression, and automated monitor audits across Codex Bot Mode teams. This procedure is distinct from [Repository Hygiene](repository-hygiene.md), which governs branch, worktree, and artifact closeout.

## 1. Principles and Separation of Concerns

- **Manager Ownership vs. Executor Assignment**: Every task has exactly one responsible Manager who owns the task from creation to verified completion or cancellation. An executor (such as Developer, Code Reviewer, or Verifier) may be assigned to an active stage, but assignment does not transfer task ownership. Identify bots by project/team/bot ID and fresh roster binding; a GitHub human assignee is optional and is not a bot identity.
- **Continuous Actual Progress Once Started**: Once the owner agrees to START work, an appointed team member must actually progress implementation, review, corrections, QA, or authorized integration until completion unless a concrete blocker prevents progress. Dispatch, ready, and accepted queues are not execution. Review and QA count only while actually underway. The Manager inspects completed turns and promptly routes the next stage, retaining responsibility beyond dispatch.
- **Explicit Waiting and Blocked Conditions**: Record blocked or waiting work with reason, responsible unblock owner, resume condition, and next check timestamp. Capacity, external dependencies, quota exhaustion, pending owner decisions, and unavailable devices are explicit waits.
- **Backlog and Paused Expectations**: Tasks in `backlog` or `paused` state carry no expectation of active turn execution; explicit owner pause overrides. Agents must not manufacture busywork or force unlimited parallel work.

## 2. Mandatory Task Lifecycle States

Each task record tracks one of the following canonical states:

| State | Meaning | Execution Expectation |
|---|---|---|
| `backlog` | Captured objective or future work; unassigned. | No active execution expected. |
| `ready` | Acceptance criteria defined; ready for assignment. | Awaiting Manager assignment. |
| `in_progress` | Assigned to an active executor for implementation. | Active turns and progress expected. |
| `review` | Deliverable produced; awaiting independent review. | Code Reviewer / Verifier execution. |
| `blocked` | Progress halted by an explicit external impediment. | Blocked reason, unblock owner, and resume condition required. |
| `paused` | Temporarily suspended by owner or manager priority. | Execution suspended until explicit resume. |
| `completed` | Verified deliverables accepted; task closed. | Work finished and reconciled. |

### Blocked State Requirements
When a task transitions to `blocked` or enters an explicit wait, the record must document:
1. **Blocked Reason**: Concrete technical or external cause (e.g. capacity, dependency, quota exhaustion, pending owner decision, unavailable device).
2. **Unblock Owner**: Entity authorized to resolve the blocker (e.g. Repository Owner, Chief of Staff).
3. **Resume Condition**: Measurable condition required before execution resumes.
4. **Next Check**: Scheduled time or event for the next status check.

## 3. Workflow Tracking Modes

As defined in [Team Work Tracking](../design/team-work-tracking.md), projects select `github`, `local`, or `both` tracking mode:
- **`github`**: Authoritative record is an issue on GitHub. Pull requests cross-link the issue.
- **`local`**: Authoritative record is in the project's chosen task ledger or local task record (such as `work/tasks/<task-id>.md` or a project task ledger), while `.bot-mode/local/requests.json` is the inter-agent request/reply ledger cross-linked to the task by stable task ID.
- **`both`**: Explicitly selects `tracking_authority` (`github` or `local`). Only the authoritative record owns mutable status; the secondary record stores pointers and synchronized evidence, with `.bot-mode/local/requests.json` linking inter-agent requests to the authoritative task.

## 4. Manager Responsibilities

The appointed project Manager enforces task follow-through through completed-turn inspection:
1. **Intake & Acknowledgment**: Acknowledge receipt of task instructions; confirm tracking mode and record stable task ID.
2. **Assignment & Boundary**: Issue explicit requests containing request ID, acceptance criteria, write boundary, and link to task record.
3. **Completed-Turn Inspection**: Inspect completed turns. Do not rely on dispatch receipts or prompt promises. Verify deliverables match criteria.
4. **Review & Verification Routing**: Route candidate deliverables to independent Code Reviewer and Verifier gates before merge.
5. **Integration & Lifecycle Closeout**: Conditional closeout based on selected tracking authority and whether authorized remote integration is required: upon dual-gate approval, if remote integration was authorized (such as GitHub pull requests or issues), execute merge on the canonical remote branch, close the tracker issue, and reconcile the local request ledger; if tracking is local without remote integration, record the exact deliverable, version/commit/diff, and verification evidence in the local authoritative task record, closing the task only when authorized.
6. **Blocker Registration**: When an unresolvable obstacle is detected, register explicit blocked state and escalate to owner.

## 5. Agent Monitor 20-Minute Audit Guidance

The existing 20-minute [Agent Monitor](agent-monitor.md) periodically audits task follow-through across appointed teams. The monitor adheres to strict non-intervention rules:

### Monitored Conditions
- **Missing Task Ownership**: In-progress task without a responsible Manager or assigned executor identified by `project/team/bot ID` with fresh roster binding.
- **Unacknowledged Handoff**: Task dispatched to an agent who remained idle without acknowledging or beginning execution within expected turn bounds.
- **Finished Turns Without Continuation or Blocker**: Executor turn has completed or is idle while holding an `in_progress` task without a tangible deliverable, test result, next step, active continuation, or recorded blocker. An earlier plan or artifact does not exempt a finished turn that lacks an active continuation or blocker.
- **Missing Review / QA Gates**: Candidate deliverable produced without assigned Code Reviewer or Verifier check against the current SHA / commit.
- **Accepted Gates Without Integration**: Deliverable passed review and verification gates but remains unmerged without a recorded blocker.
- **GitHub / Local Contradictions**: Mismatched status between GitHub issue and local task ledger in `both` tracking mode.

### Audit and Reporting Invariants
- **Authoritative Evidence Only**: Monitor uses fresh roster (`scripts/roster.py`) and authoritative tracker data. If access to a repository or tracker is unavailable, report status as `UNKNOWN` rather than inferring a violation.
- **Deduplication to Chief of Staff**: All findings are deduplicated via `(project_id, bot_id, task_id, alert_kind, observed_state)` fingerprints in `agent/STATE.md`. Actionable alerts are delivered solely to the Chief of Staff.
- **No Direct Interventions**: Monitor never invokes model wakeups, restarts processes, modifies product or repository files, or reassigns tasks.
- **No Product or Repository Mutations**: Monitor writes solely to its own `agent/STATE.md` for checkpoint and deduplication bookkeeping; it performs zero product, repository, or task edits, wakeups, interventions, model changes, or new schedules.
- **Active Tasks First**: Audit active tasks first, then classify older backlog. Avoid alert floods.

Related documentation: [Team Work Tracking](../design/team-work-tracking.md), [Agent Monitor](agent-monitor.md), [Repository Hygiene](repository-hygiene.md), [Task Template](../../templates/task.md).
