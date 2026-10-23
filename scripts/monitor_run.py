"""One deterministic Agent Monitor cycle: build the snapshot, audit, write the checkpoint.

python -B scripts/monitor_run.py --run-id <id> [--monitor-dir .]

Reads agent/monitoring.json, the project registry it names and each project's task
records, writes agent/snapshots/run-<id>.json, runs the audit and records the checkpoint
(run ID, status, coverage, delivery_status). Prints one compact JSON object; the model only
delivers report_text when report_required is true and then records the delivery result.
All output is English. Private memory, chats and transcripts are never read.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import monitor_checkpoint
import task_continuity as tc

MAX_FINDINGS = 12


def utc(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def other_invocations(sessions_root, run_id, now):
    """Fresh, unfinished Agent Monitor scheduled sessions other than this one (None = source unavailable)."""
    root = Path(sessions_root)
    if not root.is_dir():
        return None
    rows = []
    local = datetime.now()
    for day in {local, local - timedelta(days=1)}:
        folder = root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        if not folder.is_dir():
            continue
        for path in folder.glob("rollout-*.jsonl"):
            try:
                stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if run_id in path.name or (now - stamp).total_seconds() > 120 or stamp > now + timedelta(seconds=5):
                    continue
                with path.open("rb") as stream:
                    head = stream.read(6000)
                    stream.seek(max(0, path.stat().st_size - 8000))
                    tail = stream.read()
            except OSError:
                continue
            if b"Automation: Agent Monitor" in head and b'"task_complete"' not in tail:
                rows.append({"run_id": path.stem.split("-", 7)[-1], "status": "active", "updated_at": utc(stamp)})
    return rows


def project_tasks(pid, root):
    """Tasks of one project, ids prefixed with the project id. No task folder means no tasks."""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError("project root unavailable")
    if not (root / "work" / "tasks").is_dir():
        return []
    snapshot = tc.produce_snapshot(root)
    tasks = snapshot["tasks"]
    for task in tasks:
        task["id"] = f"{pid}/{task['id']}"
        for handoff in task.get("handoffs", []):
            handoff["id"] = f"{pid}/{handoff['id']}"
    return tasks


def build(monitor_dir, run_id, now, sessions_root):
    config = json.loads((monitor_dir / "agent" / "monitoring.json").read_text(encoding="utf-8-sig"))
    registry = json.loads(Path(config["registry_path"]).read_text(encoding="utf-8-sig"))
    interval = int(config.get("interval_minutes", 20))
    tasks, unavailable, covered_projects = [], [], 0
    projects = [p for p in registry.get("projects", []) if isinstance(p, dict) and p.get("id") and p.get("root")]
    for project in projects:
        try:
            tasks.extend(project_tasks(project["id"], project["root"]))
            covered_projects += 1
        except Exception as error:
            unavailable.append(f"project:{project['id']} ({type(error).__name__})")
    invocations = other_invocations(sessions_root, run_id, now)
    if invocations is None:
        unavailable.append("invocation-metadata")
        invocations = []
    invocations.append({"run_id": run_id, "status": "active", "updated_at": utc(now)})
    later = utc(now + timedelta(minutes=interval + 10))
    snapshot = {
        "version": 1, "reconciled_at": utc(now), "next_check_at": later,
        "runtime": {"scheduler": "healthy", "host": "online", "observed_at": utc(now), "next_check_at": later,
                    "evidence": [f"ref:monitor-run:{run_id}"]},
        "tasks": tasks,
        "monitor_audit": {
            "run_id": run_id, "status": "complete" if not unavailable else "incomplete",
            "coverage": "complete" if not unavailable else "partial",
            "observed_at": utc(now), "next_check_at": later,
            "covered_tasks": [task["id"] for task in tasks], "unavailable_scopes": unavailable,
            "evidence": [f"ref:monitor-run:{run_id}", f"ref:registry-projects:{covered_projects}/{len(projects)}"],
            "invocations": invocations}}
    return config, snapshot, {"projects": len(projects), "projects_covered": covered_projects}


def report_text(run_id, complete, coverage, unavailable, findings, state_file, next_check):
    lines = [f"Agent Monitor report for run {run_id}.",
             f"Audit: {'complete' if complete else 'INCOMPLETE'}; coverage: {coverage}."]
    if unavailable:
        lines.append("Unavailable scopes: " + "; ".join(unavailable[:10]) + ".")
    if findings:
        lines.append("Findings:")
        lines += [f"- {f['code']} ({f['scope']}): {f['detail']}" for f in findings[:MAX_FINDINGS]]
    lines.append(f"Checkpoint file: {state_file}. Next check: {next_check}. Delivery is recorded by the Monitor after sending.")
    return "\n".join(lines)


def run(monitor_dir, run_id, now=None, sessions_root=None):
    now = now or datetime.now(timezone.utc)
    monitor_dir = Path(monitor_dir).resolve()
    sessions_root = sessions_root or Path.home() / ".codex" / "sessions"
    shared = monitor_dir / "agent" / "STATE.md"
    build_error = None
    try:
        config, snapshot, stats = build(monitor_dir, run_id, now, sessions_root)
    except Exception as error:
        build_error = type(error).__name__
        config, snapshot, stats = {}, None, {"projects": 0, "projects_covered": 0}
    private = False
    snapshot_path = None
    if snapshot is not None:
        folder = monitor_dir / "agent" / "snapshots"
        folder.mkdir(parents=True, exist_ok=True)
        snapshot_path = folder / f"run-{re.sub(r'[^A-Za-z0-9._-]', '_', run_id)[:100]}.json"
        snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        cycle = tc.check_monitor_cycle(snapshot, now, current_run_id=run_id)
        private = any(f["code"] == "concurrent_audit" for f in cycle["findings"])
        findings, complete = cycle["findings"], cycle["audit_complete"]
    else:
        findings = [{"code": "snapshot_build_failed", "scope": "monitor", "detail": f"Snapshot could not be built ({build_error})"},
                    {"code": "audit_incomplete", "scope": "monitor", "detail": "Audit did not complete"}]
        complete = False
    state_file = monitor_dir / "agent" / (f"run-{re.sub(r'[^A-Za-z0-9._-]', '_', run_id)[:100]}-state.md" if private else "STATE.md")
    if not private and not shared.exists():
        shared.parent.mkdir(parents=True, exist_ok=True)
    state, new_alerts = tc.update_monitor_state(state_file, findings, snapshot, now, complete=complete, run_id=run_id)
    audit = (snapshot or {}).get("monitor_audit", {})
    unavailable = audit.get("unavailable_scopes") or (["snapshot"] if snapshot is None else [])
    coverage = audit.get("coverage", "none")
    report = (not complete) or bool(new_alerts)
    state["delivery_status"] = "not_attempted" if report else "not_required"
    monitor_checkpoint.save_checkpoint(state_file, {k: v for k, v in state.items()})
    ok = complete and not findings
    text = report_text(run_id, complete, coverage, unavailable, new_alerts if complete else findings,
                       state_file.name, audit.get("next_check_at", "unknown")) if report else None
    return {"ok": ok, "audit_complete": complete, "run_id": run_id, "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "coverage": coverage, "projects": stats["projects"], "projects_covered": stats["projects_covered"],
            "tasks": len((snapshot or {}).get("tasks", [])), "unavailable_scopes": unavailable,
            "findings": [{"code": f["code"], "scope": f["scope"]} for f in findings[:MAX_FINDINGS]],
            "new_alerts": len(new_alerts), "state_file": str(state_file), "private_state": private,
            "report_required": report, "report_thread_id": config.get("report_target_thread_id") if report else None,
            "report_text": text, "delivery_status": state["delivery_status"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--monitor-dir", type=Path, default=Path("."))
    parser.add_argument("--sessions-root", type=Path)
    args = parser.parse_args(argv)
    result = run(args.monitor_dir, args.run_id, sessions_root=args.sessions_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
