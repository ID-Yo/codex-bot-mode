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
| `ambiguous` | The outcome of the send is unknown (error or timeout after the relay was called, or the pump stopped while sending). Being reconciled; holds later rows for the same recipient. |
| `needs-attention` | An ambiguous row that stayed unresolved for the attention time (default 30 min). Shown by `status` and `list`; holds nothing. |
| `skipped` | An operator decided never to send it. |

**Ambiguous delivery is reconciled automatically (#422).** Every delivery begins with a stable `[Outbox delivery: <id>]` marker. The pump reads the actual destination thread (one history read per thread, up to 5 pages of 50 turns) at its start, after every failed send and every 30 s while rows are unresolved, and decides from evidence only:

- A user message with the marker proves delivery: the row becomes `delivered` (also a `needs-attention` row, later). Only messages that start with an outbox marker or digest header count; a marker quoted in another message or echoed by an agent is not proof.
- No marker proves non-delivery only when the read was **complete, well formed and unredacted**, the row is still `ambiguous` and the retry grace (default 10 min since it became ambiguous) has passed. Complete: every reply has `turns` and `items` as lists and `page` as a mapping, and the last page names no further cursor within the bound of 5 pages. Unredacted: at least one user message came back with more than one character of text, because the relay reduces a chat that is only read-only reachable to one character per item and no marker can be found in that (the relay's read policy is not changed). Then the row goes back to `pending` at its place and is sent again (note `auto-retry` on the row). A malformed, cut-off, redacted, bounded, empty or unreadable history proves nothing: the row stays `ambiguous` (later `needs-attention`) and is not resent; a marker found on the pages that were read still proves delivery. A grace of 0 turns automatic retry off.
- **Unresolved rows are looked at again.** A pump that has nothing else to deliver rereads the history of unresolved rows every 30 s, 3 times (`rechecks` in `outbox-config.json`), then ends; a late marker still turns the row into delivered without any new report. `outbox.py status --heal` starts such a pump for a queue that holds only ambiguous or needs-attention rows (the watchdog runs it every 5 minutes). Rows older than the TTL (12 h) are left to the operator, so a row that never resolves does not cause rereads for ever; `status` shows the count of rows still worth a reread as `recheckable`.
- Residual risk, stated plainly: if the relay accepts a stale send much later than the grace period (for example after a long stall), the Manager may see a report twice. A duplicate report is the price of never losing one; every report carries its marker and id.

**An unresolved row holds only its own recipient.** While a row is ambiguous, later pending rows for the same recipient (same target name or same thread) wait, so the order is kept and a late arrival cannot overtake. Rows for other recipients are delivered as usual; nothing holds the whole project.

**Needs attention.** A row still ambiguous after the attention time (default 30 min, `--attention-minutes` or `attention_minutes` in `outbox-config.json`) becomes `needs-attention`: `outbox.py status` counts it and reports a problem, `outbox.py list --state needs-attention` shows it, and it no longer holds later rows. It is never resent without proof. `status` applies this change itself, so it also happens when no pump runs. An operator decides:

```text
python -B .bot-mode/outbox.py --project <project> resolve <id> --as delivered   # the report arrived
python -B .bot-mode/outbox.py --project <project> resolve <id> --as skipped     # never send it
python -B .bot-mode/outbox.py --project <project> resolve <id> --as retry       # it did not arrive: send it again, in its place
```

`resolve` accepts an ambiguous or needs-attention row (and `retry` also a failed one), takes `--note` for a reason, and starts the pump again when rows are pending.

**Digest.** When several rows wait for the same recipient (typically because it was busy), the pump sends them as one message in creation order: a header `[Outbox digest: N reports, oldest first]`, then per report its marker line, `From: <sender> (<time>)` and the text. It is one Manager turn, so the pump waits for one turn marker, and each row keeps its own state (all `sending`, then all `delivered`, or all `ambiguous` together; each row is confirmed on its own by its marker). A digest never mixes recipients, never passes an earlier ambiguous row of its recipient, and stops at 20 reports or about 24000 characters (**--digest-max**, `digest_max`; 1 sends rows one by one). A single row is sent exactly as before.

**Failed and ambiguous rows keep their dedupe key.** The key is `request:<id>:<by>:<outcome>`, so the same report is not queued twice, also when two senders race (the second one gets the existing row). A failed or ambiguous row is re-queued only by `resolve --as retry`; the row keeps its original place in the order, so reports queued after a failed row may already have been sent by then. Fix the cause first (for example the Manager binding in the roster).

**One pump per project.** The pump holds an operating system lock on `.bot-mode/outbox/pump.lock`. The system releases it when the pump ends or dies, so there is no stale lock to take over and no process id to reuse. A second pump exits at once with code 0. A row left `sending` by a dead pump (every row of a digest) is turned into `ambiguous` by the next pump and then reconciled as above.

**Loopback relay only.** The relay descriptor (`relay.json`) is used only when its URL is plain `http` to `127.0.0.1`, `localhost` or `::1` without credentials, query or fragment. Any other descriptor is ignored with a message that never prints the token, and the client does not follow redirects, so the relay token is not sent to another host.

## Install and limits

- A new project launch (`install-team`, Director or agent install) installs `.bot-mode/outbox.py` and, when missing, `.bot-mode/outbox-config.json` (`relay_root` of the CBM checkout) next to `roster.py` and `requests.py`, and records all of them as tool components in the installation inventory (#415). Existing projects receive the files and the updated BASE through the guarded release procedure after Review and QA; the watchdog reports a registered project that has `requests.py` but no `outbox.py`.
- Not verified here: a live relay, the real Manager thread, and an installed project. The tests use a fake relay that registers and finishes turns after a number of reads.
- The Agent Monitor outbox that forwards Monitor reports is a different mechanism (`docs/operations/agent-monitor.md`).


## Reliability (#401)

The Desktop relay is a critical component: without it nothing is delivered. Since #421 it does not need the Manager chat: the standalone relay finds the Desktop tool pipe by name and takes the Manager from the roster, so the supervisor task can restart it. Registering it from the Manager chat remains a fallback. The design is detect-early, heal where possible and never fail silently.

**Recipients.** The relay sends a report only to a chat that is appointed: the relay project's own roster, the registry Manager of each registered project, and every other chat bound in a registered project's roster (a Director, a sub-Manager, a worker; #417). A thread that is bound nowhere is refused with HTTP 400 and its row is quarantined as ambiguous. Waiting on a chat and reading its content keep the stricter rules (appointed chats only; other bound chats are readable as thread status and turn metadata).

- **Pump waits.** While the relay is down the pump keeps running, re-reads the relay descriptor with a capped backoff (a restarted relay has a new port and token) and writes `.bot-mode/outbox/pump.heartbeat.json`.
- **TTL.** A pending row older than 12 h becomes `expired` (`outbox.py list --state expired`).
- **Retention (#422).** `outbox.py prune [--keep N] [--max-backups M] [--ttl-hours H] [--dry-run]` expires old pending rows (the TTL above) and deletes all but the newest N delivered rows (default 200), and the same for skipped and expired rows. Pending, sending, ambiguous, needs-attention and failed rows are never deleted. Whenever anything would change, the database is first copied with the SQLite backup API to `.bot-mode/outbox/backups/queue-<time>.sqlite`; only the newest M copies (default 5) are kept. A deleted row frees its dedupe key, so an old report could be queued again. A pump that finds more than twice N delivered rows at its start prunes by itself, so the file stays bounded without a person. `--dry-run` prints the counts and changes nothing.
- **Settings.** `.bot-mode/outbox-config.json` may hold `attention_minutes`, `retry_grace_minutes`, `digest_max`, `keep_rows` and `max_backups` next to `relay_root`; a command line option wins, then the file, then the default. The `send`, `list`, `status`, `pump` and `resolve` commands keep their arguments and output; `needs_attention` is a new field of the `status` JSON.
- **Status.** `outbox.py status [--heal]` prints JSON and exits 5 when work waits without a reachable relay or a running pump, or when an ambiguous or needs-attention row needs a look. `--heal` starts a missing pump, including to reconcile ambiguous rows.
- **Send is loud.** `send` prints the queue depth and oldest age, and warns when the relay is unreachable so the agent also writes directly into the Manager thread.
- **Supervisor.** `scripts/relay_watchdog.py --all-projects --restart [--notify]` probes the relay, restarts it without the Manager chat when it is down or serves a replaced Manager chat, runs `status --heal` per project (a pump that holds its lock without a heartbeat for 180 s is ended and replaced, but only when the process is proven to be this project's pump; an unprovable one is reported and left alone), compares installed `outbox.py`/`requests.py` with the repo and writes `.bot-mode/relay-health.json`. The task `Codex Bot Mode Supervisor` (`scripts/supervisor_task.ps1`) runs it without a model every 2 minutes and at logon, hidden, also on battery. While Codex Desktop is closed nothing can be delivered; that is reported as waiting, and as a problem only when reports are waiting. Codex Office is optional: its state is in the report, and it raises an alarm only when the task was installed with `-RestartOffice`. A relay that still serves a replaced Manager chat is never reported healthy.

