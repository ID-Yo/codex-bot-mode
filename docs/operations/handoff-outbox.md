# Handoff outbox (#399)

The handoff outbox lets an agent report to its project Manager and end its turn at once, without waiting for the Manager and without spending model tokens on polling. A local script queues the report on disk; a small local pump (no model calls) delivers the queued reports to the Manager thread one at a time.

Source files: `scripts/team_room/outbox.py` (queue, pump and CLI) and `scripts/team_room/requests.py` (queues a report when a request is closed). This text describes source behaviour. It is not evidence that any project has the outbox installed or running.

## Flow

1. `requests.py reply` or `requests.py verdict` closes the request in the ledger first. After that it queues one short report (request id, who closed it, outcome, task summary, evidence link, next owner) with `.bot-mode/outbox.py send` and starts a background pump.
2. The queue is a SQLite file, `.bot-mode/outbox/queue.sqlite`, with the rows in arrival order.
3. The pump reads the Manager thread through the local Desktop Relay. It sends the next report only when the thread is idle (a chat that Desktop has not loaded, status `notLoaded`, counts as idle when its newest turn is not in progress), then waits until that message shows up as a new Manager turn (a new turn id, or the thread reported active) and until that turn has finished, before it considers the next report. Reports from several agents therefore arrive as separate Manager turns and never as steers inside a running turn.
4. Anything the pump cannot prove is treated as not safe to send: an unknown thread status, a read error (it backs off), or no new Manager turn within `--turn-timeout` seconds (default 120). In the last case the report counts as delivered, the pump stops with exit code 4 and leaves the remaining rows pending; run the pump again after looking at the Manager thread.

The target is always the project Manager, resolved from `roster.json` (or `roster.py links`) when the pump needs it, so a replaced Manager chat is picked up at the next delivery. A report is not routed to the requester of the request: the ledger row and its reply link stay the record for any other requester, and the Manager is the single reader of the queue.

## A closed request is never reopened by the outbox

If queueing fails after the ledger was closed (the outbox command exits non-zero, times out or cannot start), `reply` and `verdict` still print the closed request and exit 0. They write a warning to stderr that starts with `warning: request <id> is closed` and contains the exact `outbox.py send` command to queue the report by hand. Do not repeat the closeout (for `verdict` that would post a second comment). A repeated `reply` with the same outcome is harmless: it keeps the ledger row and queues the report again under the same dedupe key. A project without `.bot-mode/outbox.py` queues nothing; `reply` and `verdict` then print `warning: request <id> is closed, but this project has no .bot-mode/outbox.py` (the closed request is never reopened). A task that was assigned without a ledger entry never reaches `reply`; the agent queues its report with `outbox.py send --to <requester or manager> --by <bot> --text ...` (shared BASE, 'Reporting back and delivery').

## States and rules

| State | Meaning |
|---|---|
| `pending` | Waiting for its turn. |
| `sending` | The pump has called the relay and has no answer yet. Opening the queue from `send` or `list` never changes it. |
| `delivered` | The relay accepted the message. |
| `failed` | Terminal: the target thread could not be resolved. |
| `ambiguous` | The outcome of the send is unknown (error or timeout after the relay was called, or the pump stopped while sending). |
| `skipped` | An operator decided never to send it. |

**Ambiguous delivery is quarantined.** Every new delivery begins with a stable `[Outbox delivery: <id>]` marker. The pump never automatically resends an ambiguous row. It checks the actual destination thread for the marker in a user message and marks a matching row delivered; a missing marker in a bounded or unavailable history is not proof of non-delivery. Later pending rows can overtake an ambiguous row, so strict ordering is not guaranteed. `outbox.py status` reports unresolved rows and `outbox.py list --state ambiguous` shows them. An operator checks the destination and decides:

```text
python -B .bot-mode/outbox.py --project <project> resolve <id> --as delivered   # the report arrived
python -B .bot-mode/outbox.py --project <project> resolve <id> --as skipped     # never send it
python -B .bot-mode/outbox.py --project <project> resolve <id> --as retry       # it did not arrive: send it again, in its place
```

`resolve` only accepts an ambiguous row (and `retry` also a failed one), takes `--note` for a reason, and starts the pump again when rows are pending. Never choose retry solely because a marker was not found in a bounded history: the delivery could still arrive later.

**Failed and ambiguous rows keep their dedupe key.** The key is `request:<id>:<by>:<outcome>`, so the same report is not queued twice, also when two senders race (the second one gets the existing row). A failed or ambiguous row is re-queued only by `resolve --as retry`; the row keeps its original place in the order, so reports queued after a failed row may already have been sent by then. Fix the cause first (for example the Manager binding in the roster).

**One pump per project.** The pump holds an operating system lock on `.bot-mode/outbox/pump.lock`. The system releases it when the pump ends or dies, so there is no stale lock to take over and no process id to reuse. A second pump exits at once with code 0. A row left `sending` by a dead pump is turned into `ambiguous` by the next pump, never sent again.

**Loopback relay only.** The relay descriptor (`relay.json`) is used only when its URL is plain `http` to `127.0.0.1`, `localhost` or `::1` without credentials, query or fragment. Any other descriptor is ignored with a message that never prints the token, and the client does not follow redirects, so the relay token is not sent to another host.

## Install and limits

- A new project launch (`install-team`, Director or agent install) installs `.bot-mode/outbox.py` and, when missing, `.bot-mode/outbox-config.json` (`relay_root` of the CBM checkout) next to `roster.py` and `requests.py`, and records all of them as tool components in the installation inventory (#415). Existing projects receive the files and the updated BASE through the guarded release procedure after Review and QA; the watchdog reports a registered project that has `requests.py` but no `outbox.py`.
- Not verified here: a live relay, the real Manager thread, and an installed project. The tests use a fake relay that registers and finishes turns after a number of reads.
- The Agent Monitor outbox that forwards Monitor reports is a different mechanism (`docs/operations/agent-monitor.md`).


## Reliability (#401)

The Desktop relay is a critical component: without it nothing is delivered. It can only be registered from the appointed Manager chat (it needs that chat's own app tools pipe), so a scheduled task cannot restart it. The design is therefore detect-early, heal where possible and never fail silently.

- **Pump waits.** While the relay is down the pump keeps running, re-reads the relay descriptor with a capped backoff (a restarted relay has a new port and token) and writes `.bot-mode/outbox/pump.heartbeat.json`.
- **TTL.** A pending row older than 12 h becomes `expired` (`outbox.py list --state expired`).
- **Status.** `outbox.py status [--heal]` prints JSON and exits 5 when work waits without a reachable relay or a running pump, or when an ambiguous row needs attention. `--heal` starts a missing pump, including to reconcile ambiguous rows.
- **Send is loud.** `send` prints the queue depth and oldest age, and warns when the relay is unreachable so the agent also writes directly into the Manager thread.
- **Watchdog.** `scripts/relay_watchdog.py --project <root> [--restart] [--notify]` probes the relay, runs `status --heal` per project, compares installed `outbox.py`/`requests.py` with the repo and writes `.bot-mode/relay-health.json`. A scheduled task runs it without a model every 5 minutes (alert only); the Manager chat runs it with `--restart` from a heartbeat, which is the only place the relay can be restarted.

