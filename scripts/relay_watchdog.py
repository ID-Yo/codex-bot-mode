
import json,os,sqlite3,subprocess,sys,hashlib,time,urllib.request
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
OFFICE = REPO  # the checkout that owns the relay directory; the relay program refuses a root outside its working directory
SYNCED = ("outbox.py", "requests.py")
HUNG_PUMP_SECONDS = 180  # a pump writes its heartbeat every 10 s; this long without one while it holds the lock means it is stuck
RELAY_START_WAIT = 75


def team_room():
    """The relay helper modules of this checkout (pipe discovery, roster)."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from scripts.team_room import desktop_pipe, roster
    return desktop_pipe, roster


def status_module():
    """scripts.team_room.supervisor_status of this checkout (the status file schema, #424)."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from scripts.team_room import supervisor_status
    return supervisor_status


def incidents_module():
    """scripts.team_room.incidents of this checkout: stable problem keys, one alert per incident (#424)."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from scripts.team_room import incidents
    return incidents


def desktop_running():
    """True when Codex Desktop exposes its tool pipe. Unknown platforms count as running so nothing is silenced."""
    if os.name != "nt":
        return True
    try:
        return bool(team_room()[0].tool_pipes())
    except Exception:
        return True


def current_manager(office):
    """Thread id of the roster's lead Manager, or None when there is none."""
    try:
        roster = team_room()[1]
        lead = next((p for p in roster.active(roster.load(Path(office))) if p.get("role") == "Manager"), None)
        return (lead or {}).get("thread_id")
    except Exception:
        return None


def relay_manager_changed(relay_root, office):
    """True when the running relay serves another Manager chat than the roster names now (the chat was replaced)."""
    try:
        origin = json.loads((Path(relay_root) / "relay.json").read_text(encoding="utf-8")).get("origin")
    except (OSError, ValueError):
        return False
    manager = current_manager(office)
    return bool(origin and manager and origin != manager)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]


def relay_ok(relay_root):
    try:
        return _relay_ok(relay_root)
    except (OSError, subprocess.TimeoutExpired):
        return False


def _relay_ok(relay_root):
    r = subprocess.run([sys.executable, "-B", "-m", "scripts.team_room.desktop_relay", "--root", str(relay_root), "--probe"],
                       cwd=str(OFFICE), capture_output=True, text=True, timeout=60, creationflags=hidden_flags())
    return r.returncode == 0


def office_ok(url, timeout=8):
    """True when Codex Office answers its state endpoint with participants."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/state", timeout=timeout) as resp:
            return "participants" in json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False


def restart_office():
    """Office does not need the Manager chat: start it hidden (-NoBrowser). Returns (ok, detail)."""
    return run_starter(["powershell", "-NoProfile", "-File", str(OFFICE / "scripts" / "start_codex_office.ps1"), "-NoBrowser",
                        "-Project", str(OFFICE)])


def run_starter(command, timeout=120):
    """Run a launcher that leaves a long-lived child (relay, Office) behind. Its output goes to a file:
    with pipes the child inherits them and subprocess.run never returns, not even after its timeout,
    which kept the watchdog lock held and silenced every later pass. Returns (ok, last 300 chars)."""
    import tempfile
    with tempfile.TemporaryFile() as out:
        try:
            code = subprocess.run(command, cwd=str(OFFICE), stdout=out, stderr=subprocess.STDOUT, timeout=timeout,
                                  creationflags=hidden_flags()).returncode
        except subprocess.TimeoutExpired:
            code = None
        out.seek(0)
        tail = out.read().decode("utf-8", "replace").strip()[-300:]
    return code == 0, tail if code is not None else "launcher timed out; " + tail


def hidden_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def pid_alive(pid):
    if not isinstance(pid, int):
        return None
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                           timeout=20, creationflags=hidden_flags())
        return f'"{pid}"' in r.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def tail(path, lines=8):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def record_incident(relay_root, now=None):
    """Append what is known about a dead relay to relay-incidents.jsonl, once per dead relay process (#401)."""
    relay_root = Path(relay_root)
    try:
        descriptor = json.loads((relay_root / "relay.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        descriptor = {}
    pid = descriptor.get("pid")
    log = relay_root.parent / "relay-incidents.jsonl"
    try:
        if pid is not None and any(json.loads(l).get("relay_pid") == pid for l in log.read_text(encoding="utf-8").splitlines() if l.strip()):
            return None
    except (OSError, ValueError):
        pass
    try:
        alive = json.loads((relay_root / "relay-alive.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        alive = None
    processes = []
    if os.name == "nt":
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(Codex|codex|node|pwsh|python)\\.exe$' -and $_.CommandLine -match 'app-server|codex|desktop_relay' } "
                            "| ForEach-Object { '{0}|{1}|{2}|{3}' -f $_.ProcessId,$_.ParentProcessId,$_.Name,$_.CreationDate.ToString('s') }"],
                           capture_output=True, text=True, timeout=40, creationflags=hidden_flags())
        processes = [l.strip() for l in r.stdout.splitlines() if l.strip()][:30]
    entry = {"at": (now or datetime.now(timezone.utc)).isoformat(), "relay_pid": pid, "relay_pid_alive": pid_alive(pid),
             "bridge_pid": descriptor.get("bridge_pid"), "bridge_pid_alive": pid_alive(descriptor.get("bridge_pid")),
             "last_proof_of_life": alive, "parent_alive_at_last_proof": (alive or {}).get("parent_alive"),
             "stdout_tail": tail(relay_root / "stdout.log"), "stderr_tail": tail(relay_root / "stderr.log"),
             "codex_processes": processes}
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def rotate_relay_logs(relay_root, keep=5):
    """Keep the previous relay logs: a restart must not erase the evidence of why the last relay ended (#388)."""
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    for name in ("stdout", "stderr"):
        current = Path(relay_root) / f"{name}.log"
        try:
            if current.is_file() and current.stat().st_size > 0:
                current.replace(Path(relay_root) / f"{name}.{stamp}.log")
            old = sorted(Path(relay_root).glob(f"{name}.*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in old[keep:]:
                path.unlink()
        except OSError:
            pass


def relay_python():
    """python.exe next to a pythonw.exe interpreter: the relay needs real standard streams, and CREATE_NO_WINDOW keeps it hidden."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.with_name("python.exe")).is_file():
        return str(exe.with_name("python.exe"))
    return str(exe)


def relay_pid(relay_root):
    try:
        return json.loads((Path(relay_root) / "relay.json").read_text(encoding="utf-8")).get("pid")
    except (OSError, ValueError):
        return None


def end_process_tree(process):
    """End a process this pass started, with the bridge it spawned. Returns True only when it is confirmed gone."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, timeout=20, creationflags=hidden_flags())
        else:
            process.kill()
        process.wait(5)
    except (OSError, subprocess.SubprocessError):
        pass
    return process.poll() is not None


def start_blocked(relay_root):
    """Message when an earlier start could not be ended and still runs (start-unconfirmed.json); clears the record once it is gone."""
    blocker = Path(relay_root) / "start-unconfirmed.json"
    try:
        earlier = json.loads(blocker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if pid_alive(earlier.get("pid")):
        return f"an earlier start (pid {earlier.get('pid')}) could not be ended and is still running; no further start is made"
    try:
        blocker.unlink()
    except OSError:
        pass
    return None


def start_standalone_relay(relay_root, wait=RELAY_START_WAIT, probe=None):
    """Start the relay without the Manager chat (#421): Manager from the roster, Desktop tool pipe found by name.
    It runs detached from this process, so it outlives the scheduled task. This pass owns the child it starts: it is kept only
    when it is the relay that answered, ended otherwise, and when ending it cannot be confirmed no further start is made
    until it is gone. Returns (ok, detail)."""
    probe = probe or relay_ok
    relay_root = Path(relay_root)
    relay_root.mkdir(parents=True, exist_ok=True)
    blocker = relay_root / "start-unconfirmed.json"
    message = start_blocked(relay_root)
    if message:
        return False, message
    rotate_relay_logs(relay_root)
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    command = [relay_python(), "-B", "-m", "scripts.team_room.desktop_relay", "--standalone", "--root", str(relay_root)]
    with open(relay_root / "stdout.log", "wb") as out, open(relay_root / "stderr.log", "wb") as err:
        process = subprocess.Popen(command, cwd=str(OFFICE), stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=flags,
                                   close_fds=True)

    def dispose(reason):
        """The child is not the relay that serves: end it and say if that could not be confirmed."""
        if process.poll() is not None or end_process_tree(process):
            return reason
        reason = reason.replace(" and was ended", "").replace(" and the start was ended", "")
        try:
            blocker.write_text(json.dumps({"pid": process.pid, "at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        except OSError:
            pass
        return reason + f"; the start (pid {process.pid}) could not be ended, no further start is made until it is gone"

    stderr_tail = lambda: " | ".join(tail(relay_root / "stderr.log", 3))[-300:]
    deadline = time.monotonic() + wait
    while True:
        if probe(relay_root):
            pid = relay_pid(relay_root)
            if pid in (None, process.pid):
                return True, f"standalone relay started (pid {process.pid})"
            return True, dispose(f"another relay (pid {pid}) answered; the start (pid {process.pid}) was not needed and was ended")
        code = process.poll()
        if code is not None:  # also code 0: a relay that ended before it answered did not establish readiness
            return False, f"relay exited with code {code} before it answered: " + stderr_tail()
        if time.monotonic() >= deadline:
            break
        time.sleep(1.5)
    return False, dispose("relay did not answer within %d s and the start was ended" % wait) + ": " + stderr_tail()


def stop_relay(relay_root, wait=20):
    """Ask a running relay to stop through its shutdown flag; returns True when it is gone."""
    root = Path(relay_root)
    try:
        pid = json.loads((root / "relay.json").read_text(encoding="utf-8")).get("pid")
    except (OSError, ValueError):
        return True
    if not pid_alive(pid):
        return True
    (root / "shutdown.flag").write_text("shutdown", encoding="utf-8")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.5)
    return not pid_alive(pid)


def restart_relay(relay_root=None):
    """Restart the relay without the Manager chat; the Manager-chat registration stays as a fallback."""
    relay_root = relay_root or str(OFFICE / ".bot-mode" / "desktop-relay")
    if not desktop_running():
        return False, "Codex Desktop is not running (no tool pipe); the relay starts when Desktop does"
    blocker = start_blocked(relay_root)
    if blocker:
        return False, blocker  # an earlier start still runs: neither another standalone start nor the Manager fallback
    ok, detail = start_standalone_relay(relay_root)
    if ok or not (os.environ.get("CODEX_THREAD_ID") and os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")):
        return ok, detail
    blocker = start_blocked(relay_root)  # the standalone attempt may just have left an unended child
    if blocker:
        return False, detail + "; " + blocker
    return run_starter(["powershell", "-NoProfile", "-File", str(OFFICE / "scripts" / "start_codex_office.ps1"), "-RelayOnly"])


def pump_hung(project, status, now=None):
    """{pid, at} of the pump that wrote the last heartbeat when it holds its lock but has been silent for HUNG_PUMP_SECONDS
    while work waits, else None. The heartbeat is the only record of who the pump is; stop_pump proves it before acting."""
    if not status.get("pending") or not status.get("pump_alive"):
        return None
    try:
        beat = json.loads((Path(project) / ".bot-mode" / "outbox" / "pump.heartbeat.json").read_text(encoding="utf-8"))
        at = datetime.fromisoformat(beat["at"])
        pid = beat.get("pid")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = ((now or datetime.now(timezone.utc)) - at).total_seconds()
    return {"pid": pid, "at": beat["at"]} if isinstance(pid, int) and age > HUNG_PUMP_SECONDS else None


INSPECT_PROCESS_SCRIPT = r"""
$p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $env:CBM_PID)
if (-not $p) { 'gone'; exit }
@{ cmd = [string]$p.CommandLine; created = $p.CreationDate.ToUniversalTime().ToString('o') } | ConvertTo-Json -Compress
"""

END_PROCESS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $env:CBM_PID)
if (-not $p) { 'gone'; exit }
if ([string]$p.CommandLine -ne $env:CBM_CMD -or $p.CreationDate.ToUniversalTime().ToString('o') -ne $env:CBM_CREATED) { 'changed'; exit }
try { Stop-Process -Id $p.ProcessId -Force; 'stopped' } catch { 'error' }
"""


def windows_argv(command_line):
    """Split a Windows command line the way the C runtime does."""
    import ctypes
    from ctypes import wintypes
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int(0)
    items = parse(command_line, ctypes.byref(count))
    try:
        return [items[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(items)


def _same_path(left, right):
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def is_project_pump(argv, project):
    """True only for the one shape outbox.py launches (spawn_background_pump):
    <python> [-B] <project>/.bot-mode/outbox.py --project <project> pump --max-wait <seconds>.
    Any other interpreter option, a relative script or project path, or other arguments are refused: identity must be proven."""
    if not argv or Path(argv[0]).name.lower() not in ("python.exe", "pythonw.exe", "python", "python3"):
        return False
    rest = argv[1:]
    if rest[:1] == ["-B"]:
        rest = rest[1:]
    if len(rest) != 6 or rest[1] != "--project" or rest[3] != "pump" or rest[4] != "--max-wait" or not rest[5].isdigit():
        return False
    if not (os.path.isabs(rest[0]) and os.path.isabs(rest[2])):
        return False
    return _same_path(rest[0], Path(project) / ".bot-mode" / "outbox.py") and _same_path(rest[2], project)


def _powershell(script, **variables):
    environment = dict(os.environ, **{k: str(v) for k, v in variables.items()})
    r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=40,
                       env=environment, creationflags=hidden_flags())
    return ((r.stdout or "").strip().splitlines() or [""])[-1], (r.stderr or "")[-120:]


def stop_pump(pid, project, since):
    """End a stuck pump only when it is provably this project's pump: the exact command line (interpreter, this project's
    outbox.py, --project <this project>, pump) must match and the process must have existed when it wrote its last
    heartbeat (a recycled PID is newer). The stop re-checks that command line and start time, and success is reported only
    after the process is confirmed gone. Returns (stopped, reason); when identity cannot be proven the process is left alone."""
    if os.name != "nt":
        return False, "process identity cannot be verified on this platform"
    try:
        answer, error = _powershell(INSPECT_PROCESS_SCRIPT, CBM_PID=int(pid))
        if answer == "gone":
            return False, "process already gone"
        facts = json.loads(answer)
        created = datetime.fromisoformat(facts["created"])
        if created > datetime.fromisoformat(str(since)):
            return False, "PID now belongs to a newer process"
        if not is_project_pump(windows_argv(facts["cmd"]), project):
            return False, "process is not this project's outbox pump"
        answer, error = _powershell(END_PROCESS_SCRIPT, CBM_PID=int(pid), CBM_CMD=facts["cmd"], CBM_CREATED=facts["created"])
        if answer != "stopped":
            return False, {"gone": "process already gone", "changed": "process changed before it could be stopped",
                           "error": "stop failed"}.get(answer, "stop gave no answer: " + error)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            check, _ = _powershell(INSPECT_PROCESS_SCRIPT, CBM_PID=int(pid))
            if check == "gone" or json.loads(check)["created"] != facts["created"]:
                return True, "stopped"
            time.sleep(0.5)
        return False, "stop was requested but the process is still running"
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        return False, "inspection failed (%s)" % type(error).__name__


def manager_busy(project):
    import importlib.util
    spec = importlib.util.spec_from_file_location("watchdog_outbox", Path(project) / ".bot-mode" / "outbox.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root, _ = module.find_relay_descriptor(project)
    thread = module.resolve_target_thread(project, "manager")
    if not root or not thread:
        return False
    history = module.RelayClient(root).call("read_thread", {"threadId": thread, "turnLimit": 1, "includeOutputs": False})
    # Unknown is not evidence of a busy Manager.
    return (history.get("thread", {}).get("status", {}).get("type") == "active"
            or any(t.get("status") == "inProgress" for t in history.get("turns", [])))


def classify_wait(project, status):
    if not status.get("pending") or not status.get("pump_alive"):
        return
    if not any("oldest pending row" in p for p in status.get("problems", [])):
        return
    try:
        if manager_busy(project):
            status["waiting_reason"] = "manager busy"
            status["problems"] = [p for p in status["problems"] if "oldest pending row" not in p]
    except Exception:
        pass  # keep the warning when Desktop activity cannot be verified


def outbox_status(project):
    r = subprocess.run([sys.executable, "-B", str(Path(project) / ".bot-mode" / "outbox.py"), "--project", str(project),
                        "status", "--heal"], capture_output=True, text=True, timeout=60, creationflags=hidden_flags())
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"problems": ["outbox status failed: " + (r.stderr or r.stdout)[-200:]]}


def project_report(project, heal_hung=False):
    out = {"project": str(project)}
    out["outbox"] = outbox_status(project)
    hung = pump_hung(project, out["outbox"])
    if hung and heal_hung:
        stopped, reason = stop_pump(hung["pid"], project, hung["at"])
        out["hung_pump"] = {"pid": hung["pid"], "stopped": stopped, "reason": reason}
        if stopped:
            out["outbox"] = outbox_status(project)  # --heal starts a fresh pump now that the lock is free
        else:
            out["outbox"].setdefault("problems", []).append(
                f"pump {hung['pid']} holds the lock but wrote no heartbeat for {HUNG_PUMP_SECONDS} s; not stopped ({reason})")
    elif hung:
        out["outbox"].setdefault("problems", []).append(f"pump {hung['pid']} holds the lock but wrote no heartbeat for {HUNG_PUMP_SECONDS} s")
    classify_wait(project, out["outbox"])
    drift = []
    for name in SYNCED:
        installed = Path(project) / ".bot-mode" / name
        source = REPO / "scripts" / "team_room" / name
        if installed.is_file() and source.is_file() and sha(installed) != sha(source):
            drift.append(name)
    out["installed_differs_from_repo"] = drift
    return out


def registered_projects(office):
    """Roots of the projects in the Office registry that have an installed outbox."""
    try:
        data = json.loads((Path(office) / ".bot-mode" / "local" / "office-project-roots.json").read_text(encoding="utf-8"))
        roots = [Path(p["root"]) for p in data.get("projects", []) if p.get("root")]
    except (OSError, ValueError, AttributeError, TypeError):
        return []
    return [str(root) for root in roots if (root / ".bot-mode" / "outbox.py").is_file()]


def projects_missing_outbox(office):
    """Roots of registered projects that have the request helper but no outbox: their closed requests report nowhere (#415)."""
    try:
        data = json.loads((Path(office) / ".bot-mode" / "local" / "office-project-roots.json").read_text(encoding="utf-8"))
        roots = [Path(p["root"]) for p in data.get("projects", []) if p.get("root")]
    except (OSError, ValueError, AttributeError, TypeError):
        return []
    return [str(r) for r in roots if (r / ".bot-mode" / "requests.py").is_file() and not (r / ".bot-mode" / "outbox.py").is_file()]


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Zero-token health check of the Desktop relay and the project outboxes (#401).")
    ap.add_argument("--office-root", default=str(REPO), help="Checkout that owns the relay (default: this one)")
    ap.add_argument("--relay-root", default=None)
    ap.add_argument("--project", action="append", default=[], help="Project root with an outbox (repeatable)")
    ap.add_argument("--all-projects", action="store_true", help="Also check every registered project that has an outbox")
    ap.add_argument("--restart", action="store_true", help="Restart the relay when it is down (works only in the Manager chat)")
    ap.add_argument("--health-file", default=None)
    ap.add_argument("--status-file", default=None, help="Supervisor status file (default: supervisor-status.json next to the health file)")
    ap.add_argument("--interval-seconds", type=int, default=120, help="How often the scheduled task runs; a status file older than three of these is Unknown")
    ap.add_argument("--incident-file", default=None, help="Open incidents (default: local/incidents.json next to the health file)")
    ap.add_argument("--office-url", default="http://localhost:24242")
    ap.add_argument("--restart-office", action="store_true", help="Start Codex Office when it does not answer")
    ap.add_argument("--notify", action="store_true",
                    help="Show ONE Windows message per incident that needs the owner (the relay is down and could not be restarted), after 10 minutes")
    a = ap.parse_args(argv)
    global OFFICE
    OFFICE = Path(a.office_root).resolve()
    a.relay_root = a.relay_root or str(OFFICE / ".bot-mode" / "desktop-relay")
    a.health_file = a.health_file or str(OFFICE / ".bot-mode" / "relay-health.json")
    a.status_file = a.status_file or str(Path(a.health_file).with_name("supervisor-status.json"))
    started = time.monotonic()
    a.incident_file = a.incident_file or str(Path(a.health_file).parent / "local" / "incidents.json")
    if a.all_projects:
        a.project = list(dict.fromkeys(a.project + registered_projects(OFFICE)))

    report = {"at": datetime.now(timezone.utc).isoformat(), "relay_ok": relay_ok(a.relay_root), "problems": [],
              "desktop_running": desktop_running()}
    turnover_blocked = False
    if report["relay_ok"] and relay_manager_changed(a.relay_root, OFFICE):
        # The Manager chat was replaced: the relay still serves the old thread id. Restart it for the new one.
        if a.restart:
            stopped = stop_relay(a.relay_root)
            report["manager_changed"] = {"relay_stopped": stopped}
            if stopped:
                report["relay_ok"] = relay_ok(a.relay_root)
            else:
                turnover_blocked = True  # the old relay is still there: do not start a competing one
        else:
            report["manager_changed"] = {"relay_stopped": None}
    if not report["relay_ok"] and report["desktop_running"]:
        try:
            record_incident(a.relay_root)
        except Exception:
            pass
    if not report["relay_ok"] and a.restart:
        try:
            ok, detail = restart_relay(a.relay_root)
        except (OSError, subprocess.TimeoutExpired) as error:
            ok, detail = False, type(error).__name__
        report["restart"] = {"ok": ok, "detail": detail}
        report["relay_ok"] = ok and relay_ok(a.relay_root)
    if not report["relay_ok"] and report["desktop_running"]:
        report["problems"].append("Desktop relay down and could not be restarted (" + str((report.get("restart") or {}).get("detail", "restart not requested")) +
                                  "): run python -B scripts/relay_watchdog.py --restart, or in the Manager chat powershell -File scripts/start_codex_office.ps1 -RelayOnly")
    if report["relay_ok"] and relay_manager_changed(a.relay_root, OFFICE):
        # Healthy is not enough: it must serve the Manager the roster names now.
        report["problems"].append("Desktop relay serves a replaced Manager chat" + (
            " and did not stop in time; no second relay is started" if turnover_blocked else
            " even after a restart" if a.restart else "; run python -B scripts/relay_watchdog.py --restart"))
    report["office_ok"] = office_ok(a.office_url)
    if not report["office_ok"] and a.restart_office:
        try:
            ok, detail = restart_office()
        except (OSError, subprocess.TimeoutExpired) as error:
            ok, detail = False, type(error).__name__
        report["office_restart"] = {"ok": ok, "detail": detail}
        report["office_ok"] = ok and office_ok(a.office_url)
    if not report["office_ok"] and a.restart_office:
        # Office is optional: its state stays in the report, but only an install that expects it (--restart-office) raises an alarm.
        report["problems"].append("Codex Office does not answer on " + a.office_url)
    for root in projects_missing_outbox(OFFICE):
        report["problems"].append(f"{Path(root).name}: .bot-mode/requests.py is installed but .bot-mode/outbox.py is missing, "
                                  "so completion reports are not delivered; install outbox.py and outbox-config.json (#415)")
    report["projects"] = []
    for project in a.project:
        try:
            report["projects"].append(project_report(project, heal_hung=a.restart))
        except Exception as error:
            report["projects"].append({"project": str(project), "outbox": {"problems": ["health check failed: " + type(error).__name__]}})
    for p in report["projects"]:
        report["problems"] += [f"{Path(p['project']).name}: {x}" for x in p["outbox"].get("problems", []) if "relay is not reachable" not in x]
        if p.get("installed_differs_from_repo"):
            report["problems"].append(f"{Path(p['project']).name}: installed {', '.join(p['installed_differs_from_repo'])} differ from the repo")
    if not report["relay_ok"] and not report["desktop_running"]:
        # Codex Desktop is closed: nothing can be delivered, and that is expected. It matters only when rows are waiting.
        if any(p["outbox"].get("pending") for p in report["projects"]):
            report["problems"].append("Codex Desktop is not running and reports are waiting; they are delivered when Desktop starts")
    health = Path(a.health_file)
    try:
        report["incidents"] = run_incidents(a, report)
    except Exception as error:  # incident bookkeeping must never stop the pass: the health and status files still have to be written
        report["incidents"] = []
        report["incidents_error"] = f"{type(error).__name__}: {error}"[:300]
    health.parent.mkdir(parents=True, exist_ok=True)
    temporary = health.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(health)
    write_status(a, report, started)
    print(json.dumps({"ok": not report["problems"], "relay_ok": report["relay_ok"], "office_ok": report["office_ok"], "problems": report["problems"]}, ensure_ascii=False))
    return 1 if report["problems"] else 0


def write_status(a, report, started):
    """Write the supervisor status file for this pass; a failure here never fails the pass (readers treat a stale file as Unknown)."""
    try:
        module = status_module()
        reason = "Manager chat replaced" if report.get("manager_changed") else "relay down"
        data = module.build(report, module.load(a.status_file), at=datetime.now(timezone.utc), pass_seconds=time.monotonic() - started,
                            interval_seconds=a.interval_seconds, pid=os.getpid(), version=sha(__file__), restart_reason=reason)
        module.write(a.status_file, data)
    except OSError:
        pass


def show_owner_message(incident):
    """One Windows message for an incident that needs the owner. A machine without msg.exe simply shows none."""
    text = f"Codex relay/outbox needs you: {incident['text']} {incident['remedy']}"[:400]
    if os.name != "nt":
        return False
    try:
        subprocess.run(["msg", "*", "/TIME:600", text], capture_output=True, timeout=10, creationflags=hidden_flags())
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def run_incidents(a, report, now=None):
    """Fold this pass's problems into the open incidents and carry out what is due: one Manager row per incident, one Windows
    message for an owner incident after 10 minutes. Returns the open incidents for the health file."""
    now = now or datetime.now(timezone.utc)
    module = incidents_module()
    state_path = Path(a.incident_file)
    log_path = state_path.with_name("incidents.jsonl")
    roots = {Path(project).name: project for project in a.project}
    state, dropped = module.read(state_path)
    if dropped:  # damaged records were removed; their problems open again as new incidents
        module.log_event(log_path, {"at": now.isoformat(), "event": "state_repaired", "dropped": dropped})
    events, actions = module.step(state, [module.classify(text, list(roots)) for text in report["problems"]], now)
    for event in events:
        module.log_event(log_path, event)
    for action in actions:
        incident = state["incidents"][action["key"]]
        if action["do"] == "manager_row":
            root = roots.get(incident.get("project")) or OFFICE
            try:
                ok, detail = module.queue_row(root, module.row_text(incident, action["n"]), module.dedupe_key(incident, action["n"]))
            except (OSError, sqlite3.Error):
                continue  # tried again in the next pass; the outbox dedupe key makes a repeated row harmless
            if not ok:  # no row can be queued: say why once; an outbox that is not installed is final, a missing Manager is tried again
                done = incident.setdefault("done", {})
                if done.get("skipped") != detail:
                    module.log_event(log_path, {"at": now.isoformat(), "event": "row_skipped", "key": incident["key"], "detail": detail})
                    done["skipped"] = detail
                if not detail.startswith(module.NO_OUTBOX):
                    continue
            module.mark(state, incident["key"], "row", now, action["upto"])
        elif action["do"] == "popup" and a.notify and show_owner_message(incident):
            module.mark(state, incident["key"], "popup", now)
            module.log_event(log_path, {"at": now.isoformat(), "event": "owner_message", "key": incident["key"]})
    module.save(state_path, state)
    return module.summary(state)


def supervise(argv, interval=600):
    """Keep current inherited Manager context in memory only; never persist Desktop pipe context."""
    while True:
        try:
            main(argv)
        except Exception as error:
            print(json.dumps({"ok": False, "error": type(error).__name__}), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    args = sys.argv[1:]
    # One supervisor and one health pass per Office root; OS locks release on exit.
    import importlib.util
    spec = importlib.util.spec_from_file_location("watchdog_lock", REPO / "scripts/team_room/outbox.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    office = Path(args[args.index("--office-root") + 1]) if "--office-root" in args else REPO
    (office / ".bot-mode").mkdir(parents=True, exist_ok=True)
    if "--supervise" in args:
        args.remove("--supervise")
        supervisor_lock = module.PumpLock(office / ".bot-mode/relay-supervisor.lock")
        if not supervisor_lock.acquire():
            sys.exit(0)
        # Each pass runs through the same single-pass lock as Task Scheduler.
        while True:
            subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), *args], creationflags=hidden_flags())
            time.sleep(600)
    else:
        lock = module.PumpLock(office / ".bot-mode/relay-watchdog.lock")
        if not lock.acquire():
            sys.exit(0)
        try:
            sys.exit(main(args))
        finally:
            lock.release()


