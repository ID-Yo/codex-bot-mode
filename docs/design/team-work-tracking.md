# Team work tracking

Task recording is mandatory. At appointment the owner selects `tracking_mode`:
`github`, `local` or `both`. Defaults do not select it. A dry run can show an
unselected mode; installation and Office delivery require the selection.
Existing teams retain their explicit workflow; unknown choices are recorded as
rollout gaps rather than guessed. Individual additions inherit their team's
workflow from its roster entry and project instructions.

Before implementation, create or link a record with a stable ID, goal, scope,
acceptance criteria, responsible owner, status and evidence. Track relevant
subtasks, bugs and decisions under it. Routine one-line inquiries need no task.
Chat alone is not an implementation record.

- `github`: reuse an existing issue or create one with criteria, status and
  evidence. Code and documentation changes use a branch and PR linked to it.
- `local`: use the project's task ledger and stable task ID. Record the local
  change/diff, exact version and verification results; a GitHub PR is not required.
- `both`: also select `tracking_authority` (`github` or `local`). Cross-link
  local records, issue and PR. Only the authoritative record owns mutable status;
  the other contains pointers and dated evidence. The Manager reconciles links
  and evidence at handoff and completion, without maintaining competing statuses.

GitHub CI failure does not suspend tracking. Run applicable local CI and record
commands, results and limitations; no paid service is required. If GitHub API
is unavailable, use a temporary local pending-sync record, reconcile against
existing issues when available and add the resulting link. Never call it published
until the remote record is read back.

CLI example:

```text
python -B scripts/team.py install-team development --project <project> --tracking-mode both --tracking-authority github --approval once --confirm
```

Answers files use `team.tracking_mode` and `team.tracking_authority`.
The shared roster stores the choice under `teams[].work_tracking`.
Approval and tracking are separate: a selected mode grants no publication rights.
