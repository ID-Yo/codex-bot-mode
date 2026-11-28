"""Retry or skip an unconfirmed outbox row from the Office Delivery panel (#424, S4-E).

Office decides nothing itself and sends nothing to Desktop. It lists the rows of one project that wait for a decision and
passes a decision to the project's own installed outbox.py (list --state, resolve), the only writer of the queue. The
project is named by the caller but found only through the supervisor's fresh status file: a name that is not listed, or a
status that is missing or old (state unknown), resolves nothing. Every failure, timeout or unreadable answer is a refusal
and never a success. Each attempt is appended to .bot-mode/delivery-actions.jsonl (no report text, no paths, no token).
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

from . import supervisor_status

UNSURE_STATES = ("ambiguous", "needs-attention")
ACTIONS = {"retry": "retry", "skip": "skipped"}  # the panel word -> the outbox.py resolve word
ROW_ID = re.compile(r"[0-9a-f]{32}")
PROJECT_NAME = re.compile(r"[\w .-]{1,80}")
TIMEOUT_SECONDS = 30
LIST_LIMIT = 20  # the newest rows per state that outbox.py list shows
NOTE = "office panel"
JOURNAL = "delivery-actions.jsonl"
DECIDED = re.compile(r"Resolved ([0-9a-f]{32}): ([\w-]+)\.")
_journal_lock = threading.Lock()


class Refused(Exception):
    """The request was not carried out. status is the HTTP status, reason a short code for the journal. outcome_unknown is
    True when the resolve may or may not have taken effect (timeout or unclear answer), False when nothing was changed."""

    def __init__(self, status, message, reason, outcome_unknown=False):
        super().__init__(message)
        self.status, self.message, self.reason, self.outcome_unknown = status, message, reason, outcome_unknown


def _folder(root, project, now):
    """The project folder named in the supervisor's fresh status file, with an installed outbox.py."""
    result = supervisor_status.read(Path(root) / ".bot-mode" / "supervisor-status.json", now)
    if result["status"] is None:
        raise Refused(409, "Delivery status is unknown, so nothing can be decided now.", "status_unknown")
    entries = [entry for entry in result["status"].get("projects", [])
               if isinstance(entry, dict) and entry.get("name") == project and isinstance(entry.get("project"), str)]
    if not entries:
        raise Refused(404, "Unknown project.", "unknown_project")
    if len(entries) > 1:
        raise Refused(409, "The project name is not unique in the supervisor status.", "project_not_unique")
    folder = Path(entries[0]["project"])
    if not (folder / ".bot-mode" / "outbox.py").is_file():
        raise Refused(409, "The project has no installed outbox.py.", "no_outbox")
    return folder


def _run(folder, arguments, changes=False):
    """One call of the project's outbox.py without a shell and with a time limit: (exit code, stdout, stderr).
    changes is True for the resolve call: a timeout then leaves the outcome unknown."""
    command = [sys.executable, "-B", str(folder / ".bot-mode" / "outbox.py"), "--project", str(folder), *arguments]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                              timeout=TIMEOUT_SECONDS, creationflags=flags, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        if changes:
            raise Refused(503, "The project's outbox did not answer in time. The outcome is unknown; check the list.",
                          "timeout_outcome_unknown", True) from None
        raise Refused(503, "The project's outbox did not answer in time. Nothing was changed.", "list_timeout") from None
    except OSError:
        raise Refused(503, "The project's outbox could not be started.", "cannot_start") from None
    if done.returncode == 2 and "usage:" in done.stderr:  # argparse: this outbox.py does not know the command or option
        raise Refused(409, "The project's installed outbox.py is too old (no list --state or resolve). Update it first.", "outbox_too_old")
    return done.returncode, done.stdout, done.stderr


def _age(stamp, now):
    try:
        then = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, round((now - then).total_seconds()))


def waiting_rows(root, project, now=None):
    """The rows of one project that wait for a decision: id, state, sender, target, age. Never the text or the error."""
    now = now or datetime.now(timezone.utc)
    folder = _folder(root, project, now)
    rows = []
    for state in UNSURE_STATES:
        code, out, _ = _run(folder, ["list", "--state", state])
        try:
            items = json.loads(out)
        except ValueError:
            items = None
        if code != 0 or not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise Refused(503, "The project's outbox list could not be read.", "list_unreadable")
        for item in items:
            if isinstance(item.get("id"), str) and ROW_ID.fullmatch(item["id"]):
                rows.append({"id": item["id"], "state": state, "sender": str(item.get("sender_bot") or "")[:40],
                             "target": str(item.get("target_bot") or "")[:40], "age_seconds": _age(item.get("created_at"), now)})
    return sorted(rows, key=lambda row: -(row["age_seconds"] or 0))


def list_is_cut(rows):
    """True when a state shows as many rows as outbox.py list ever shows, so older rows of it may be missing."""
    return any(sum(1 for row in rows if row["state"] == state) >= LIST_LIMIT for state in UNSURE_STATES)


def _journal(root, entry):
    """Append one line to the Office journal. False when it cannot be written."""
    path = Path(root) / ".bot-mode" / JOURNAL
    try:
        with _journal_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def _decide(root, project, row_id, action, confirm, now, entry):
    if not isinstance(action, str) or action not in ACTIONS:
        raise Refused(400, "The action must be retry or skip.", "bad_action")
    if not isinstance(row_id, str) or not ROW_ID.fullmatch(row_id):
        raise Refused(400, "The report id is not valid.", "bad_id")
    if not isinstance(project, str) or not PROJECT_NAME.fullmatch(project):
        raise Refused(400, "The project name is not valid.", "bad_project")
    if confirm is not True:
        raise Refused(400, "The decision needs a confirmation.", "not_confirmed")
    folder = _folder(root, project, now)
    waiting = {row["id"]: row for row in waiting_rows(root, project, now)}
    if row_id not in waiting:
        raise Refused(409, "That report is not waiting for a decision (another state, or it does not exist).", "not_unconfirmed")
    entry["state_seen"] = waiting[row_id]["state"]
    code, out, err = _run(folder, ["resolve", row_id, "--as", ACTIONS[action], "--note", NOTE], changes=True)
    decided = DECIDED.search(out)
    if code == 2 and err.startswith("refused:"):
        raise Refused(409, "The report changed state before the decision reached the queue.", "state_changed")
    if code != 0 or not decided or decided.group(1) != row_id:
        raise Refused(503, "The project's outbox gave no clear answer. The outcome is unknown; check the list.", "resolve_unclear", True)
    return decided.group(2)


def resolve(root, project, row_id, action, confirm, now=None):
    """Retry or skip one row. Returns {'id', 'action', 'state'} with the state the outbox reported, or raises Refused.
    Every attempt, refused or not, is written to the journal."""
    now = now or datetime.now(timezone.utc)
    entry = {"at": now.isoformat(), "event": "resolve", "by": "office-owner",
             "project": project if isinstance(project, str) and PROJECT_NAME.fullmatch(project) else None,
             "id": row_id if isinstance(row_id, str) and ROW_ID.fullmatch(row_id) else None,
             "action": action if isinstance(action, str) and action in ACTIONS else None}
    try:
        state = _decide(root, project, row_id, action, confirm, now, entry)
    except Refused as refusal:
        _journal(root, {**entry, "result": "refused", "status": refusal.status, "reason": refusal.reason,
                        "outcome": "unknown" if refusal.outcome_unknown else "none"})
        raise
    journaled = _journal(root, {**entry, "result": "resolved", "state": state, "outcome": "done"})
    return {"id": row_id, "action": action, "state": state, "journal_written": journaled}
