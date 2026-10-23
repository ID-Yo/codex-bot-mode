"""One deterministic Agent Monitor cycle: build the snapshot, audit, write the checkpoint.

python -B scripts/monitor_run.py --run-id <id> [--monitor-dir .]

Reads agent/monitoring.json, the project registry it names and each project's task
records, writes agent/snapshots/run-<id>.json, runs the audit and records the checkpoint
(run ID, status, coverage, delivery_status). Prints one compact JSON object; the model only
delivers report_text when report_required is true and then records the delivery result.
Concurrency is checked first and on every path, from the Monitor's own run leases
(agent/runs/<run-id>.lease.json). Other sessions, chats and transcripts are never opened.
Every failure is an incomplete audit with a report to the recipient from monitoring.json.
All output is English.
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
# The scheduled prompt is an unescaped JSON string value; the same words quoted inside another
# chat's tool output or code are escaped and do not match.
LEASE_FRESH_SECONDS = 120
LEASE_LIMIT = 4096


def utc(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def lease_path(runs_dir, run_id):
    return Path(runs_dir) / (re.sub(r"[^A-Za-z0-9._-]", "_", run_id)[:100] + ".lease.json")


def write_lease(runs_dir, run_id, status, now):
    """Atomic write of this run's own lease file."""
    import os
    import tempfile
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(runs_dir), prefix="lease-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump({"run_id": run_id, "status": status, "updated_at": utc(now)}, out)
    os.replace(temporary, lease_path(runs_dir, run_id))


def other_invocations(runs_dir, run_id, now):
    """Fresh unfinished leases of other Monitor runs. Returns (rows, readable); stale leases are abandoned."""
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return [], True
    rows, readable = [], True
    own = lease_path(runs_dir, run_id).name
    try:
        files = list(runs_dir.glob("*.lease.json"))
    except OSError:
        return [], False
    for path in files:
        if path.name == own:
            continue
        try:
            if path.stat().st_size > LEASE_LIMIT:
                raise ValueError("lease too large")
            row = json.loads(path.read_text(encoding="utf-8"))
            stamp = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
            if row["status"] != "active" or not isinstance(row["run_id"], str):
                continue
            if 0 <= (now - stamp).total_seconds() <= LEASE_FRESH_SECONDS:
                rows.append({"run_id": row["run_id"], "status": "active", "updated_at": utc(stamp)})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            readable = False
    return rows, readable


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


def load_config(monitor_dir):
    config = json.loads((monitor_dir / "agent" / "monitoring.json").read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("monitoring.json must be an object")
    return config


def build(config, run_id, now, invocations, invocations_available):
    registry = json.loads(Path(config["registry_path"]).read_text(encoding="utf-8-sig"))
    interval = int(config.get("interval_minutes", 20))
    tasks, unavailable, covered_projects = [], [], 0
    rows = registry.get("projects", [])
    if not isinstance(rows, list):
        raise ValueError("registry projects must be a list")
    for index, project in enumerate(rows):
        pid = project.get("id") if isinstance(project, dict) else None
        root = project.get("root") if isinstance(project, dict) else None
        if not isinstance(pid, str) or not pid or not isinstance(root, str) or not root:
            unavailable.append(f"registry-entry:{pid if isinstance(pid, str) and pid else index} (missing id or root)")
            continue
        try:
            tasks.extend(project_tasks(pid, root))
            covered_projects += 1
        except Exception as error:
            unavailable.append(f"project:{pid} ({type(error).__name__})")
    if not invocations_available:
        unavailable.append("invocation-metadata")
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
            "evidence": [f"ref:monitor-run:{run_id}", f"ref:registry-projects:{covered_projects}/{len(rows)}"],
            "invocations": [*invocations, {"run_id": run_id, "status": "active", "updated_at": utc(now)}]}}
    return snapshot, {"projects": len(rows), "projects_covered": covered_projects}


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


def run(monitor_dir, run_id, now=None):
    now = now or datetime.now(timezone.utc)
    monitor_dir = Path(monitor_dir).resolve()
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", run_id)[:100]
    runs_dir = monitor_dir / "agent" / "runs"
    # 1. Concurrency first, independent of everything else (own leases only).
    try:
        invocations, available = other_invocations(runs_dir, run_id, now)
    except Exception:
        invocations, available = [], False
    concurrent = bool(invocations)
    try:
        write_lease(runs_dir, run_id, "active", now)
    except OSError:
        available = False
    # 2. Recipient and configuration, independent of the registry.
    config, extra = {}, []
    try:
        config = load_config(monitor_dir)
    except Exception as error:
        extra.append({"code": "config_unavailable", "scope": "monitor",
                      "detail": f"agent/monitoring.json could not be read ({type(error).__name__}); no report recipient"})
    # 3. Snapshot.
    snapshot, stats, build_error = None, {"projects": 0, "projects_covered": 0}, None
    try:
        if not config:
            raise ValueError("no configuration")
        snapshot, stats = build(config, run_id, now, invocations or [], available)
    except Exception as error:
        build_error = type(error).__name__
    snapshot_path = None
    if snapshot is not None:
        folder = monitor_dir / "agent" / "snapshots"
        folder.mkdir(parents=True, exist_ok=True)
        snapshot_path = folder / f"run-{safe_id}.json"
        snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        cycle = tc.check_monitor_cycle(snapshot, now, current_run_id=run_id)
        findings, complete = [*extra, *cycle["findings"]], cycle["audit_complete"]
        concurrent = concurrent or any(f["code"] == "concurrent_audit" for f in cycle["findings"])
    else:
        findings = [*extra, {"code": "snapshot_build_failed", "scope": "monitor",
                             "detail": f"Snapshot could not be built ({build_error})"},
                    {"code": "audit_incomplete", "scope": "monitor", "detail": "Audit did not complete"}]
        if concurrent:
            findings.append({"code": "concurrent_audit", "scope": "monitor", "detail": "Another fresh monitor invocation is active"})
        complete = False
    state_file = monitor_dir / "agent" / (f"run-{safe_id}-state.md" if concurrent else "STATE.md")
    state, new_alerts = tc.update_monitor_state(state_file, findings, snapshot, now, complete=complete, run_id=run_id)
    audit = (snapshot or {}).get("monitor_audit", {})
    unavailable = audit.get("unavailable_scopes") or (["snapshot"] if snapshot is None else [])
    coverage = audit.get("coverage", "none")
    report = (not complete) or bool(new_alerts)
    state["delivery_status"] = "not_attempted" if report else "not_required"
    monitor_checkpoint.save_checkpoint(state_file, dict(state))
    if not report:
        try:
            write_lease(runs_dir, run_id, "finished", now)
        except OSError:
            pass
    text = report_text(run_id, complete, coverage, unavailable, new_alerts if complete else findings,
                       state_file.name, audit.get("next_check_at", "unknown")) if report else None
    return {"ok": complete and not findings, "audit_complete": complete, "run_id": run_id,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "coverage": coverage, "projects": stats["projects"], "projects_covered": stats["projects_covered"],
            "tasks": len((snapshot or {}).get("tasks", [])), "unavailable_scopes": unavailable,
            "findings": [{"code": f["code"], "scope": f["scope"]} for f in findings[:MAX_FINDINGS]],
            "new_alerts": len(new_alerts), "state_file": str(state_file), "private_state": concurrent,
            "report_required": report, "report_thread_id": config.get("report_target_thread_id") if report else None,
            "report_text": text, "delivery_status": state["delivery_status"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--monitor-dir", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    result = run(args.monitor_dir, args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
