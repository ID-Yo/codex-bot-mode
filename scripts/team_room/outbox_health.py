"""What the Office delivery panel shows (#424, S4-D): the supervisor's status file, read-only.

Office never calls Desktop or the relay for this. It reads .bot-mode/supervisor-status.json of the checkout it runs from
(written by scripts/relay_watchdog.py, see supervisor_status.py) and, when that file is fresh, the open incidents the
supervisor listed in relay-health.json. A missing, unreadable or old file is state 'unknown' with the reason and no
numbers: unknown is not zero and never healthy. Only whitelisted fields leave this module (no file paths).
"""
from datetime import datetime, timezone
import json
from pathlib import Path

from . import supervisor_status

SCHEMA = 1


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value, limit=300):
    return value[:limit] if isinstance(value, str) else None


def _texts(values):
    return [_text(item) for item in values if isinstance(item, str)][:20] if isinstance(values, list) else []


def _age(stamp, now):
    try:
        then = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        return None
    return max(0, round((now - then).total_seconds()))


def _action(value):
    """A restart or pump action without its project path (paths stay out of the API)."""
    if not isinstance(value, dict):
        return None
    row = {key: _text(value.get(key)) for key in ("at", "action", "reason", "detail")}
    row["ok"] = value["ok"] if isinstance(value.get("ok"), bool) else None
    return row


def _canary(value, now):
    """The zero-token canary of one project (#426): the latency of the last one and its age, or None when the supervisor has no
    canary for it (an old outbox.py, or switched off): unknown, never a good result."""
    if not isinstance(value, dict):
        return None
    return {"last_ok_age_seconds": _age(value.get("last_ok_at"), now), "last_latency_seconds": _number(value.get("last_latency_seconds")),
            "last_chat_status": _text(value.get("last_chat_status"), 40), "pending_seconds": _number(value.get("pending_seconds"))}


def _project(row, now):
    return {"name": _text(row.get("name")) or "?", "pending": _number(row.get("pending")),
            "oldest_pending_seconds": _number(row.get("oldest_pending_seconds")), "ambiguous": _number(row.get("ambiguous")),
            "needs_attention": _number(row.get("needs_attention")),
            "relay_reachable": row.get("relay_reachable") if isinstance(row.get("relay_reachable"), bool) else None,
            "pump_alive": row.get("pump_alive") if isinstance(row.get("pump_alive"), bool) else None,
            "last_delivery_age_seconds": _age(row.get("last_delivered_at"), now), "canary": _canary(row.get("canary"), now),
            "problems": _texts(row.get("problems"))}


def _incidents(health_file, limit_seconds, now):
    """Open incidents from relay-health.json (#439), only while that file is as fresh as the status file."""
    try:
        data = json.loads(Path(health_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    age = _age(data.get("at"), now) if isinstance(data, dict) else None
    if age is None or age > limit_seconds or not isinstance(data.get("incidents"), list):
        return []
    rows = []
    for item in data["incidents"][:20]:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            rows.append({"key": _text(item["key"]), "severity": _text(item.get("severity")), "opened": _text(item.get("opened")),
                         "reopened": _number(item.get("reopened")), "text": _text(item.get("text")), "remedy": _text(item.get("remedy")),
                         "clearing": item.get("clearing") is True})
    return rows


def build(root, now=None):
    """The delivery panel model for the Office checkout at root."""
    now = now or datetime.now(timezone.utc)
    folder = Path(root) / ".bot-mode"
    result = supervisor_status.read(folder / "supervisor-status.json", now)
    model = {"schema": SCHEMA, "observed_at": now.isoformat(), "state": result["state"], "reason": result["reason"],
             "age_seconds": result["age_seconds"], "supervisor": None, "relay": None, "last_pump_action": None,
             "desktop_running": None, "office_ok": None, "projects": [], "problems": [], "incidents": [], "incidents_error": None}
    document = result["status"]
    if document is None:
        return model
    interval = _number(document.get("interval_seconds")) or supervisor_status.DEFAULT_INTERVAL
    relay = document.get("relay") if isinstance(document.get("relay"), dict) else {}
    supervisor = document.get("supervisor") if isinstance(document.get("supervisor"), dict) else {}
    model.update(
        supervisor={"at": _text(document.get("at")), "interval_seconds": interval, "pass_seconds": _number(document.get("pass_seconds")),
                    "version": _text(supervisor.get("version"), 40)},
        relay={"ok": relay.get("ok") is True, "manager_changed": relay.get("manager_changed") is True, "last_restart": _action(relay.get("last_restart"))},
        last_pump_action=_action(document.get("last_pump_action")),
        desktop_running=document.get("desktop_running") if isinstance(document.get("desktop_running"), bool) else None,
        office_ok=document.get("office_ok") if isinstance(document.get("office_ok"), bool) else None,
        projects=[_project(row, now) for row in document.get("projects", []) if isinstance(row, dict)][:50],
        problems=_texts(document.get("problems")),
        incidents_error=_text(document.get("incidents_error")),
        incidents=_incidents(folder / "relay-health.json", supervisor_status.STALE_FACTOR * interval, now))
    return model
