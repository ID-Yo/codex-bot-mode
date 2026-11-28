"""Setup status of a project (#454): which steps of connecting a team to the Office and its delivery path are finished.

Everything is read only except one project-local file, .bot-mode/local/setup-status.json. Nothing here writes the Office
registry: registering a project stays the owner's explicit step (the project onboarding in the Office panel) and is shown as
waiting for the owner's consent until it exists. 'Delivery works' is said only when every step is finished and a canary was
processed or a real report was delivered; a proof from the past with an unfinished step reads 'proven earlier', and without a
proof the status says 'delivery not proven'."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from . import outbox, registry, roster

SCHEMA = 1
FILE = "setup-status.json"
DONE = "done"
WAITING_FOR_OWNER = "waiting for owner consent"
NOT_APPLICABLE = "not applicable"
UNKNOWN = "unknown"
PROOF_NONE, PROOF_CANARY, PROOF_REPORT = "none", "canary delivered", "real report delivered"
OFFICE = Path(__file__).resolve().parents[2]
REGISTRATION_REMEDY = ("registering the project in Office needs the owner's consent (the project onboarding in the Office panel); "
                       "until then the relay refuses its Manager chat and reports to it wait")


def _step(state, detail):
    return {"state": state, "detail": detail}


def _same(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def roster_step(project):
    try:
        manager = next((p for p in roster.active(roster.load(project)) if p.get("role") == "Manager"), None)
    except Exception as error:
        return _step(UNKNOWN, "the roster cannot be read (%s)" % str(error)[:80])
    if manager and manager.get("thread_id"):
        return _step(DONE, "a Manager chat is bound")
    return _step("incomplete", "no Manager chat is bound in the roster (roster.py set-thread <manager-bot> <thread-id>)")


def outbox_step(project):
    if (Path(project) / ".bot-mode" / "outbox.py").is_file():
        return _step(DONE, "outbox.py is installed")
    return _step("incomplete", "outbox.py is not installed: completion reports go nowhere")


def relay_step(project):
    relay_root, _ = outbox.find_relay_descriptor(project)
    if not relay_root:
        return _step("incomplete", "no usable relay.json (url and token, loopback only) is found for this project")
    if not outbox.relay_reachable(project):
        return _step("incomplete", "the relay named by %s does not accept connections" % (Path(relay_root) / "relay.json"))
    return _step(DONE, "the relay answers (only its port is checked, not what it allows)")


def registration_step(project, office):
    if _same(project, office):
        return _step(NOT_APPLICABLE, "this is the Office's own project")
    loaded = registry.load_registry(registry.registry_path(office))
    if loaded["status"] == "invalid_config":
        return _step(UNKNOWN, "the Office registry cannot be read (%s), so it is not known whether the project is registered" % loaded["error"][:80])
    if any(_same(entry["root"], project) for entry in loaded["projects"]):
        return _step(DONE, "the project is in the Office registry")
    return _step(WAITING_FOR_OWNER, REGISTRATION_REMEDY)


def _count(connection, query):
    """The number a count query returns, or None when it cannot be read (for example a missing table)."""
    try:
        return connection.execute(query).fetchone()[0]
    except sqlite3.Error:
        return None


def delivery_proof_step(project):
    """none, canary delivered or real report delivered, read from the queue without opening it for writing."""
    database = Path(project) / ".bot-mode" / "outbox" / "queue.sqlite"
    if not database.is_file():
        return _step(PROOF_NONE, "no outbox queue yet")
    try:
        # Path.as_uri encodes # and %, and mode=ro never creates a file
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as error:
        return _step(UNKNOWN, "the queue cannot be read (%s)" % str(error)[:80])
    try:
        delivered = _count(connection, "SELECT COUNT(*) FROM outbox WHERE state = 'delivered'")
        processed = _count(connection, "SELECT COUNT(*) FROM canary WHERE state = 'done'")  # a queue made before the canary has no such table
    finally:
        connection.close()
    if delivered:
        return _step(PROOF_REPORT, "a report was delivered")
    if processed:
        return _step(PROOF_CANARY, "the canary was processed (nothing is sent: it proves the path to the chat, not a delivered message)")
    if delivered is None:
        return _step(UNKNOWN, "the queue cannot be read")
    return _step(PROOF_NONE, "no report was delivered and no canary was processed yet")


def collect(project, office=OFFICE):
    """The setup status of the project as a dict. complete is True only when every step is done or not applicable and delivery is proven."""
    steps = {"roster": roster_step(project), "outbox": outbox_step(project), "relay_route": relay_step(project),
             "office_registration": registration_step(project, office)}
    proof = delivery_proof_step(project)
    unfinished = [name for name, step in steps.items() if step["state"] not in (DONE, NOT_APPLICABLE)]
    proven = proof["state"] in (PROOF_CANARY, PROOF_REPORT)
    complete = not unfinished and proven
    delivery = "works" if complete else "proven earlier" if proven else "not proven"
    return {"schema": SCHEMA, "checked_at": datetime.now(timezone.utc).isoformat(), "project": str(project), "steps": steps,
            "delivery_proof": proof, "delivery": delivery, "incomplete": unfinished, "complete": complete}


def write(project, status):
    """Store the status in the project's .bot-mode/local folder (atomic). Returns the path."""
    roster.ensure_gitignore(project)  # the local folder stays out of Git
    folder = Path(project) / ".bot-mode" / "local"
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / (FILE + ".tmp")
    try:
        temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, folder / FILE)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return folder / FILE


def render(status):
    labels = {"roster": "Roster", "outbox": "Outbox", "relay_route": "Relay route", "office_registration": "Office registration"}
    lines = ["Setup status of " + status["project"]]
    for name, step in status["steps"].items():
        lines.append("  %-20s %s - %s" % (labels[name], step["state"], step["detail"]))
    lines.append("  %-20s %s - %s" % ("Delivery proof", status["delivery_proof"]["state"], status["delivery_proof"]["detail"]))
    if status["delivery"] == "works":
        lines.append("Delivery works.")
    elif status["delivery"] == "proven earlier":
        lines.append("Delivery was proven earlier (" + status["delivery_proof"]["state"] + "), but a step is unfinished now.")
    else:
        lines.append("Delivery not proven: no canary was processed and no report was delivered yet.")
    if status["incomplete"]:
        lines.append("Not finished: " + ", ".join(labels[name] for name in status["incomplete"]) + ". Report each of them as incomplete.")
    return lines
