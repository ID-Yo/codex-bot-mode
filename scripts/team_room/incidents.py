"""One alert per incident for the supervisor (#424, S4-B).

relay_watchdog.py finds problems every pass. Without memory it would announce the same fault again whenever it flaps.
This module gives every problem a stable key (no ages, no pids), keeps the open incidents in .bot-mode/local/incidents.json
and decides what is due:

- a key that appears opens an incident: ONE row to the project's Manager (outbox, dedupe key incident:<key>:<opened>:<n>),
  shown in the health file for Office; a reminder row after REMIND_AFTER_SECONDS while it is still open;
- a fault that needs the owner (severity 'owner': the relay is down and the supervisor could not bring it back) additionally
  gets ONE Windows message, only after OWNER_AFTER_SECONDS, so a fault that heals itself never pops up;
- a key that disappears is not closed at once: it closes after GRACE_SECONDS without it and at least CLEAN_PASSES passes
  without it, and a return before that is the same incident (no new alert). A flapping fault is therefore one incident,
  not one alert per flap;
- severity 'info' is recorded and shown, never sent.

The state functions are pure (state in, state out); relay_watchdog.py runs the actions and calls mark() after each success,
so a failed delivery is tried again in the next pass (the outbox dedupe key makes a repeated row harmless).
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

SCHEMA = 1
GRACE_SECONDS = 15 * 60
CLEAN_PASSES = 3  # a fault also has to be absent from this many passes before it closes (a long gap between passes is not proof)
OWNER_AFTER_SECONDS = 10 * 60
REMIND_AFTER_SECONDS = 6 * 3600
OWNER, MANAGER, INFO = "owner", "manager", "info"
SENDER = "supervisor"
SEVERITIES = (OWNER, MANAGER, INFO)
MACHINE_KEYS = frozenset(("relay-down", "relay-wrong-manager", "office-down", "desktop-closed"))  # not about one project
PROJECT_PREFIX = re.compile(r"^([^:\s][^:]{0,79}): ")
NO_OUTBOX = "no outbox installed"  # queue_row: nothing to queue and nothing to retry
NO_MANAGER = "no Manager in the roster"  # queue_row: nothing to queue yet, tried again in the next pass

# (pattern on the problem text without its "<project>: " prefix, key, severity, remedy)
RULES = (
    (r"^Desktop relay down and could not be restarted", "relay-down", OWNER,
     "Check that Codex Desktop is running and signed in, then run: python -B scripts/relay_watchdog.py --restart"),
    (r"^Desktop relay serves a replaced Manager chat", "relay-wrong-manager", OWNER,
     "Run: python -B scripts/relay_watchdog.py --restart"),
    (r"^Codex Office does not answer", "office-down", MANAGER, "Start it: powershell -File scripts/start_codex_office.ps1 -NoBrowser"),
    (r"^Codex Desktop is not running and reports are waiting", "desktop-closed", INFO,
     "Start Codex Desktop; the waiting reports are delivered when it runs."),
    (r"requests\.py is installed but \.bot-mode/outbox\.py is missing", "outbox-missing", MANAGER,
     "Install .bot-mode/outbox.py and outbox-config.json in the project (#415)."),
    (r"^installed .* differ from the repo", "outbox-drift", MANAGER,
     "Bring the installed outbox.py and requests.py up to date through the guarded release."),
    (r"unconfirmed delivery is being reconciled", "ambiguous", INFO, "The pump checks the thread by itself; nothing to do unless it becomes needs-attention."),
    (r"could not be confirmed and need a decision", "needs-attention", MANAGER,
     "Run outbox.py list --state needs-attention, then outbox.py resolve <id> --as delivered|skipped|retry."),
    (r"no pump is running", "pump-down", MANAGER, "outbox.py status --heal starts a pump; the supervisor does it every pass."),
    (r"oldest pending row is", "pending-stale", INFO, "Check the Manager thread: relay and pump are up but nothing is delivered."),
    (r"^outbox canary has waited", "outbox-canary", MANAGER,
     "The zero-token canary is not processed: run outbox.py status in the project and check the relay, the pump and the Manager chat. "
     "A Manager that is only busy does not cause this."),
    (r"holds the lock but wrote no heartbeat", "pump-stuck", MANAGER, "End the pump process named in the message; the supervisor starts a new one."),
    (r"^health check failed", "check-failed", MANAGER, "Run python -B scripts/relay_watchdog.py --project <project> and read its error."),
    (r"^outbox status failed", "status-failed", MANAGER, "Run outbox.py status in the project and read its error."),
)


def classify(text, project_names=()):
    """{'key', 'severity', 'remedy', 'text', 'project'} for one problem text. project is the name from a '<name>: ' prefix: a
    registered project's name, or for any other project-scoped problem the prefix itself (a project whose outbox is missing is by
    definition not among the registered ones, and two such projects must stay two incidents)."""
    text = str(text)
    project = next((name for name in project_names if text.startswith(name + ": ")), None)
    body = text[len(project) + 2:] if project else text
    for pattern, key, severity, remedy in RULES:
        if re.search(pattern, body):
            break
    else:
        key, severity, remedy = "other:" + re.sub(r"[^a-z]+", "-", re.sub(r"\d+", "", body.lower())).strip("-")[:40], MANAGER, "Read the problem text."
    if project is None and key not in MACHINE_KEYS and not key.startswith("other:"):
        found = PROJECT_PREFIX.match(text)
        project = found.group(1) if found else None
    return {"key": key + (":" + project if project else ""), "severity": severity, "remedy": remedy, "text": text, "project": project}


def empty():
    return {"schema": SCHEMA, "incidents": {}}


def load(path):
    return read(path)[0]


def _iso(value):
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except (TypeError, ValueError):
        return False


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _valid(key, incident):
    """True when a stored incident has every field step() and summary() read, with the types they need."""
    if not isinstance(incident, dict) or incident.get("key") != key or incident.get("severity") not in SEVERITIES:
        return False
    if not (_iso(incident.get("opened")) and _iso(incident.get("last_seen"))):
        return False
    if not (isinstance(incident.get("text"), str) and isinstance(incident.get("remedy"), str)):
        return False
    if incident.get("absent_since") is not None and not _iso(incident["absent_since"]):
        return False
    if incident.get("project") is not None and not isinstance(incident["project"], str):
        return False
    if any(name in incident and not _int(incident[name]) for name in ("reopened", "absent_passes")):
        return False
    done = incident.get("done", {})
    return isinstance(done, dict) and ("row" not in done or _int(done["row"])) and all(
        isinstance(done[name], str) for name in ("popup", "skipped") if name in done)


def read(path):
    """(state, dropped): the stored state with every damaged record removed, and the keys of the records that were removed.
    A missing, unreadable or foreign file gives the empty state. A damaged record (missing or mistyped field) is dropped on its own:
    the incident it described opens again as a new one if its problem is still there, so one bad record cannot stop a pass."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty(), []
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("incidents"), dict):
        return empty(), []
    kept = {key: value for key, value in data["incidents"].items() if _valid(key, value)}
    return {"schema": SCHEMA, "incidents": kept}, sorted(str(key) for key in data["incidents"] if key not in kept)


def save(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def log_event(path, event):
    """One line per event (opened, owner message, closed) in incidents.jsonl, never rewritten."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def _seconds(since, now):
    return (now - datetime.fromisoformat(since)).total_seconds()


def step(state, problems, now):
    """Fold this pass's problems (classify() dicts) into the state. Returns (events, actions):
    events are 'opened' / 'returned' / 'closed' dicts for the log; actions are what is due now:
    {'do': 'manager_row', 'key', 'n', 'upto'} (n 0 = the first row, 1.. = reminders; upto is what mark() records, so a first row
    that was late does not trigger a reminder right behind it) and {'do': 'popup', 'key'}."""
    stamp = now.isoformat()
    incidents = state["incidents"]
    present = {}
    for problem in problems:
        present.setdefault(problem["key"], problem)
    events, actions = [], []
    for key, problem in present.items():
        incident = incidents.get(key)
        if incident is None:
            incidents[key] = {"key": key, "opened": stamp, "last_seen": stamp, "text": problem["text"], "severity": problem["severity"],
                              "remedy": problem["remedy"], "project": problem["project"], "reopened": 0, "absent_since": None, "done": {}}
            events.append({"at": stamp, "event": "opened", "key": key, "severity": problem["severity"], "text": problem["text"]})
            continue
        if incident.get("absent_since"):
            incident["reopened"] = incident.get("reopened", 0) + 1
            incident["absent_since"] = None
            incident["absent_passes"] = 0
            events.append({"at": stamp, "event": "returned", "key": key, "reopened": incident["reopened"]})
        incident.update(last_seen=stamp, text=problem["text"], severity=problem["severity"], remedy=problem["remedy"])
    for key in list(incidents):
        if key in present:
            continue
        incident = incidents[key]
        if not incident.get("absent_since"):
            incident["absent_since"] = stamp
            incident["absent_passes"] = 1
            continue
        incident["absent_passes"] = incident.get("absent_passes", 1) + 1
        if _seconds(incident["absent_since"], now) >= GRACE_SECONDS and incident["absent_passes"] >= CLEAN_PASSES:
            lasted = (datetime.fromisoformat(incident["absent_since"]) - datetime.fromisoformat(incident["opened"])).total_seconds()
            events.append({"at": stamp, "event": "closed", "key": key, "open_seconds": round(lasted), "reopened": incident.get("reopened", 0)})
            del incidents[key]
    for key, incident in incidents.items():
        if incident.get("absent_since"):
            continue  # inside the grace window nothing is announced
        age = _seconds(incident["opened"], now)
        done = incident.setdefault("done", {})
        if incident["severity"] in (MANAGER, OWNER):
            due = int(age // REMIND_AFTER_SECONDS)
            sent = done.get("row")
            if sent is None or due > sent:
                actions.append({"do": "manager_row", "key": key, "n": 0 if sent is None else due, "upto": due})
        if incident["severity"] == OWNER and age >= OWNER_AFTER_SECONDS and "popup" not in done:
            actions.append({"do": "popup", "key": key})
    return events, actions


def mark(state, key, what, now, upto=None):
    """Record that an action was carried out ('row' with the reminder number it covers, or 'popup')."""
    incident = state["incidents"].get(key)
    if incident is not None:
        incident.setdefault("done", {})[what] = upto if what == "row" else now.isoformat()


def row_text(incident, n=0):
    head = "[Supervisor alert]" if not n else f"[Supervisor reminder {n}]"
    return (f"{head} {incident['severity']} incident {incident['key']}, open since {incident['opened']}. {incident['text']} "
            f"Remedy: {incident['remedy']} One message per incident; a reminder follows after {REMIND_AFTER_SECONDS // 3600} h if it is still open.")


def dedupe_key(incident, n=0):
    return f"incident:{incident['key']}:{incident['opened']}:{n}"


def queue_row(root, text, key):
    """Queue one report for the Manager of the project at root. Returns (ok, detail). Nothing is queued where no outbox is installed
    (detail starts with NO_OUTBOX) or where the roster names no Manager (NO_MANAGER): a row without a target could never be delivered."""
    from . import outbox
    root = Path(root)
    if not (root / ".bot-mode" / "outbox.py").is_file():
        return False, f"{NO_OUTBOX} in {root.name}"
    thread = outbox.resolve_target_thread(root, "manager")
    if not thread:
        return False, f"{NO_MANAGER} of {root.name}"
    db = outbox.OutboxDB(root / ".bot-mode" / "outbox" / "queue.sqlite")
    try:
        db.enqueue(SENDER, "manager", thread, text, key)
    finally:
        db.close()
    return True, "queued"


def summary(state):
    """Open incidents for the health file and Office: stable order, no internals."""
    rows = []
    for key in sorted(state["incidents"]):
        incident = state["incidents"][key]
        rows.append({"key": key, "severity": incident["severity"], "opened": incident["opened"], "reopened": incident.get("reopened", 0),
                     "text": incident["text"], "remedy": incident["remedy"], "clearing": bool(incident.get("absent_since")),
                     "manager_notified": "row" in incident.get("done", {}), "owner_notified": "popup" in incident.get("done", {})})
    return rows
