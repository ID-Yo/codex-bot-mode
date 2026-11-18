
import json,os,subprocess,sys,hashlib,time,urllib.request
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
OFFICE = REPO  # the checkout that owns the relay directory; the relay program refuses a root outside its working directory
SYNCED = ("outbox.py", "requests.py")


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


def restart_relay():
    """Only the appointed Manager chat can register the relay (it needs that chat's own app tools pipe)."""
    if not (os.environ.get("CODEX_THREAD_ID") and os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")):
        return False, "not in a Manager chat: cannot register the relay here"
    return run_starter(["powershell", "-NoProfile", "-File", str(OFFICE / "scripts" / "start_codex_office.ps1"), "-RelayOnly"])


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


def project_report(project):
    out = {"project": str(project)}
    r = subprocess.run([sys.executable, "-B", str(Path(project) / ".bot-mode" / "outbox.py"), "--project", str(project),
                        "status", "--heal"], capture_output=True, text=True, timeout=60, creationflags=hidden_flags())
    try:
        out["outbox"] = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out["outbox"] = {"problems": ["outbox status failed: " + (r.stderr or r.stdout)[-200:]]}
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
    ap.add_argument("--office-url", default="http://localhost:24242")
    ap.add_argument("--restart-office", action="store_true", help="Start Codex Office when it does not answer")
    ap.add_argument("--notify", action="store_true", help="Show a Windows message when health turns bad")
    a = ap.parse_args(argv)
    global OFFICE
    OFFICE = Path(a.office_root).resolve()
    a.relay_root = a.relay_root or str(OFFICE / ".bot-mode" / "desktop-relay")
    a.health_file = a.health_file or str(OFFICE / ".bot-mode" / "relay-health.json")
    if a.all_projects:
        a.project = list(dict.fromkeys(a.project + registered_projects(OFFICE)))

    report = {"at": datetime.now(timezone.utc).isoformat(), "relay_ok": relay_ok(a.relay_root), "problems": []}
    if not report["relay_ok"]:
        try:
            record_incident(a.relay_root)
        except Exception:
            pass
    if not report["relay_ok"] and a.restart:
        try:
            ok, detail = restart_relay()
        except (OSError, subprocess.TimeoutExpired) as error:
            ok, detail = False, type(error).__name__
        report["restart"] = {"ok": ok, "detail": detail}
        report["relay_ok"] = ok and relay_ok(a.relay_root)
    if not report["relay_ok"]:
        report["problems"].append("Desktop relay down: in the Manager chat run powershell -File scripts/start_codex_office.ps1 -RelayOnly")
    report["office_ok"] = office_ok(a.office_url)
    if not report["office_ok"] and a.restart_office:
        try:
            ok, detail = restart_office()
        except (OSError, subprocess.TimeoutExpired) as error:
            ok, detail = False, type(error).__name__
        report["office_restart"] = {"ok": ok, "detail": detail}
        report["office_ok"] = ok and office_ok(a.office_url)
    if not report["office_ok"]:
        report["problems"].append("Codex Office does not answer on " + a.office_url)
    for root in projects_missing_outbox(OFFICE):
        report["problems"].append(f"{Path(root).name}: .bot-mode/requests.py is installed but .bot-mode/outbox.py is missing, "
                                  "so completion reports are not delivered; install outbox.py and outbox-config.json (#415)")
    report["projects"] = []
    for project in a.project:
        try:
            report["projects"].append(project_report(project))
        except Exception as error:
            report["projects"].append({"project": str(project), "outbox": {"problems": ["health check failed: " + type(error).__name__]}})
    for p in report["projects"]:
        report["problems"] += [f"{Path(p['project']).name}: {x}" for x in p["outbox"].get("problems", []) if "relay is not reachable" not in x]
        if p.get("installed_differs_from_repo"):
            report["problems"].append(f"{Path(p['project']).name}: installed {', '.join(p['installed_differs_from_repo'])} differ from the repo")
    health = Path(a.health_file)
    was_bad = False
    if health.is_file():
        try:
            was_bad = bool(json.loads(health.read_text(encoding="utf-8")).get("problems"))
        except ValueError:
            pass
    health.parent.mkdir(parents=True, exist_ok=True)
    temporary = health.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(health)
    if report["problems"] and a.notify and not was_bad and os.name == "nt":
        subprocess.run(["msg", "*", "/TIME:600", "Codex relay/outbox: " + "; ".join(report["problems"])[:400]], capture_output=True, timeout=10, creationflags=hidden_flags())
    print(json.dumps({"ok": not report["problems"], "relay_ok": report["relay_ok"], "office_ok": report["office_ok"], "problems": report["problems"]}, ensure_ascii=False))
    return 1 if report["problems"] else 0


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


