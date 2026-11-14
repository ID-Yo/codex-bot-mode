
import json,os,subprocess,sys,hashlib,time,urllib.request
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
OFFICE = REPO  # the checkout that owns the relay directory; the relay program refuses a root outside its working directory
SYNCED = ("outbox.py",)  # requests.py is patched per project until #404 is merged


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
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    r = subprocess.run(["powershell", "-NoProfile", "-File", str(OFFICE / "scripts" / "start_codex_office.ps1"), "-NoBrowser"],
                       cwd=str(OFFICE), capture_output=True, text=True, timeout=120, creationflags=flags)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-300:]


def hidden_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def restart_relay():
    """Only the appointed Manager chat can register the relay (it needs that chat's own app tools pipe)."""
    if not (os.environ.get("CODEX_THREAD_ID") and os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")):
        return False, "not in a Manager chat: cannot register the relay here"
    r = subprocess.run(["powershell", "-NoProfile", "-File", str(OFFICE / "scripts" / "start_codex_office.ps1"), "-RelayOnly"],
                       cwd=str(OFFICE), capture_output=True, text=True, timeout=120, creationflags=hidden_flags())
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-300:]


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


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Zero-token health check of the Desktop relay and the project outboxes (#401).")
    ap.add_argument("--office-root", default=str(REPO), help="Checkout that owns the relay (default: this one)")
    ap.add_argument("--relay-root", default=None)
    ap.add_argument("--project", action="append", default=[], help="Project root with an outbox (repeatable)")
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

    report = {"at": datetime.now(timezone.utc).isoformat(), "relay_ok": relay_ok(a.relay_root), "problems": []}
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


