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
from concurrent.futures import ThreadPoolExecutor

import status_sources
import task_continuity as tc

MAX_FINDINGS = 12
# The scheduled prompt is an unescaped JSON string value; the same words quoted inside another
# chat's tool output or code are escaped and do not match.
LEASE_FRESH_SECONDS = 120
LEASE_LIMIT = 4096

REGISTRY_IMPORT_ERROR = None
try:
    from team_room import registry as room_registry
except ImportError as _first_error:
    try:
        import registry as room_registry
    except ImportError:
        room_registry = None
        REGISTRY_IMPORT_ERROR = f"{type(_first_error).__name__}: {_first_error}"[:160]


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


def relay_roots(config, project_root):
    """Project roots to search for the Desktop Relay descriptor, in order: monitoring.json desktop_relay_root, the
    project itself, and the project that owns the registry file (<root>/.bot-mode/local/<registry>.json)."""
    roots = []
    if isinstance(config.get("desktop_relay_root"), str) and config["desktop_relay_root"]:
        roots.append(config["desktop_relay_root"])
    roots.append(project_root)
    registry = Path(config["registry_path"]) if config.get("registry_path") else None
    if registry and registry.parent.name == "local" and registry.parent.parent.name == ".bot-mode":
        roots.append(str(registry.parent.parent.parent))
    return list(dict.fromkeys(roots))


def project_tasks(project, now, interval=20):
    """(tasks, summary) of one project from GitHub labels, the request ledger and explicit task records."""
    return status_sources.project_status(project, now, interval)


def load_config(monitor_dir):
    config = json.loads((monitor_dir / "agent" / "monitoring.json").read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("monitoring.json must be an object")
    return config


def build(config, run_id, now, invocations, invocations_available):
    registry = json.loads(Path(config["registry_path"]).read_text(encoding="utf-8-sig"))
    interval = int(config.get("interval_minutes", 20))
    tasks, unavailable, covered_projects, project_status = [], [], 0, {}
    rows = registry.get("projects", [])
    if not isinstance(rows, list):
        raise ValueError("registry projects must be a list")
    def one(project):
        try:
            return project_tasks(project, now, interval), None
        except Exception as error:
            return None, error
    valid = [p for p in rows if isinstance(p, dict) and isinstance(p.get("id"), str) and p["id"]
             and isinstance(p.get("root"), str) and p["root"]]
    with ThreadPoolExecutor(max_workers=6) as pool:  # GitHub reads dominate the run time; keep a run well under a minute
        results = dict(zip((p["id"] for p in valid), pool.map(one, valid)))
    hygiene, open_requests = {}, {}
    for index, project in enumerate(rows):
        pid = project.get("id") if isinstance(project, dict) else None
        root = project.get("root") if isinstance(project, dict) else None
        if not isinstance(pid, str) or not pid or not isinstance(root, str) or not root:
            unavailable.append(f"registry-entry:{pid if isinstance(pid, str) and pid else index} (missing id or root)")
            continue
        result, error = results[pid]
        if error is not None:
            unavailable.append(f"project:{pid} ({type(error).__name__})")
            project_status[pid] = {"state": "unavailable", "total": 0, "by_status": {}, "sources": [], "problems": [type(error).__name__]}
            continue
        found, summary = result
        tasks.extend(found)
        open_requests[pid] = {"root": root, "relay_roots": relay_roots(config, root),
                              "items": summary.pop("open_requests", [])}
        project_status[pid] = summary
        if summary.get("hygiene"):
            hygiene[pid] = summary["hygiene"]
        if summary["problems"]:
            unavailable.append(f"project:{pid} ({'; '.join(summary['problems'])})")
        elif summary["state"] == "unknown":
            unavailable.append(f"project:{pid} (UNKNOWN: no status-bearing tasks from GitHub labels, ledger or task records)")
        else:
            covered_projects += 1
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
            "project_status": project_status, "hygiene": hygiene,
            "evidence": [f"ref:monitor-run:{run_id}", f"ref:registry-projects:{covered_projects}/{len(rows)}"],
            "invocations": [*invocations, {"run_id": run_id, "status": "active", "updated_at": utc(now)}]}}
    return snapshot, {"projects": len(rows), "projects_covered": covered_projects, "open_requests": open_requests}


def report_text(run_id, complete, coverage, unavailable, findings, state_file, next_check):
    lines = [f"Agent Monitor report for run {run_id}.",
             f"Audit: {'complete' if complete else 'INCOMPLETE'}; coverage: {coverage}."]
    notes = [f for f in findings if f["code"] == "record_not_closed"]
    findings = [f for f in findings if f["code"] != "record_not_closed"]
    if notes:
        grouped = {}
        for f in notes:
            project, _, record = f["scope"].partition("/")
            grouped.setdefault(project, []).append(record)
        lines.append("Hygiene (reported once): records still open although their GitHub issue is closed: " + "; ".join(
            f"{project} {len(ids)} ({', '.join(ids[:3])}{', ...' if len(ids) > 3 else ''})" for project, ids in sorted(grouped.items())) + ".")
    if unavailable:
        lines.append("Unavailable scopes: " + "; ".join(unavailable[:10]) + ".")
    if len(findings) > MAX_FINDINGS:
        by_project = {}
        for f in findings:
            project = f["scope"].split("/", 1)[0] if "/" in f["scope"] else "(monitor)"
            by_project.setdefault(project, {}).setdefault(f["code"], 0)
            by_project[project][f["code"]] += 1
        lines.append("Findings by project: " + "; ".join(
            f"{p} {sum(c.values())} (" + ", ".join(f"{k} {v}" for k, v in sorted(c.items())) + ")"
            for p, c in sorted(by_project.items())) + ".")
    if findings:
        lines.append("Findings:")
        lines += [f"- {f['code']} ({f['scope']}): {f['detail']}" for f in findings[:MAX_FINDINGS]]
    lines.append(f"Checkpoint file: {state_file}. Next check: {next_check}. Delivery is recorded by the Monitor after sending.")
    return "\n".join(lines)


MAX_SIGNAL_ITEMS = 10  # items per signal type in one Manager prompt
# Alert codes the Monitor forwards to the project Manager: (headline, list label, requested action).
SIGNAL_CODES = {
    "stage_finished": ("stage finished without active successor", "Tasks",
                       "Inspect finished turn and start the next executor or record a blocker in the same turn."),
    "request_not_started": ("request opened but not started", "Requests",
                            "Check that the recipient received it, re-dispatch or reassign it, or record a blocker in the same turn."),
    "request_not_started_overflow": ("more requests opened but not started than are listed", "Remaining requests",
                                     "Close, re-dispatch or cancel the old open requests of this project; the oldest are listed separately."),
}


def delivery_finding(project, detail):
    return {"code": "manager_delivery_unavailable", "scope": project, "detail": detail}


def manager_signals(config, new_alerts):
    """(deliveries, findings) for new stage_finished and request_not_started alerts: one delivery per project whose single Manager resolves.

    Nothing is dropped silently: a missing registry module, a missing registry_path, a resolution error or a project
    without a resolvable Manager each become a manager_delivery_unavailable finding for that project."""
    by_project = {}
    for alert in new_alerts:
        if alert.get("code") in SIGNAL_CODES and alert.get("scope"):
            project, _, task = alert["scope"].partition("/")
            task = task or "(project)"
            by_project.setdefault(project, {}).setdefault(alert["code"], []).append((task, alert.get("detail")))
    deliveries, findings = [], []
    registry_file = Path(config["registry_path"]) if config.get("registry_path") else None
    for project, grouped in sorted(by_project.items()):
        if room_registry is None:
            findings.append(delivery_finding(project, "The Manager registry module could not be imported "
                                             f"({REGISTRY_IMPORT_ERROR or 'ImportError'}); the Manager signal was not delivered"))
            continue
        if registry_file is None:
            findings.append(delivery_finding(project, "registry_path is not configured; the Manager signal was not delivered"))
            continue
        try:
            manager = room_registry.resolve_project_manager(registry_file, project)
        except Exception as error:
            findings.append(delivery_finding(project, f"Manager could not be resolved ({type(error).__name__}); the Manager signal was not delivered"))
            continue
        thread = manager.get("thread_id") if isinstance(manager, dict) and manager.get("status") == "ok" else None
        if not thread:
            status = manager.get("status") if isinstance(manager, dict) else "invalid"
            findings.append(delivery_finding(project, f"No Manager thread to notify (resolution status: {status}); the Manager signal was not delivered"))
            continue
        sections = []
        for code, (headline, label, action) in SIGNAL_CODES.items():
            if code in grouped:
                shown = grouped[code][:MAX_SIGNAL_ITEMS]
                lines = "\n".join(f"- {task}: {detail}" for task, detail in shown)
                if len(grouped[code]) > len(shown):
                    lines += f"\n- ... and {len(grouped[code]) - len(shown)} more (see the Monitor report)"
                sections.append(f"Agent Monitor signal for {project} Manager: {headline}.\n{label}:\n{lines}\n{action}")
        deliveries.append({
            "project_id": project,
            "thread_id": thread,
            "manager_bot": manager.get("bot"),
            "prompt": "\n\n".join(sections),
        })
    return deliveries, findings


CHECKPOINT_FINDING_STEPS = (150, 60, 20)  # findings kept when the checkpoint block would exceed its size bound
LEDGER_KEYS = tc.LEDGER_KEYS


def is_size_error(error):
    return isinstance(error, ValueError) and "bounded size limit" in str(error)


def persist_state(state_file, findings, snapshot, now, complete, run_id):
    """update_monitor_state that degrades instead of raising when the findings do not fit the bounded checkpoint.

    On a size error the findings are cut to the most important ones (hygiene notes last) and a checkpoint_overflow
    finding states how many were left out per code, so the report still shows that more exists. The final save of the
    run is checked too (the delivery status is added after this call)."""
    attempt, omitted = list(findings), None
    for limit in (None, *CHECKPOINT_FINDING_STEPS):
        if limit is not None:
            ranked = sorted(findings, key=lambda f: f["code"] == "record_not_closed")
            kept, dropped = ranked[:limit], ranked[limit:]
            counts = {}
            for f in dropped:
                counts[f["code"]] = counts.get(f["code"], 0) + 1
            omitted = {"code": "checkpoint_overflow", "scope": "monitor",
                       "detail": (f"{len(dropped)} finding(s) did not fit the bounded checkpoint and are not tracked: "
                                  + ", ".join(f"{code} {n}" for code, n in sorted(counts.items())))}
            attempt = [*kept, omitted]
        try:
            state, new_alerts = tc.update_monitor_state(state_file, attempt, snapshot, now, complete=complete, run_id=run_id)
            probe = dict(state)
            probe["alert_ledger"] = slim_ledger(state)
            probe["delivery_status"] = "not_attempted"
            monitor_checkpoint.checkpoint_block(probe)  # raises on size without writing
            return state, new_alerts, attempt
        except ValueError as error:
            if not is_size_error(error):
                raise
    raise ValueError("Monitor checkpoint exceeds the bounded size limit even with the findings cut")


def slim_ledger(state):
    ledger = state.get("alert_ledger") if isinstance(state.get("alert_ledger"), dict) else {}
    return {fp: {k: v for k, v in entry.items() if k in LEDGER_KEYS} for fp, entry in ledger.items()}


def save_final_state(state_file, state):
    final = dict(state)
    final["alert_ledger"] = slim_ledger(state)  # details live in the report and the snapshot, not the bounded block
    monitor_checkpoint.save_checkpoint(state_file, final)


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
        for pid, ids in snapshot["monitor_audit"].get("hygiene", {}).items():
            findings.extend({"code": "record_not_closed", "scope": f"{pid}/{rid}",
                             "detail": "Ledger record is open but its GitHub issue is closed"} for rid in ids)
        try:
            findings.extend(status_sources.request_start_findings(stats.get("open_requests", {}), now))
        except Exception as error:
            findings.append({"code": "request_start_unavailable", "scope": "monitor",
                             "detail": f"Request start check failed ({type(error).__name__}); open requests were not verified"})
    else:
        findings = [*extra, {"code": "snapshot_build_failed", "scope": "monitor",
                             "detail": f"Snapshot could not be built ({build_error})"},
                    {"code": "audit_incomplete", "scope": "monitor", "detail": "Audit did not complete"}]
        if concurrent:
            findings.append({"code": "concurrent_audit", "scope": "monitor", "detail": "Another fresh monitor invocation is active"})
        complete = False
    # Shared state is written only when other runs were observed and this run was registered.
    private = concurrent or not available
    state_file = monitor_dir / "agent" / (f"run-{safe_id}-state.md" if private else "STATE.md")
    state, new_alerts, findings = persist_state(state_file, findings, snapshot, now, complete, run_id)
    audit = (snapshot or {}).get("monitor_audit", {})
    unavailable = list(audit.get("unavailable_scopes") or (["snapshot"] if snapshot is None else []))
    if not available and "invocation-metadata" not in unavailable:
        unavailable.append("invocation-metadata")
    coverage = audit.get("coverage", "none")
    report = (not complete) or bool(new_alerts)
    manager_deliveries, delivery_findings = manager_signals(config, new_alerts)
    findings = [*findings, *delivery_findings]
    state["delivery_status"] = "not_attempted" if report else "not_required"
    save_final_state(state_file, state)
    if not report:
        try:
            write_lease(runs_dir, run_id, "finished", now)
        except OSError:
            pass
    text = report_text(run_id, complete, coverage, unavailable,
                       [*new_alerts, *delivery_findings] if complete else findings,
                       state_file.name, audit.get("next_check_at", "unknown")) if report else None

    return {"ok": complete and not findings, "audit_complete": complete, "run_id": run_id,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "coverage": coverage, "projects": stats["projects"], "projects_covered": stats["projects_covered"],
            "tasks": len((snapshot or {}).get("tasks", [])), "unavailable_scopes": unavailable,
            "findings": [{"code": f["code"], "scope": f["scope"]} for f in findings[:MAX_FINDINGS]],
            "new_alerts": len(new_alerts), "state_file": str(state_file), "private_state": private,
            "report_required": report, "report_thread_id": config.get("report_target_thread_id") if report else None,
            "report_text": text, "delivery_status": state["delivery_status"],
            "manager_deliveries": manager_deliveries}


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
