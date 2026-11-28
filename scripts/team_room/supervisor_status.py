"""The supervisor's status file (#424, S4-C): what the machine-level supervisor saw in its last pass.

scripts/relay_watchdog.py writes .bot-mode/supervisor-status.json after every pass (atomically); Codex Office and the
doctor read it. relay-health.json stays as it is (older readers); this file adds the pass time, the expected interval, the
reason of the last restart and a per-project summary.

A reader must never treat a missing, unreadable, foreign or old file as "healthy": read() returns state 'unknown' with the
reason. A file is old when it is more than STALE_FACTOR intervals old, because a supervisor that stopped running cannot
report its own death.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

SCHEMA = 1
DEFAULT_INTERVAL = 120  # seconds, the scheduled task repeats every 2 minutes
STALE_FACTOR = 3  # older than this many intervals: Unknown
CLOCK_SKEW = 120  # a file dated further than this in the future is not trusted
PROJECT_FIELDS = ("pending", "oldest_pending_seconds", "ambiguous", "needs_attention", "relay_reachable", "pump_alive",
                  "last_delivered_at")
TEXT_LIMIT = 300


def _text(value):
    return str(value)[:TEXT_LIMIT] if value is not None else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _carry(previous, *path):
    value = previous
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, dict) else None


def pump_action(project_report, at):
    """The last thing the supervisor did to a pump in this project in this pass, or None."""
    hung = project_report.get("hung_pump")
    if isinstance(hung, dict):
        return {"at": at, "project": project_report.get("project"), "action": "stuck pump " + ("replaced" if hung.get("stopped") else "not stopped"),
                "reason": _text(hung.get("reason")), "pid": hung.get("pid")}
    if (project_report.get("outbox") or {}).get("healed"):
        return {"at": at, "project": project_report.get("project"), "action": "pump started", "reason": "work was waiting and no pump ran"}
    return None


def canary_facts(canary):
    """The canary fields a reader may use (#426): when it last succeeded, its latency, the chat status it saw and how long one
    has been waiting. None when no canary ran (old outbox.py, or switched off): unknown, never a good result."""
    if not isinstance(canary, dict):
        return None
    return {"last_ok_at": _text(canary.get("last_ok_at")), "last_latency_seconds": _number(canary.get("last_latency_seconds")),
            "last_read_seconds": _number(canary.get("last_read_seconds")), "last_chat_status": _text(canary.get("last_chat_status")),
            "pending_seconds": _number(canary.get("pending_seconds"))}


def build(report, previous=None, *, at, pass_seconds, interval_seconds=DEFAULT_INTERVAL, pid=None, version=None, restart_reason=None):
    """The status document for one supervisor pass. report is the watchdog's own report dict; previous is the last status
    document (or None): the reasons of earlier restarts are kept until a newer one replaces them."""
    stamp = at.isoformat() if hasattr(at, "isoformat") else str(at)
    restart = report.get("restart")
    last_restart = _carry(previous, "relay", "last_restart")
    if isinstance(restart, dict):
        last_restart = {"at": stamp, "reason": restart_reason or "relay down", "ok": bool(restart.get("ok")), "detail": _text(restart.get("detail"))}
    last_pump = _carry(previous, "last_pump_action")
    projects = []
    for entry in report.get("projects", []):
        outbox = entry.get("outbox") or {}
        projects.append({"project": entry.get("project"), "name": Path(str(entry.get("project", ""))).name,
                         "canary": canary_facts(entry.get("canary")),
                         **{key: outbox.get(key) for key in PROJECT_FIELDS},
                         "problems": [str(p) for p in outbox.get("problems", [])]})
        action = pump_action(entry, stamp)
        if action:
            last_pump = action
    return {"schema": SCHEMA, "at": stamp, "pass_seconds": round(float(pass_seconds), 2), "interval_seconds": int(interval_seconds),
            "supervisor": {"pid": pid, "version": version},
            "desktop_running": report.get("desktop_running"), "office_ok": report.get("office_ok"),
            "relay": {"ok": bool(report.get("relay_ok")), "manager_changed": bool(report.get("manager_changed")), "last_restart": last_restart},
            "last_pump_action": last_pump, "projects": projects, "problems": [str(p) for p in report.get("problems", [])],
            "incidents_error": _text(report.get("incidents_error"))}


def write(path, data):
    """Atomic write: a reader sees the old or the new document, never half of one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def load(path):
    """The stored document, or None when it is missing, unreadable or not a schema-1 document."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None


def read(path, now=None):
    """{'state': 'ok'|'problems'|'unknown', 'reason': text, 'age_seconds': number|None, 'status': document|None}.
    Only a fresh document can be 'ok' or 'problems'; everything else is 'unknown', never healthy."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _unknown("the supervisor has not written a status file")
    except (OSError, ValueError):
        return _unknown("the supervisor status file cannot be read")
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        return _unknown("the supervisor status file has an unsupported schema")
    try:
        written = datetime.fromisoformat(raw["at"])
        if written.tzinfo is None:
            raise ValueError("no time zone")
        interval = int(raw.get("interval_seconds") or DEFAULT_INTERVAL)
    except (KeyError, TypeError, ValueError):
        return _unknown("the supervisor status file has no valid time")
    age = ((now or datetime.now(timezone.utc)) - written).total_seconds()
    if age < -CLOCK_SKEW:
        return _unknown("the supervisor status file is dated in the future", age)
    limit = STALE_FACTOR * max(interval, 1)
    if age > limit:
        return _unknown(f"the supervisor last ran {int(age)} s ago, expected every {interval} s; it may not be running", age)
    problems = raw.get("problems")
    state = "problems" if problems else "ok"
    return {"state": state, "reason": "; ".join(str(p) for p in problems) if problems else "healthy", "age_seconds": round(max(age, 0.0)), "status": raw}


def _unknown(reason, age=None):
    return {"state": "unknown", "reason": reason, "age_seconds": None if age is None else round(age), "status": None}
