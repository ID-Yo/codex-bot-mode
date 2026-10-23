# Agent Monitor

The **Agent Monitor** provides read-only, task-aware, and pause-aware activity observation across appointed project agents in Codex Bot Mode. It tracks task follow-through and reports fresh, deduplicated alerts exclusively to the authorized Chief of Staff.

## Architecture and Delegation Chain

The operational hierarchy in Codex Bot Mode follows a clear chain of authority:
- **Owner / Chief of Staff**: Allocates cross-project priorities and reviews operational health.
- **Project Manager**: Directs one project team, assigns work, and resolves blockers.
- **Project Agents**: Execute assigned tasks within designated boundaries.
- **Agent Monitor**: Observes agent activity metadata and alerts the Chief of Staff when execution stalls or quota fails.

```text
Chief of Staff <-------- [Alerts / Health Reports] <-------- Agent Monitor
      |                                                            ^
      v                                                            | (read-only metadata)
Project Managers -----------------------------------------> Project Agents
```

## Scope and Safeguards

- **Metadata Only & Discard Rule**: The Monitor inspects thread state and task tracking via supported app tools (such as `list_threads`). It explicitly discards thread titles, summaries, tool inputs/outputs, and conversation text before analysis and never persists them.
- **Zero Unsolicited Interventions**: The Monitor has zero authority to reassign tasks, edit product code, restart processes, or switch models.
- **Authorized Routing & Standing Approval**: All findings route exclusively to the configured `recipient` parameter (Chief of Staff). When `recipient` is empty, the Monitor enters `setup_required` state and emits zero alerts. Because heartbeat runs have no inbound requester message, alerts route exclusively to this target. The outside-project approval requirement in BASE.md is satisfied by this standing target authorization recorded at setup.
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
