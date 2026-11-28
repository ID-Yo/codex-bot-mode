"""Doctor for Codex Bot Mode on this computer (#423, epic #420): one command that checks everything the agent-to-Manager
outbox needs, says what is wrong in plain words with the exact fix, and with --fix repairs the safe parts.

  python -B scripts/doctor.py                      # read-only: check this checkout and every registered project
  python -B scripts/doctor.py --project <root>     # also check one project
  python -B scripts/doctor.py --fix                # install the supervisor task, restart the relay, install a MISSING project outbox
  python -B scripts/doctor.py --project <root> --canary   # deliver one real test message to that project's Manager
  python -B scripts/doctor.py --json

Exit code: 0 when nothing failed (warnings are allowed), 1 when a check failed, 2 for a wrong command line.
The checks cost no model tokens and change nothing: they read the queue file read-only and never run a project's own
outbox.py (whose status --heal would start a pump). --fix never overwrites an installed outbox.py or outbox-config.json.
--canary wakes a Manager, so it names its projects explicitly (--canary-all-projects opts in to every checked project)."""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASK_NAME = "Codex Bot Mode Supervisor"
MAX_REPEAT_MINUTES = 5
CANARY_WAIT = 120.0
STALE_SECONDS = 600  # a pending row older than this is worth a look
OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
RECEIPT = re.compile(r"Queued outbox handoff ([0-9a-f]{32}) for ")


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def watchdog():
    return _load("cbm_doctor_watchdog", "scripts/relay_watchdog.py")


def outbox_module():
    return _load("cbm_doctor_outbox", "scripts/team_room/outbox.py")


def check(name, status, detail, fix=None, **extra):
    return {"name": name, "status": status, "detail": detail, "fix": fix, **extra}


def hidden_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run(command, timeout=60):
    r = subprocess.run(command, capture_output=True, text=True, timeout=timeout, creationflags=hidden_flags())
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def same_path(left, right):
    try:
        return os.path.normcase(os.path.abspath(str(left).strip().strip('"'))) == os.path.normcase(os.path.abspath(str(right)))
    except (OSError, ValueError):
        return False


# ---- machine checks -------------------------------------------------------------------------------------------------

MIN_PYTHON = (3, 12)  # Path.is_junction in the path-safety checks


def check_python(version=None):
    version = version or sys.version_info
    ok = tuple(version[:2]) >= MIN_PYTHON
    return check("python", OK if ok else FAIL, "Python %d.%d.%d" % tuple(version[:3]),
                 None if ok else "Install Python %d.%d or newer and run the doctor with it" % MIN_PYTHON)


def check_tools():
    results = []
    git = shutil.which("git")
    results.append(check("git", OK if git else FAIL, git or "git not found", None if git else "Install Git for Windows"))
    gh = shutil.which("gh")
    if not gh:
        results.append(check("gh", WARN, "GitHub CLI not found (needed for issues and catalog fetches)", "Install it, then run: gh auth login"))
    else:
        try:
            code, _ = run([gh, "auth", "status"], 30)
        except (OSError, subprocess.TimeoutExpired):
            code = 1
        results.append(check("gh", OK if code == 0 else WARN, "signed in" if code == 0 else "not signed in",
                             None if code == 0 else "Run: gh auth login"))
    return results


def check_codex(desktop_pipe):
    results = []
    try:
        results.append(check("codex runtime", OK, str(desktop_pipe.find_codex_binary())))
    except ValueError as error:
        results.append(check("codex runtime", FAIL, str(error), "Install Codex Desktop and sign in, or set CBM_CODEX_BINARY"))
        return results
    pipes = desktop_pipe.tool_pipes() if os.name == "nt" else []
    if os.name != "nt":
        results.append(check("codex desktop", SKIP, "only Windows is supported for the Desktop tool pipe"))
    else:
        results.append(check("codex desktop", OK if pipes else WARN,
                             "running (tool pipe found)" if pipes else "Codex Desktop is not running (no tool pipe)",
                             None if pipes else "Start Codex Desktop; the relay connects by itself when it is up"))
    return results


TASK_SCRIPT = ("$t=Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue; if(-not $t){'null'}else{"
               "[pscustomobject]@{state=[string]$t.State;battery=(-not $t.Settings.DisallowStartIfOnBatteries);hidden=[bool]$t.Settings.Hidden;"
               "multi=[string]$t.Settings.MultipleInstances;"
               "actions=@($t.Actions|ForEach-Object{[pscustomobject]@{execute=[string]$_.Execute;arguments=[string]$_.Arguments}});"
               "triggers=@($t.Triggers|ForEach-Object{[pscustomobject]@{enabled=[bool]$_.Enabled;repeat=[string]$_.Repetition.Interval}})"
               "}|ConvertTo-Json -Compress -Depth 5}")


def task_state():
    """Facts about the supervisor task, or None when it is not installed. Raises OSError or ValueError for an answer that
    is empty or not a JSON object."""
    code, out = run(["powershell", "-NoProfile", "-Command", TASK_SCRIPT % TASK_NAME], 40)
    if code != 0:
        raise OSError(out[-200:])
    lines = [line for line in out.strip().splitlines() if line.strip()]
    if not lines:
        raise OSError("the scheduler returned no answer")
    data = json.loads(lines[-1])
    if data is not None and not isinstance(data, dict):
        raise ValueError("the scheduler answer is not an object")
    return data


def repeat_minutes(text):
    best = None
    for part in (text or "").split(","):
        m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", part.strip())
        if m and (m.group(1) or m.group(2)):
            minutes = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
            best = minutes if best is None else min(best, minutes)
    return best


def _as_list(value):
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, dict) else []


def runs_watchdog(action, office):
    """True when one scheduled action is a python interpreter that runs exactly this office's relay_watchdog.py with
    --office-root naming this office. The script is the first operand that is not an option (-B only is allowed before
    it); a path that appears later, as data, or in a sibling checkout does not count."""
    if not isinstance(action, dict):
        return False
    if not re.fullmatch(r"pythonw?\.exe", Path(str(action.get("execute") or "").strip().strip('"').replace("\\", "/")).name, re.I):
        return False
    tokens = [t.strip('"') for t in re.findall(r'"[^"]*"|\S+', str(action.get("arguments") or ""))]
    index, seen_b = 0, False
    while index < len(tokens):
        if tokens[index] == "-B" and not seen_b:
            seen_b, index = True, index + 1
            continue
        break
    if index >= len(tokens) or tokens[index].startswith("-"):
        return False
    if not same_path(tokens[index], Path(office) / "scripts" / "relay_watchdog.py"):
        return False
    rest = tokens[index + 1:]
    for position, token in enumerate(rest):
        if token == "--office-root" and position + 1 < len(rest):
            return same_path(rest[position + 1], office)
        if token.startswith("--office-root="):
            return same_path(token.split("=", 1)[1], office)
    return False


def evaluate_task(state, office):
    """The supervisor check for the facts of the scheduled task (pure: no operating system calls)."""
    fix = "powershell -File scripts/supervisor_task.ps1"
    if state is None:
        return check("supervisor task", FAIL, "%s is not installed: nothing restarts the relay or the pumps" % TASK_NAME, fix)
    problems = []
    if state.get("state") == "Disabled":
        problems.append("disabled")
    if not state.get("battery"):
        problems.append("does not run on battery")
    if not state.get("hidden"):
        problems.append("not hidden (a console window may flash)")
    if state.get("multi") != "IgnoreNew":
        problems.append("may start overlapping runs")
    enabled = [t for t in _as_list(state.get("triggers")) if isinstance(t, dict) and t.get("enabled")]
    minutes = repeat_minutes(",".join(str(t.get("repeat") or "") for t in enabled))
    if minutes is None or minutes > MAX_REPEAT_MINUTES:
        problems.append("no enabled trigger repeats at most every %d min (best: %s)" % (MAX_REPEAT_MINUTES, "none" if minutes is None else "%d min" % minutes))
    if not any(runs_watchdog(a, office) for a in _as_list(state.get("actions"))):
        problems.append("does not run this checkout's scripts/relay_watchdog.py with --office-root " + str(office) + " (another checkout, or a different command)")
    if problems:
        return check("supervisor task", FAIL, "; ".join(problems), fix)
    return check("supervisor task", OK, "installed, every %d min, hidden, also on battery" % minutes)


def check_supervisor(office):
    fix = "powershell -File scripts/supervisor_task.ps1"
    if os.name != "nt":
        return check("supervisor task", SKIP, "Windows Task Scheduler only; start the relay with python -B scripts/relay_watchdog.py --restart from your own scheduler")
    try:
        state = task_state()
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return check("supervisor task", WARN, "could not read the scheduled task (%s)" % str(error)[:120], fix)
    return evaluate_task(state, office)


def check_relay(office, wd):
    relay_root = Path(office) / ".bot-mode" / "desktop-relay"
    manager = wd.current_manager(office)
    out = []
    out.append(check("manager chat", OK if manager else FAIL, ("roster Manager " + manager) if manager else "no Manager chat in the roster of " + str(office),
                     None if manager else "Launch a team in Codex (\"Start a Development team in this project\") so a Manager chat exists"))
    try:
        healthy = wd.relay_ok(relay_root)
    except Exception:
        healthy = False
    out.append(check("relay", OK if healthy else FAIL,
                     "answers a real Desktop read" if healthy else "the Desktop relay does not answer",
                     None if healthy else "python -B scripts/doctor.py --fix  (or: python -B scripts/relay_watchdog.py --restart)"))
    return out


# ---- project checks -------------------------------------------------------------------------------------------------

def read_config(project):
    """('missing', None), ('invalid', reason) or ('ok', config dict) for .bot-mode/outbox-config.json."""
    path = Path(project) / ".bot-mode" / "outbox-config.json"
    if not path.is_file():
        return "missing", None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "invalid", "not readable JSON"
    if not isinstance(data, dict):
        return "invalid", "not a JSON object"
    relay_root = data.get("relay_root")
    if relay_root is not None and not isinstance(relay_root, str):
        return "invalid", "relay_root is not text"
    return "ok", data


def queue_facts(project):
    """Counts of the queue file, read with a read-only connection; None when there is no queue yet. Raises sqlite3.Error."""
    db = Path(project) / ".bot-mode" / "outbox" / "queue.sqlite"
    if not db.is_file():
        return None
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        counts = dict(conn.execute("SELECT state, COUNT(*) FROM outbox GROUP BY state").fetchall())
        oldest = conn.execute("SELECT MIN(created_at) FROM outbox WHERE state = 'pending'").fetchone()[0]
    finally:
        conn.close()
    age = None
    if oldest:
        try:
            age = max(0.0, datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(oldest).timestamp())
        except ValueError:
            pass
    return {"counts": counts, "oldest_pending_seconds": age}


def check_queue(name, project):
    heal = "python -B %s --project %s status --heal  (starts a pump; this check never does)" % (Path(project) / ".bot-mode" / "outbox.py", project)
    try:
        facts = queue_facts(project)
    except sqlite3.Error as error:
        return check(name + ": queue", WARN, "the queue file cannot be read (%s)" % str(error)[:100])
    if facts is None:
        return check(name + ": queue", OK, "no queue yet")
    counts, problems = facts["counts"], []
    if counts.get("ambiguous"):
        problems.append("%d report(s) with an unconfirmed delivery (the pump reconciles them)" % counts["ambiguous"])
    if counts.get("needs-attention"):
        problems.append("%d report(s) need a decision: outbox.py list --state needs-attention, then resolve <id>" % counts["needs-attention"])
    if counts.get("failed"):
        problems.append("%d report(s) failed (no target thread)" % counts["failed"])
    age = facts["oldest_pending_seconds"]
    if counts.get("pending") and age is not None and age > STALE_SECONDS:
        problems.append("oldest pending report is %d s old" % age)
    if problems:
        return check(name + ": queue", WARN, "; ".join(problems), heal)
    return check(name + ": queue", OK, "pending %s, delivered %s" % (counts.get("pending", 0), counts.get("delivered", 0)))


def check_route(name, project, ob):
    """The relay descriptor this project's outbox would actually use, and whether its port answers."""
    root, data = ob.find_relay_descriptor(project)
    fix = ("point relay_root in %s at the Office checkout's .bot-mode/desktop-relay, then: python -B scripts/doctor.py --fix"
           % (Path(project) / ".bot-mode" / "outbox-config.json"))
    if not root:
        return check(name + ": relay route", FAIL,
                     "no usable relay.json (url and token, loopback only) is found for this project: its reports cannot be delivered", fix)
    if not ob.relay_reachable(project):
        return check(name + ": relay route", FAIL, "the relay named by %s does not accept connections" % (Path(root) / "relay.json"),
                     "python -B scripts/doctor.py --fix  (or: python -B scripts/relay_watchdog.py --restart)")
    return check(name + ": relay route", OK, "relay at %s answers" % root)


REGISTRY_REMEDY = ("registering a project in Office needs the owner's consent (the project onboarding in the Office panel: the agent prepares it, "
                   "the owner confirms); the doctor and --fix never write the Office registry")
RELAY_REFUSAL = "Unappointed Desktop recipient refused"  # the text a relay that names the refusal puts in its HTTP 400


def registry_roots(office):
    """(roots, problem) of the Office registry, read only. A missing registry file is an empty registry; problem is None
    unless the file exists and cannot be read."""
    path = Path(office) / ".bot-mode" / "local" / "office-project-roots.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as error:
        return [], "cannot be read (%s)" % str(error)[:80]
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        return [], "cannot be read (it has no list of projects)"
    return [p["root"] for p in projects if isinstance(p, dict) and isinstance(p.get("root"), str)], None


def check_registration(name, project, office):
    """Whether the Office registry names this project. The relay serves only registered projects. Never writes the registry."""
    label = name + ": Office registration"
    roots, problem = registry_roots(office)
    if problem:
        return check(label, WARN, "unknown: the Office registry %s, so it is not known whether the project is registered" % problem,
                     REGISTRY_REMEDY)
    if any(same_path(root, project) for root in roots):
        return check(label, OK, "the project is in the Office registry")
    return check(label, WARN, "the project is not in the Office registry: until the owner approves registering it, the relay refuses "
                              "its Manager chat and its reports wait", REGISTRY_REMEDY)


def check_manager_route(name, project, wd, ob):
    """One read-only read_thread of the Manager chat through the project's relay, as the pump does before a delivery: would the
    relay let a report in? Nothing is sent, nobody is woken and the project's own outbox.py is not run."""
    label = name + ": Manager route"
    manager = wd.current_manager(project)
    root, _ = ob.find_relay_descriptor(project)
    if not manager:
        return check(label, SKIP, "no Manager chat is bound in this project's roster (see the Manager chat check)")
    if not root:
        return check(label, SKIP, "this project has no usable relay descriptor (see the relay route check)")
    if not ob.relay_reachable(project):
        return check(label, SKIP, "the relay of this project does not accept connections (see the relay route check)")
    try:
        ob.RelayClient(root).call("read_thread", {"threadId": manager, "turnLimit": 1, "includeOutputs": False}, timeout=10)
    except Exception as error:
        answer = str(error)
        if not answer.startswith("HTTP Error 400"):
            return check(label, WARN, "unknown: the Manager chat could not be read (%s)" % answer[:120],
                         "run the doctor again, or check the relay")
        if RELAY_REFUSAL in answer:
            return check(label, FAIL, "the relay refused the Manager chat: the project is not in the Office registry or the chat is not "
                                      "bound in its roster", REGISTRY_REMEDY)
        return check(label, FAIL, "the relay answered HTTP 400 for the Manager chat: probably the project is not in the Office registry "
                                  "or the chat is not bound in its roster (this relay does not say which)", REGISTRY_REMEDY)
    return check(label, OK, "the relay lets a report to the Manager chat through")


def check_project(project, office, wd, ob=None):
    name = Path(project).name
    full = str(project)
    ob = ob or outbox_module()
    results = []
    bot_mode = Path(project) / ".bot-mode"
    installed = bot_mode / "outbox.py"
    source = REPO / "scripts" / "team_room" / "outbox.py"
    if not installed.is_file():
        results.append(check(name + ": outbox", FAIL, "outbox.py is not installed: completion reports go nowhere",
                             "python -B scripts/doctor.py --fix --project " + full, project=full, code="outbox_missing"))
        return results
    if sha(installed) != sha(source):
        results.append(check(name + ": outbox", WARN, "installed outbox.py differs from this checkout",
                             "the doctor never overwrites an installed copy: update it through your normal project update, or copy scripts/team_room/outbox.py yourself",
                             project=full, code="outbox_differs"))
    else:
        results.append(check(name + ": outbox", OK, "installed, matches this checkout"))
    state, detail = read_config(project)
    config_path = bot_mode / "outbox-config.json"
    if state == "missing":
        results.append(check(name + ": outbox config", WARN, "outbox-config.json is missing",
                             "python -B scripts/doctor.py --fix --project " + full, project=full, code="config_missing"))
    elif state == "invalid":
        results.append(check(name + ": outbox config", FAIL, "outbox-config.json is invalid (%s)" % detail,
                             "edit or delete %s by hand; the doctor does not overwrite it" % config_path, project=full, code="config_invalid"))
    else:
        results.append(check(name + ": outbox config", OK, "relay_root " + str(detail.get("relay_root"))))
    results.append(check_route(name, project, ob))
    manager = wd.current_manager(project)
    results.append(check(name + ": Manager chat", OK if manager else WARN,
                         "bound" if manager else "no Manager chat bound in this project's roster (reports to 'manager' cannot be delivered)",
                         None if manager else "roster.py set-thread manager <thread-id> --reason appointed"))
    results.append(check_registration(name, project, office))
    results.append(check_manager_route(name, project, wd, ob))
    results.append(check_queue(name, project))
    return results


def canary(project, office, wait=CANARY_WAIT):
    """Deliver one real message to the project's Manager through the outbox and wait for the delivered state.

    Unknown is not failure: when the enqueue receipt is missing, or the queue cannot be read, the result is a warning that
    says the message may have been sent."""
    name = Path(project).name + ": canary"
    cli = Path(project) / ".bot-mode" / "outbox.py"
    if not cli.is_file():
        return check(name, FAIL, "outbox.py is not installed")
    text = "[CBM doctor canary] Delivery test of the outbox. Nothing to do; reply only: ok."
    try:
        code, out = run([sys.executable, "-B", str(cli), "--project", str(project), "send", "--to", "manager", "--by", "doctor", "--text", text], 60)
    except subprocess.TimeoutExpired:
        return check(name, WARN, "unknown: the send did not answer in time; the message may or may not be queued",
                     "python -B %s --project %s list" % (cli, project))
    if code != 0:
        return check(name, FAIL, "send failed: " + out.strip()[-200:])
    match = RECEIPT.search(out)
    if not match:
        return check(name, WARN, "unknown: the send exited 0 but printed no enqueue receipt; a test message may be queued",
                     "python -B %s --project %s list" % (cli, project))
    row = match.group(1)
    started = time.monotonic()
    last = "pending"
    read_once = False
    while True:
        try:
            code, listing = run([sys.executable, "-B", str(cli), "--project", str(project), "list"], 30)
            rows = json.loads(listing) if code == 0 else None
        except (subprocess.TimeoutExpired, ValueError):
            rows = None
        if isinstance(rows, list):
            read_once = True
            found = next((r for r in rows if isinstance(r, dict) and r.get("id") == row), None)
            last = (found or {}).get("state", "missing")
            if last == "delivered":
                return check(name, OK, "delivered to the Manager in %d s (row %s)" % (time.monotonic() - started, row))
            if last in ("failed", "expired", "skipped"):
                return check(name, FAIL, "the test message ended as %s (row %s)" % (last, row),
                             "python -B %s --project %s list --state %s" % (cli, project, last))
            if last in ("ambiguous", "needs-attention"):
                return check(name, WARN, "unknown: the delivery of the test message is unconfirmed (%s, row %s)" % (last, row),
                             "python -B %s --project %s status --heal" % (cli, project))
        if time.monotonic() - started >= wait:
            break
        time.sleep(3)
    if not read_once:
        return check(name, WARN, "unknown: the queue could not be read after the send (row %s)" % row, "python -B %s --project %s list" % (cli, project))
    return check(name, FAIL, "not delivered within %d s (row %s, state %s)" % (wait, row, last),
                 "python -B %s --project %s status --heal" % (cli, project))


# ---- fixes ----------------------------------------------------------------------------------------------------------

def install_outbox(project, relay_root):
    """Create .bot-mode/outbox.py when it does not exist, and outbox-config.json when it does not exist. An existing file
    is never touched: both are created exclusively, so a copy that appeared since the check is kept."""
    bot_mode = Path(project) / ".bot-mode"
    bot_mode.mkdir(parents=True, exist_ok=True)
    made = []
    for target, data in ((bot_mode / "outbox.py", (REPO / "scripts" / "team_room" / "outbox.py").read_bytes()),
                         (bot_mode / "outbox-config.json", (json.dumps({"relay_root": Path(relay_root).as_posix()}) + "\n").encode("utf-8"))):
        try:
            with open(target, "xb") as handle:
                handle.write(data)
            made.append(target.name)
        except FileExistsError:
            pass
    return made


def create_config(project, relay_root):
    bot_mode = Path(project) / ".bot-mode"
    try:
        with open(bot_mode / "outbox-config.json", "xb") as handle:
            handle.write((json.dumps({"relay_root": Path(relay_root).as_posix()}) + "\n").encode("utf-8"))
        return True
    except FileExistsError:
        return False


def apply_fixes(results, office, projects, wd):
    done = []
    names = {r["name"]: r for r in results if r["status"] in (FAIL, WARN)}
    if "supervisor task" in names and os.name == "nt":
        code, out = run(["powershell", "-NoProfile", "-File", str(REPO / "scripts" / "supervisor_task.ps1")], 120)
        done.append("supervisor task: " + ("installed" if code == 0 else "failed: " + out.strip()[-160:]))
    if "relay" in names:
        ok, detail = wd.restart_relay(Path(office) / ".bot-mode" / "desktop-relay")
        done.append("relay: " + ("restarted" if ok else "not restarted: " + detail))
    relay_root = Path(office) / ".bot-mode" / "desktop-relay"
    checked = {str(p) for p in projects}
    for r in results:
        project = r.get("project")
        if r["status"] not in (FAIL, WARN) or project not in checked:
            continue
        label = Path(project).name
        try:
            if r.get("code") == "outbox_missing":
                made = install_outbox(project, relay_root)
                done.append(label + ": " + ("installed " + ", ".join(made) if made else "outbox.py appeared meanwhile, left as it is"))
            elif r.get("code") == "config_missing":
                done.append(label + ": " + ("outbox-config.json created" if create_config(project, relay_root) else "outbox-config.json appeared meanwhile, left as it is"))
        except OSError as error:
            done.append(label + ": install failed: " + str(error)[:120])
    return done


# ---- main -----------------------------------------------------------------------------------------------------------

def collect(office, projects, wd, desktop_pipe):
    results = [check_python()] + check_tools() + check_codex(desktop_pipe) + [check_supervisor(office)] + check_relay(office, wd)
    ob = outbox_module() if projects else None
    for project in projects:
        results += check_project(project, office, wd, ob)
    return results


def render(results):
    marks = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]", SKIP: "[skip]"}
    lines = []
    for r in results:
        lines.append("%s %s: %s" % (marks[r["status"]], r["name"], r["detail"]))
        if r["fix"] and r["status"] in (WARN, FAIL):
            lines.append("       fix: " + r["fix"])
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check Codex Bot Mode (relay, supervisor, outbox) on this computer.")
    ap.add_argument("--office-root", default=str(REPO), help="Checkout that owns the relay (default: this one)")
    ap.add_argument("--project", action="append", default=[], help="Project root to check (repeatable)")
    ap.add_argument("--all-projects", action="store_true", help="Also check every registered project (default when no --project is given)")
    ap.add_argument("--fix", action="store_true", help="Repair what is safe: supervisor task, relay, a missing project outbox or config")
    ap.add_argument("--canary", action="store_true", help="Deliver one real test message to the Manager of each --project (wakes that Manager)")
    ap.add_argument("--canary-all-projects", action="store_true", help="Opt in: send a canary to every checked project (wakes every Manager)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.canary and not a.project and not a.canary_all_projects:
        print("--canary needs at least one --project <root> (each canary wakes that project's Manager); "
              "--canary-all-projects sends one to every checked project", file=sys.stderr)
        return 2
    office = Path(a.office_root).resolve()
    wd = watchdog()
    wd.OFFICE = office
    desktop_pipe, _ = wd.team_room()
    projects = list(dict.fromkeys(a.project))
    if a.all_projects or not projects:
        projects = list(dict.fromkeys(projects + wd.registered_projects(office) + wd.projects_missing_outbox(office)))
    results = collect(office, projects, wd, desktop_pipe)
    fixed = []
    if a.fix and any(r["status"] in (FAIL, WARN) for r in results):
        fixed = apply_fixes(results, office, projects, wd)
        results = collect(office, projects, wd, desktop_pipe)
    if a.canary or a.canary_all_projects:
        for project in (projects if a.canary_all_projects else list(dict.fromkeys(a.project))):
            results.append(canary(project, office))
    failed = [r for r in results if r["status"] == FAIL]
    if a.json:
        print(json.dumps({"ok": not failed, "results": results, "fixed": fixed}, ensure_ascii=False, indent=1))
    else:
        print(render(results))
        for line in fixed:
            print("fixed: " + line)
        print("\nOK: everything needed works." if not failed else "\n%d check(s) failed. --fix repairs only what is safe to repair (the supervisor task, the relay, a missing project "
              "outbox or config); every other fix line is a step to do by hand." % len(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
