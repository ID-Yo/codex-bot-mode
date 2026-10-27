# Agent Monitor

The **Agent Monitor** provides read-only, task-aware, and pause-aware activity observation across appointed project agents in Codex Bot Mode. It tracks task follow-through and reports fresh, deduplicated alerts to the authorized Chief of Staff and sends the Manager signals listed under Scope and Safeguards directly to the owning project Manager.

## Architecture and Delegation Chain

The operational hierarchy in Codex Bot Mode follows a clear chain of authority:
- **Owner / Chief of Staff**: Allocates cross-project priorities and reviews operational health.
- **Project Manager**: Directs one project team, assigns work, and resolves blockers.
- **Project Agents**: Execute assigned tasks within designated boundaries.
- **Agent Monitor**: Observes agent activity metadata, alerts the Chief of Staff when execution stalls or quota fails, and signals the owning project Manager directly for a finished stage without successor and a request not started.

```text
Chief of Staff <-------- [Alerts / Health Reports] <-------- Agent Monitor
      |                                                            ^
      v                                                            | (read-only metadata)
Project Managers -----------------------------------------> Project Agents
```

## Scope and Safeguards

- **Metadata Only & Discard Rule**: The Monitor inspects thread state and task tracking via supported app tools (such as `list_threads`). It explicitly discards thread titles, summaries, tool inputs/outputs, and conversation text before analysis and never persists them.
- **Zero Unsolicited Interventions**: The Monitor has zero authority to reassign tasks, edit product code, restart processes, or switch models.
- **Authorized Routing & Standing Approval**: The report goes to the configured `recipient` parameter (Chief of Staff). The only other project messages are the `manager_deliveries` items (`stage_finished`, `request_not_started`, `request_not_started_overflow`, `active_without_work`), sent to the resolved project Manager under the same standing authorization. When `recipient` is empty, the Monitor enters `setup_required` state and emits zero alerts. Because heartbeat runs have no inbound requester message, the report goes to this target. The outside-project approval requirement in BASE.md is satisfied by this standing target authorization recorded at setup.
- **Durable Deduplication in Own State**: The Monitor maintains its own runtime `agent/STATE.md` (authorized and required by BASE.md) to record deterministic event fingerprints `(project_id, bot_id, task_id, alert_kind, observed_state)` and delivery status (`sent`, `acknowledged`, `unconfirmed`). It suppresses duplicate alerts across cycles when evidence is unchanged. If a delivery outcome is unconfirmed, it records `delivery_unknown` and avoids blind resends. Upon agent recovery or task resolution, active alert fingerprints are cleared.
- **No Custom Schedulers**: Periodic monitoring relies on the supported desktop application heartbeat configured manually by the Chief of Staff (defaulting to 20-minute intervals with `gpt-6-luna` on low reasoning). The Monitor runs zero background daemon loops.

## Task Follow-Through Protocol

The Monitor evaluates task follow-through per canonical policy ([Task Ownership and Follow-Through Policy](task-ownership-and-follow-through.md)) by distinguishing four execution stages:
1. **Assigned**: The task has been recorded in the tracker and assigned to a specific agent.
2. **Active**: The agent is actively executing turns or running verified commands.
3. **Verified**: The deliverable artifact has been produced and validated against criteria.
4. **Blocked**: An explicit blocker has been recorded by the agent or manager.

### Alert Trigger Criteria
- An alert is warranted when an agent in **assigned** stage is **idle or completed** while holding an execution task, but has delivered only an initial plan with no artifact, test output, or recorded blocker.
- **Unknown Not Idle**: If task progress or deliverable evidence is missing from the tracker, classify the task as `unknown`, not stalled.
- **Elapsed time alone is not a stall**: An agent waiting on an external review, dependency, or scheduled run is not stalled.
- **Canonical Pause**: If the project or agent is in a paused state, the Monitor respects the pause and suppresses stall alerts.
- **Deduplication**: The Monitor emits exactly one evidence-backed alert per event using its `agent/STATE.md` fingerprint ledger.
- **Overdue reminders**: An unresolved overdue finding (a task, blocker, runtime or reconciliation deadline that has passed) is not silenced for good after its first confirmed delivery. The ledger keeps `alerted_at` per fingerprint, and the finding is reported again once the last report is six hours old, marked as a reminder with the date it was first seen. Reminders repeat every six hours until the finding is resolved, which clears it. A reminder never reopens the delivery: if its delivery is unknown it is not retried before the next interval. Only the four deadline findings (task, blocker, runtime and reconciliation overdue) repeat; every other code, including metadata findings, hygiene notes and the Manager signal codes, keeps its report-once behaviour even when its task deadline has expired, and paused tasks raise no overdue finding. A ledger time that is missing, unreadable or without a timezone counts as due instead of stopping the run. Reminders go to the Chief of Staff report only; the project Manager still receives `stage_finished` and the request signals.

## Request Not Started

The Monitor raises `request_not_started` (scope `<project>/<request-id>`) when all of these hold: a request in the project's `.bot-mode/local/requests.json` ledger is `open` and not recorded as started, it was opened more than 30 minutes ago, the recipient bot's bound thread (roster or bindings) is not running (idle or notLoaded, i.e. loaded in Desktop or not), and no turn on that thread started after the request was opened. The signal is deduplicated like every other alert, appears in the report to the Chief of Staff, and goes to the project Manager through `manager_deliveries` (the same path as `stage_finished`). Closed, answered, cancelled and GitHub-closed requests never fire.

Ledger links and answered requests (#305). A GitHub link groups with its issue or pull request whatever its fragment or query (`#issuecomment-`, `#discussion_r`, `?x`, a trailing slash), and every text field of a ledger row is read null-safely (`requests.py open` stores `"link": ""` without `--link`). An answered request that has no GitHub item and no task record is history: it never raises `stage_finished`. A `partial` reply keeps signalling until the requester (or the owner) records that the remainder is tracked elsewhere: `requests.py --project <root> accept <id> --by <requester> --link <follow-up>` (the link defaults to a `--successor` named at `reply --outcome partial`). The record `accepted: {by, at, link}` ends the signal; only the requester or `owner` can write it. A signal that the report confirmed as `sent` is not delivered to the Manager again while its scope is unchanged.

Thread status and turn start times come from one `read_thread` call per recipient with outputs disabled, sent through the project's local Desktop Relay, the authenticated path the Monitor stack already uses for dispatch; no message text is read. `wait_threads` is not used (it fails on the live relay while `read_thread` works). The relay descriptor is searched in `desktop_relay_root` from `monitoring.json` (optional), the project root, and the project that owns the registry file (`<root>/.bot-mode/local/<registry>.json`). Whatever cannot be read (no relay, unreadable thread or history, no start times, no bound thread, unreadable `opened`) is never treated as started or as not started: it becomes one `request_start_unavailable` finding per project that names each cause and the number of requests it affects. An HTTP 400 from the relay is a per-thread answer: the finding names the bot and the thread and says to check the roster binding, and it does not count toward the three transport errors in a row that stop the reads. The relay serves read_thread only for its appointed targets (the Office project's own chats and the registry Managers), so it refuses worker threads of other projects with the same 400.

Work and output are bounded. Each distinct recipient thread is read once per run (cached), at most 30 threads and 90 seconds of reads per run, and reads stop after three transport errors in a row; the unchecked rest is reported in the per-project `request_start_unavailable` finding as deferred. At most 10 `request_not_started` findings per project (oldest first) and 40 per run are listed; the remainder is one `request_not_started_overflow` finding per project with its count (also a direct Manager signal), so old open requests stay visible without overflowing the 64 KiB checkpoint. If the findings of a run still do not fit the checkpoint, the run keeps the most important ones and adds a `checkpoint_overflow` finding with the counts left out instead of failing.

## Active Without Work (#372)

An open issue labelled `state:active` raises `active_without_work` (scope `<project>/issue-<number>`) after 30 minutes without a change when it has no live (not answered) request, no linked task record (Authority URL) naming an executor and updated in the last 6 hours or in a `blocked` or `paused` status, and the project has no pause (`.bot-mode/PAUSED`). It reminds the Manager to assign the next concrete tasks or to record a blocker or wait with the next check. It does not say an agent is idle (activity is not read for it) and is not an outage. Backlog, ready, review, blocked, needs-owner and future issues, closed issues and pull requests never match; nothing is dispatched or relabeled. It is deduplicated like every other alert, appears in the Chief of Staff report and goes to the project Manager through `manager_deliveries`.

## Run and Audit Reliability

The scheduled turn and the audit it was meant to perform are separate outcomes. A completed heartbeat invocation alone never means that project inspection or checkpoint persistence completed.

- Keep a compact JSON checkpoint block at the start of the Monitor's own `agent/STATE.md`, before narrative history. `scripts/monitor_checkpoint.py` reads at most 16 KiB for this block and checks memory size without reading memory contents. Missing, malformed, stale or out-of-bound checkpoint data is `unknown`.
- `scripts/task_continuity.py monitor-audit` requires a fresh checkpoint bound to the current invocation ID, explicit covered task IDs, full coverage and fresh invocation metadata. A second currently active invocation makes the cycle fail. Old `active` records are stale/unknown, not proof of concurrent execution.
- Partial coverage still reports findings for the task scopes actually inspected. The cycle is marked incomplete, unavailable scopes remain unknown, and existing fingerprints for unobserved scopes are preserved. A later complete cycle clears resolved fingerprints.
- An incomplete or failed audit is a health finding. Persist the failure and send a concise, deduplicated report to the configured Chief of Staff with known task findings, coverage limits, delivery status and next check. Do not report clean from partial or old evidence.
- The Monitor reports and does not dispatch. The Chief of Staff validates the project scope and routes an authorized handoff to its Project Manager. That Manager decides whether to start the next stage or record a blocker. A delivered alert does not prove work resumed; only later observed stage evidence does.

The checker validates a supplied snapshot; it does not independently fetch live scheduler metadata, project data, or deliver messages. Those remain explicit inputs/actions of the appointed Monitor and Chief of Staff. Public tests demonstrate the contract with synthetic invocation metadata and do not claim installed-schedule or live continuation acceptance.

## Quota Exhaustion and Standby Guidance

- **Explicit Evidence Required**: The Monitor reports only verified, explicit quota exhaustion messages from the provider. Generic test failures, network timeouts, access errors, or application exceptions must never be attributed to quota limits without evidence.
- **No Autonomous Model Switching**: The Monitor never alters model configurations or agent bindings.
- **Chief of Staff Failover Action**: The authorized Chief of Staff alone may rebind the affected existing chat to an owner-authorized backup model (for example, Gemini Flash high as a configurable backup option, not the normal operational model), recording the primary model, reason, and timestamp.
- **Anti-Flapping and Recovery**: Maintain the backup binding until quota resets; avoid rapid model switching or blind retries. No availability promises are made for backup providers.
- **Privacy**: Specific account IDs, credit balances, and billing records remain strictly private and outside repository files.

## Related Documentation

- [Task Ownership and Follow-Through Policy](task-ownership-and-follow-through.md)
- [Repository Hygiene](repository-hygiene.md)
