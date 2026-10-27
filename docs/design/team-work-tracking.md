# Team work tracking

Task recording is mandatory. At appointment the owner selects `tracking_mode`:
`github`, `local` or `both`. Defaults do not select it. A dry run can show an
unselected mode; installation and Office delivery require the selection.
Existing teams retain their explicit workflow; unknown choices are recorded as
rollout gaps rather than guessed. Individual additions inherit their team's
workflow from its roster entry and project instructions.

Every actionable task, implementation or change, bug and decision must be linked to
an existing GitHub issue, or receive a new one, before execution. Reuse a matching
issue; do not open one per chat message, test command or executor stage. The issue
holds a stable ID, goal, scope, acceptance criteria, the responsible Manager and
executor, one lifecycle label, a type label only when the type is clear (never
guess one), a priority label only when explicitly selected (priority never grants
start approval), status, stage evidence and the next
responsible participant and action. Track relevant subtasks, bugs and decisions under
it, or in linked child issues when they need independent ownership or acceptance.
Conversations and short read-only inquiries need no issue; when one becomes
actionable, link or create the issue first. Chat alone is not an implementation record.

- `github`: the issue is the authoritative record. Code and documentation changes
  use a branch and PR linked to it.
- `local`: a GitHub issue is still required before actionable execution. The task
  ledger and stable task ID are supplemental pointers, offline queue entries and
  dated evidence (change/diff, exact version, verification results).
- `both`: `tracking_authority` is `github`. Cross-link local records, issue and PR.
  The issue owns mutable status; the local record contains pointers and dated
  evidence. A legacy `tracking_authority: local` is recorded as a rollout gap and
  is not used for new actionable work. The Manager reconciles links and evidence at
  handoff and completion, without maintaining competing statuses.

GitHub CI failure does not suspend tracking. Run applicable local CI and record
commands, results and limitations; no paid service is required. If GitHub API
is unavailable, immediately write a local pending-sync record (stable ID, goal,
scope, acceptance, owner, state, evidence), disclose that it is not published,
reconcile against existing issues without duplicates when access returns and add
the resulting link. Never call it published until the remote record is read back.
A project with no configured authorized GitHub destination records the setup
blocker and continues only independent work that is already authorized; it never
infers publication rights or publishes private content to an unrelated or public
tracker.

CLI example:

```text
python -B scripts/team.py install-team development --project <project> --tracking-mode both --tracking-authority github --approval once --confirm
```

Answers files use `team.tracking_mode` and `team.tracking_authority`.
The shared roster stores the choice under `teams[].work_tracking`.
Approval and tracking are separate: a selected mode grants no publication rights.

For full operational lifecycle procedures, see [Task Ownership and Follow-Through Policy](../operations/task-ownership-and-follow-through.md) and [Task Template](../../templates/task.md).
