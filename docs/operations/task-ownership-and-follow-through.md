# Task Ownership and Follow-Through Policy

Canonical operational procedure for task recording, lifecycle progression, and automated monitor audits across Codex Bot Mode teams. This procedure is distinct from [Repository Hygiene](repository-hygiene.md), which governs branch, worktree, and artifact closeout.

## 1. Principles and Separation of Concerns

- **Manager Ownership vs. Executor Assignment**: Every task has exactly one responsible Manager who owns the task from creation to verified completion or cancellation. An executor (such as Developer, Code Reviewer, or Verifier) may be assigned to an active stage, but assignment does not transfer task ownership. Identify bots by project/team/bot ID and fresh roster binding; a GitHub human assignee is optional and is not a bot identity.
- **Continuous Actual Progress Once Started**: Once the owner agrees to START work, an appointed team member must actually progress implementation, review, corrections, QA, or authorized integration until completion unless a concrete blocker prevents progress. Dispatch, ready, and accepted queues are not execution. Review and QA count only while actually underway. The Manager inspects completed turns and promptly routes the next stage, retaining responsibility beyond dispatch.
- **Explicit Waiting and Blocked Conditions**: Record blocked or waiting work with reason, responsible unblock owner, resume condition, and next check timestamp. Capacity, external dependencies, quota exhaustion, pending owner decisions, and unavailable devices are explicit waits.
- **Backlog and Paused Expectations**: Tasks in `backlog` or `paused` state carry no expectation of active turn execution; explicit owner pause overrides. Agents must not manufacture busywork or force unlimited parallel work. This does not let approved ready work wait unrecorded while an eligible executor is idle (section 6).

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

### State vocabulary

Several documents and tools use different names for the same state. The canonical names are the seven above; these are their equivalents:

| Canonical | Team workflow design | Task snapshot and Monitor | GitHub label |
|---|---|---|---|
| `backlog` | (none) | `backlog`, `future` | `state:backlog`, `backlog`, `state:future` |
| `ready` | `ready` | `ready` | `state:ready` |
| `in_progress` | `active` | `in-progress` | `state:active` |
| `review` | `review` | `review` | `state:review` |
| `blocked` | `needs-owner` (an owner decision is the blocker) | `blocked` | `state:needs-owner` |
| `paused` | (none) | `paused` | (none) |
| `completed` | `accepted` | `done` | closed issue with the reason recorded |

`cancelled` (team workflow design) is a closed issue with the cancellation reason. `ready` means criteria are defined; it is not approval to start (section 6). A state name never changes the rule that only the authoritative record owns mutable status.

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

## 5. Stage Completion Record and Handoff

A stage is one executor's assigned piece of a task: implementation, review, correction, QA or authorized integration. Stage completion, pull request merge and issue completion are different events. Finishing a stage never closes the issue and never replaces the independent gates.

- **Record**: When an executor finishes a stage it posts a stage completion comment on the authoritative GitHub issue (tracking `github` or `both` with GitHub authority), or appends the same record to the authoritative local task record (`local`), and replies to the requester in the requester's channel. A reply only in the executor's own chat does not count. The record states: task ID and stage; executor identity (project/team/bot ID); result (delivered, pass, fail, unverified or blocked); evidence links (pull request, report, checks) with the exact candidate commit SHA and author, or "none" and why; checks run, results and limitations; the proposed next responsible participant and action; and, once the Manager has dispatched it, the request ID, delivery ID and acknowledgment of that dispatch, or the blocker issue link. An identifier that does not exist is stated as unavailable, never invented.
- **Who decides**: The executor proposes the next responsible participant and action. Specialists do not assign work to each other. The Manager, in the same turn in which the record arrives, dispatches the next scoped request (request ID, acceptance criteria, write boundary, reply target) or records a blocker under the blocker issue procedure. The proposal does not transfer ownership: the Manager keeps the task from creation to verified completion or cancellation.
- **Acknowledgment**: An accepted delivery is not a started stage. The recipient acknowledges in one line that it has actually started (what it started and the exact candidate it works on); the next stage counts as started only then. Without an acknowledgment the Manager reads the recipient's chat, asks once more, then escalates to the owner.
- **Snapshot**: Where the project keeps a task snapshot for the continuity check, the Manager updates it at the handoff (stage, executor, candidate, next action) and runs the check; a handoff is complete only when the check passes or its finding is recorded as a blocker.
- **Closing**: The issue is closed only by the Manager after recorded acceptance, never by a stage completion or an intermediate pull request.
- **Gates**: The record names the exact candidate. A review or QA request without the exact candidate SHA and author is invalid.

## 6. Approved Ready Work and Idle Executors

- **Approval to start is a recorded act**: the task's `start_approved` field is set to true together with evidence of who approved, when, and for which scope. On GitHub-tracked work an additive label `start:approved` may carry the approval only when it is backed by a reference to the owner's instruction or a standing delegation. Approval is never inferred from `ready` or `backlog` alone. Existing labels and local records need no migration.
- **No unrecorded waiting**: Approved ready work must not wait while an eligible appointed executor is idle without a recorded reason. Eligible means: explicitly appointed in the roster for the needed role and task scope, no owner pause, no single-writer conflict, sufficient permissions, and fresh Desktop activity evidence (no older than one audit interval, 20 minutes by default) showing it idle.
- **Manager duty**: In the same processing turn in which the Manager finds approved ready work and an eligible idle executor, it dispatches the next scoped assignment. Otherwise it records on the task the reason (capacity, dependency, authorization, quota or device), the unblock owner, the resume condition and the next check. It checks this at every completed turn and every Monitor signal.
- **Unknown is not Idle**: Missing or stale activity evidence is Unknown. Nobody infers an idle executor or a stall from it. Explicit owner pause, single writers, permissions and the ban on busywork stay in force.

## 7. Issue and Pull Request Linking

- **Intermediate pull requests use Refs**: A pull request that is a stage candidate, a correction, a release preparation or anything else that does not complete the issue links it with `Refs #N`. It never uses `Closes`, `Fixes` or `Resolves`, because merging it would close the issue before the work is accepted.
- **Terminal Closes only for the final accepted deliverable**: `Closes #N` belongs only in the pull request that is the final deliverable, after independent review and verification passed on its exact candidate and acceptance is recorded with no stage remaining.
- **Request done is not issue done**: A reply with outcome `done` closes that request, not the issue. An open work issue whose last request is done needs the next stage dispatched, the blocker recorded, or its closure by the Manager under section 4.
- **Open work issues are visible**: Every open work issue carries a state label and a responsible participant. An open issue without them is a recording omission to correct, not a task nobody owns.

### Enforcement status of sections 5 to 7

These sections are operating rules applied through project instructions and the catalog profiles. They are not technical enforcement. The Monitor currently detects a stage that finished without a successor (from a replied request), a request not started, overdue deadlines and a few related conditions. It does not detect open issues without a state label, an open issue whose last request is done, approved ready work without a dispatch, or a missing structured next responsible participant. Each of those needs its own implemented and verified change; until then the Manager checks them at every completed turn. When detection of ready work is added it keeps the 30-minute grace already used for requests not started and the 20-minute audit cadence, and treats missing or stale activity as Unknown.

The Monitor derives its handoff acknowledgment and start times from the request's opening time; that is not evidence of an acknowledgment or a start and is tracked as a separate defect (see the issue linked from the pull request that introduced this section). Until it is fixed, read the Monitor's start information with that limit.

### Scope of this policy

Editing these files changes the repository and the catalog only. Installed profiles are updated in place by a reviewed, scoped profile patch with a backup (see [Profile Updates](profile-updates.md)), after the review and QA gates and the activation acknowledgment. No new or replacement chat is created unless a chat is broken and that is separately authorized, and no profile of another project is written implicitly. Catalog role template versions are not bumped for these text changes: earlier playbook changes did not bump them and the integers are not compatibility markers.

## 8. Agent Monitor 20-Minute Audit Guidance

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
- **Deduplication and routing**: All findings are deduplicated via `(project_id, bot_id, task_id, alert_kind, observed_state)` fingerprints in `agent/STATE.md`. Actionable alerts are reported to the Chief of Staff. The signals `stage_finished`, `request_not_started` and `request_not_started_overflow` are also sent directly to the owning project Manager (see [Agent Monitor](agent-monitor.md)). The Chief of Staff report informs; it does not forward the same item to the Manager again, except to report that no Manager message could be sent (`manager_delivery_unavailable`).
- **No Direct Interventions**: Monitor never invokes model wakeups, restarts processes, modifies product or repository files, or reassigns tasks.
- **No Product or Repository Mutations**: Monitor writes solely to its own `agent/STATE.md` for checkpoint and deduplication bookkeeping; it performs zero product, repository, or task edits, wakeups, interventions, model changes, or new schedules.
- **Active Tasks First**: Audit active tasks first, then classify older backlog. Avoid alert floods.

Related documentation: [Team Work Tracking](../design/team-work-tracking.md), [Agent Monitor](agent-monitor.md), [Repository Hygiene](repository-hygiene.md), [Task Template](../../templates/task.md).

