---
{"document":"task","status":"draft","owner":"<owner>","last_reviewed":null}
---

# Task: <title>

## Metadata
- **Task ID**: `<stable-id>`
- **Project**: `<project-id>`
- **Team**: `<team-name>`
- **Responsible Manager**: `<project/team/bot ID with fresh roster binding>`
- **Assigned Executor**: `<project/team/bot ID with fresh roster binding or unassigned>`
- **Assignment Acknowledgment**: `<acknowledged timestamp and evidence link>`
- **State**: `<backlog | ready | in_progress | review | blocked | paused | completed>`
- **Tracking Mode**: `<github | local | both>`
- **Tracking Authority**: `<github | local>`
- **GitHub Issue**: `<url or none>`
- **Local Task Record**: `<path to project task ledger or local record>`
- **Local Request ID**: `<request-id or none>`
- **Latest Evidence**: `<timestamp and link to latest artifact or check>`

## Goal and Scope
<What concrete outcome this task accomplishes and what remains out of scope.>

## Acceptance Criteria
1. <Measurable, testable criterion 1>
2. <Measurable, testable criterion 2>

## Next Action
- **Action**: <Immediate concrete next step>
- **Actor**: <Bot ID or role responsible for next step>

## Approval to Start (Required before ready work is started)
- **start_approved**: <true | false>
- **Approved by**: <owner or delegated authority, with identity and a link to the instruction or standing delegation>
- **Approved on**: <timestamp>
- **Approved scope**: <what may start; a backlog item or a ready label alone is not approval; on GitHub the approval is written in the issue text with the same backing reference; no label carries it>

## Stage Completion Records (one entry per finished stage; the same record is posted as a comment on the authoritative issue)
- **Stage and executor**: <stage; project/team/bot ID>
- **Result**: <delivered | pass | fail | unverified | blocked>
- **Evidence**: <links; exact candidate commit SHA and author, or none and why>
- **Checks and limitations**: <what ran, results, what is unverified>
- **Next responsible and action**: <participant proposed by the executor and the concrete action>
- **Dispatch**: <request ID, delivery ID and acknowledgment once the Manager dispatched it, or the blocker issue link>
- A finished stage never closes the issue; the Manager keeps ownership until verified completion. Intermediate pull requests link the issue with Refs; only the final accepted deliverable uses Closes.

## Blockers and Explicit Waits (Required if state is blocked or waiting)
- **Blocker / Decision Issue**: <Confirmed issue URL, or local pending-sync path and sync condition if tracker access is unavailable>
- **Requested Action / Decision**: <Specific input needed, options and recommendation when relevant>
- **Blocked Reason**: <Concrete cause: capacity, dependency, quota, decision, device>
- **Unblock Owner**: <Authorized entity: Repository Owner, Chief of Staff, etc.>
- **Resume Condition**: <Measurable condition required before execution resumes>
- **Next Check**: <Timestamp or event for next check>

## Evidence and Artifacts
- **Branch / PR**: `<branch-name or pr-url>`
- **Verification Report**: `<path to review or verification report>`
- **Test Outcome**: `<test command and pass summary>`
