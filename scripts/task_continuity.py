"""Audit an explicit task evidence snapshot; never dispatch, repair or schedule work.

Vendored core from open-dreamdocs:
Source: open-dreamdocs (pinned commit: aedd92a61f2e4830d86582754fe59f5fbf6a08a7)
License: MIT (Copyright (c) 2026 open-dreamdocs contributors)
Pinned commit: aedd92a61f2e4830d86582754fe59f5fbf6a08a7
License: MIT
"""
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import sys
import tempfile


def check(snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    findings = []

    def finding(code, scope, detail):
        findings.append({"code": code, "scope": scope, "detail": detail})

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    def evidence(value):
        return isinstance(value, list) and bool(value) and all(text(v) for v in value)

    def timestamp(value, scope, field, future=False):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None or (not future and result > now):
                raise ValueError()
            return result
        except (ValueError, TypeError, AttributeError):
            finding("invalid_time", scope, field + " needs a timezone-aware observation (not future)")
            return None

    def deadline(value, scope, code):
        result = timestamp(value, scope, "next_check_at", future=True)
        if result and result <= now:
            finding(code, scope, "Next check is due; reconcile actual evidence")

    if not isinstance(snapshot, dict) or type(snapshot.get("version")) is not int or snapshot["version"] != 1:
        return {"ok": False, "findings": [{"code": "invalid_snapshot", "scope": "snapshot",
                                           "detail": "Expected version 1 object"}]}
    timestamp(snapshot.get("reconciled_at"), "snapshot", "reconciled_at")
    deadline(snapshot.get("next_check_at"), "snapshot", "reconciliation_overdue")
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, list):
        finding("invalid_tasks", "snapshot", "tasks must be an explicitly reconciled array")
        tasks = []
    active = False
    seen = set()
    for task in tasks:
        if not isinstance(task, dict):
            finding("invalid_task", "unknown", "Expected task object")
            continue
        ident = task.get("id")
        scope = ident if text(ident) else "unknown"
        required = {"id", "manager", "executor", "status", "start_approved", "next_action",
                    "acceptance", "evidence", "updated_at", "next_check_at", "handoffs",
                    "blocker", "candidate", "review", "accepted"}
        if not required <= task.keys():
            finding("missing_field", scope, "Required task fields missing: " + ", ".join(sorted(required - task.keys())))
        if task.get("executor") is not None and not text(task["executor"]):
            finding("invalid_executor", scope, "executor must be identity or null")
        for key in ("blocker", "candidate", "review", "accepted"):
            if task.get(key) is not None and not isinstance(task[key], dict):
                finding("invalid_field", scope, key + " must be object or null")
        if not text(ident) or scope in seen:
            finding("invalid_id", scope, "Task IDs must be nonempty and unique")
        seen.add(scope)
        for key in ("manager", "next_action"):
            if not text(task.get(key)):
                finding("missing_field", scope, key + " is required")
        for key in ("acceptance", "evidence"):
            if not evidence(task.get(key)):
                finding("missing_field", scope, key + " needs nonempty references/criteria")
        state = task.get("status")
        if not isinstance(state, str):
            state = "invalid"
        if state not in {"backlog", "ready", "in-progress", "review", "blocked", "paused", "future", "done"}:
            finding("invalid_status", scope, "Unknown task status")
        if type(task.get("start_approved")) is not bool:
            finding("invalid_approval", scope, "start_approved must be explicit boolean")
        if state in {"in-progress", "review", "blocked", "done"} and task.get("start_approved") is not True:
            finding("approval_mismatch", scope, "Started/completed work requires recorded start approval")
        updated = timestamp(task.get("updated_at"), scope, "updated_at")
        excluded = state in {"backlog", "paused", "future", "done"}
        if not excluded:
            deadline(task.get("next_check_at"), scope, "task_overdue")
        started_work = not excluded and (task.get("start_approved") is True or state in {"in-progress", "review", "blocked"})
        active = active or started_work
        blocker = task.get("blocker")
        if state == "blocked":
            if not isinstance(blocker, dict) or not all(text(blocker.get(k)) for k in ("reason", "owner", "resume_condition")) or not evidence(blocker.get("evidence")):
                finding("blocker_missing", scope, "Blocked work needs reason, unblock owner, resume condition and evidence")
            else:
                deadline(blocker.get("next_check_at"), scope, "blocker_overdue")
        elif blocker is not None and state not in {"paused", "backlog", "future"}:
            finding("blocker_status", scope, "Record a current blocker using blocked status")
        handoffs = task.get("handoffs")
        if not isinstance(handoffs, list):
            finding("invalid_handoffs", scope, "handoffs must be an ordered durable array")
            handoffs = []
        ids = set()
        running = []
        previous_handoff = None
        for h in handoffs:
            if not isinstance(h, dict):
                finding("invalid_handoff", scope, "Expected handoff object")
                continue
            hid = h.get("id")
            if not {"id", "worker", "stage", "requested_at", "acknowledged_at", "started_at", "finished_at", "evidence"} <= h.keys():
                finding("invalid_handoff", scope, "All handoff fields are required; unobserved timestamps use null")
            if not text(hid) or hid in ids or not text(h.get("worker")) or h.get("stage") not in ("implementation", "review", "qa", "integration"):
                finding("invalid_handoff", scope, "Unique handoff ID, worker and concrete stage required")
            if text(hid):
                ids.add(hid)
            previous = latest_seen = None
            for field in ("requested_at", "acknowledged_at", "started_at", "finished_at"):
                value = h.get(field)
                if value is not None:
                    current = timestamp(value, scope, field)
                    # A reply time proves the finish; it does not need an observed acknowledgment or start time.
                    if field not in ("requested_at", "finished_at") and previous is None:
                        finding("handoff_order", scope, "Request, acknowledgment, start and finish must be ordered")
                    if current and (previous or latest_seen) and current < (previous or latest_seen):
                        finding("handoff_order", scope, "Handoff timestamps out of order")
                    previous = current
                    latest_seen = current or latest_seen
                else:
                    previous = None
            if h.get("requested_at") is None:
                finding("invalid_handoff", scope, "Durable request timestamp required")
            if not evidence(h.get("evidence")):
                finding("handoff_evidence", scope, "References to request/ack/actual activity required")
            if h.get("started_at") and not h.get("finished_at"):
                running.append(h)
            if state == "ready" and h.get("started_at") and task.get("start_approved") is not True:
                finding("approval_mismatch", scope, "Unapproved ready work reports actual worker start")
            if h.get("started_at") and previous_handoff is not None:
                prior_end = previous_handoff.get("finished_at")
                current_start = timestamp(h["started_at"], scope, "started_at")
                prior_finish = timestamp(prior_end, scope, "finished_at") if prior_end else None
                if not prior_finish or (current_start and current_start < prior_finish):
                    finding("handoff_overlap", scope, "Successor cannot start before previous stage finishes")
            previous_handoff = h
        if started_work and state != "blocked":
            last = handoffs[-1] if handoffs and isinstance(handoffs[-1], dict) else {}
            if last.get("finished_at"):
                finding("stage_finished", scope, "Inspect finished turn and verify successor start or record blocker")
            elif not last.get("started_at") and last.get("start_check") != "relay":
                finding("worker_not_started", scope, "Dispatch/ack/ready is not actual worker start")
            if not text(task.get("executor")) or last.get("worker") != task.get("executor"):
                finding("executor_mismatch", scope, "Current executor must match latest handoff worker")
            if len(running) > 1:  # no observed running start is Unknown, not a conflict
                finding("active_handoffs", scope, "Use one current stage; reconcile superseded/conflicting handoffs")
            if state == "review" or last.get("stage") == "review":
                candidate = task.get("candidate")
                if not isinstance(candidate, dict) or not all(text(candidate.get(k)) for k in ("id", "author")):
                    finding("candidate_missing", scope, "Review needs exact immutable candidate and author")
                elif last.get("worker") == candidate["author"]:
                    finding("review_not_independent", scope, "Current reviewer must differ from candidate author")
                if last.get("stage") != "review":
                    finding("review_stage", scope, "Review status needs a current review handoff")
        if state == "done":
            candidate, review, accepted = (task.get(k) for k in ("candidate", "review", "accepted"))
            reviewed_at = None
            if not isinstance(candidate, dict) or not all(text(candidate.get(k)) for k in ("id", "author")):
                finding("candidate_missing", scope, "Exact immutable candidate and author identity required")
                candidate = {}
            if not isinstance(review, dict) or not text(review.get("reviewer")) or not evidence(review.get("evidence")):
                finding("review_missing", scope, "Independent review identity and evidence required")
            else:
                reviewed_at = timestamp(review.get("reviewed_at"), scope, "reviewed_at")
                if review.get("candidate") != candidate.get("id") or review.get("verdict") != "approve":
                    finding("review_mismatch", scope, "Review must approve exact accepted candidate")
                if review.get("reviewer") == candidate.get("author"):
                    finding("review_not_independent", scope, "Author PASS is not independent review")
            if not isinstance(accepted, dict) or accepted.get("by") != task.get("manager") or not evidence(accepted.get("evidence")):
                finding("acceptance_missing", scope, "Manager acceptance evidence required")
            else:
                accepted_at = timestamp(accepted.get("accepted_at"), scope, "accepted_at")
                if accepted_at and reviewed_at and accepted_at < reviewed_at:
                    finding("acceptance_order", scope, "Manager acceptance cannot precede independent review")
                if accepted.get("candidate") != candidate.get("id"):
                    finding("acceptance_mismatch", scope, "Manager must accept exact reviewed candidate")
        if updated and snapshot.get("reconciled_at"):
            reconciled = timestamp(snapshot["reconciled_at"], "snapshot", "reconciled_at")
            if reconciled and updated > reconciled:
                finding("unreconciled_task", scope, "Task update newer than snapshot reconciliation")
    runtime = snapshot.get("runtime")
    if not isinstance(runtime, dict):
        finding("runtime_missing", "runtime", "Explicit external scheduler/host observations required, including unknown")
    else:
        if (not {"scheduler", "host", "observed_at", "next_check_at", "evidence"} <= runtime.keys()
                or runtime.get("scheduler") not in ("healthy", "unhealthy", "unknown", "not_configured")
                or runtime.get("host") not in ("online", "offline", "unknown")
                or not isinstance(runtime.get("evidence"), list)
                or not all(text(v) for v in runtime.get("evidence", []))):
            finding("invalid_runtime", "runtime", "Explicit valid scheduler/host observation fields required")
        if active:
            if runtime.get("scheduler") != "healthy" or runtime.get("host") != "online" or not evidence(runtime.get("evidence")):
                finding("runtime_unverified", "runtime", "Scheduler health/online host not currently evidenced; configuration is insufficient")
            timestamp(runtime.get("observed_at"), "runtime", "observed_at")
            deadline(runtime.get("next_check_at"), "runtime", "runtime_overdue")
    return {"ok": not findings, "findings": findings,
            "limits": "Reported snapshot only; references and worker activity are not independently verified. No scheduler, repair or dispatch."}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result



OVERDUE_REMINDER_INTERVAL = timedelta(hours=6)  # unresolved overdue findings are re-reported at most this often
LEDGER_KEYS = ("first_seen", "status", "escalated", "reported", "alerted_at", "manager")  # manager: delivery of Manager-routed signals


# Only these deadline findings are repeated. Everything else (metadata findings, hygiene notes, Manager signal codes
# such as stage_finished, request signals) keeps its report-once behaviour, and findings of paused tasks never exist.
REMINDER_CODES = frozenset({"task_overdue", "reconciliation_overdue", "blocker_overdue", "runtime_overdue"})


def reminder_due(entry, now, interval):
    """True when the last report is at least interval old. A missing, unparsable or timezone-less time counts as due."""
    try:
        last = datetime.fromisoformat(str(entry.get("alerted_at") or entry.get("first_seen")).replace("Z", "+00:00"))
        if last.tzinfo is None:
            return True
        return now - last >= interval
    except (ValueError, TypeError, OverflowError):
        return True


def update_monitor_state(state_file_path, findings, snapshot=None, now=None, *, complete=True, run_id=None,
                         reminder_interval=OVERDUE_REMINDER_INTERVAL):
    now = now or datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path = Path(state_file_path).resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if state_path.exists():
        try:
            if state_path.suffix.lower() == ".md":
                from monitor_checkpoint import load_checkpoint
                state = load_checkpoint(state_path)
            else:
                content = state_path.read_text(encoding="utf-8")
                state = json.loads(content)
            if not isinstance(state, dict):
                raise ValueError("State root must be a JSON object")
        except Exception as e:
            raise ValueError(f"Corrupt state file: {state_path} ({e})") from e

    state["last_check"] = {
        "as_of_utc": now_str,
        "active_findings": len(findings),
    }
    state["last_status"] = ("audit_incomplete" if not complete else "clean" if not findings else "findings")
    state["audit_success"] = bool(complete)
    if isinstance(snapshot, dict) and isinstance(snapshot.get("monitor_audit"), dict):
        audit = snapshot["monitor_audit"]
        state.update({"run_id": audit.get("run_id"), "audit_status": "complete" if complete else "incomplete",
                      "coverage": audit.get("coverage", "unknown"),
                      "observed_at": audit.get("observed_at"), "next_check_at": audit.get("next_check_at"),
                      "unavailable_scopes": audit.get("unavailable_scopes", []),
                      "delivery_status": state.get("delivery_status", "unconfirmed")})
    if run_id:
        # The checkpoint always belongs to this invocation, even when the snapshot was missing or invalid.
        if not (isinstance(snapshot, dict) and isinstance(snapshot.get("monitor_audit"), dict)):
            state.update({"audit_status": "incomplete", "coverage": "none", "observed_at": now_str,
                          "next_check_at": now_str, "unavailable_scopes": ["snapshot"]})
            state.setdefault("delivery_status", "not_attempted")
        state["run_id"] = run_id
        if not complete:
            state["audit_status"] = "incomplete"

    task_deadlines = {}
    if isinstance(snapshot, dict) and isinstance(snapshot.get("tasks"), list):
        for t in snapshot["tasks"]:
            if isinstance(t, dict) and isinstance(t.get("id"), str):
                nca = t.get("next_check_at")
                if nca and isinstance(nca, str):
                    try:
                        task_deadlines[t["id"]] = datetime.fromisoformat(nca.replace("Z", "+00:00"))
                    except Exception:
                        pass

    existing_ledger = state.get("alert_ledger", {})
    if not isinstance(existing_ledger, dict):
        existing_ledger = {}

    current_fps = set()
    new_alerts = []
    for f in findings:
        scope = f.get("scope", "snapshot")
        code = f.get("code", "unknown")
        fp = f"{code}:{scope}"
        if fp in current_fps:
            continue  # one signal per fingerprint in a run
        current_fps.add(fp)

        task_deadline = task_deadlines.get(scope)
        is_overdue = False
        if task_deadline and task_deadline <= now:
            is_overdue = True
        elif code in ("task_overdue", "reconciliation_overdue", "blocker_overdue", "runtime_overdue"):
            is_overdue = True

        if fp not in existing_ledger:
            existing_ledger[fp] = {
                "first_seen": now_str,
                "last_seen": now_str,
                "detail": f.get("detail"),
                "status": "escalated" if is_overdue else "new",
                "escalated": is_overdue,
                "reported": False,
                "alerted_at": now_str,
            }
            new_alerts.append(f)
        elif run_id and existing_ledger[fp].get("reported") is not True:
            # Monitor runs only: seen before but never confirmed as delivered (a repeated invocation, a failed or
            # unknown delivery, or a ledger from before this flag): still a new signal for the recipient.
            entry = existing_ledger[fp]
            entry.update(last_seen=now_str, detail=f.get("detail"), status="new", reported=False, alerted_at=now_str)
            new_alerts.append(f)
        else:
            entry = existing_ledger[fp]
            entry["last_seen"] = now_str
            entry["detail"] = f.get("detail")
            if is_overdue:
                entry["status"] = "re_escalated"
                entry["escalated"] = True
                if run_id and code in REMINDER_CODES and entry.get("reported") is True and reminder_due(entry, now, reminder_interval):
                    # A delivered, still unresolved overdue finding is reported again once per interval. The entry stays
                    # delivered, so a reminder whose delivery is unknown is not retried before the next interval.
                    entry["alerted_at"] = now_str
                    new_alerts.append({**f, "detail": f"{f.get('detail')} (reminder: unresolved since {entry.get('first_seen')})"})
            else:
                entry["status"] = "deduplicated"

    if complete:
        existing_ledger = {fp: data for fp, data in existing_ledger.items() if fp in current_fps}
    state["alert_ledger"] = existing_ledger
    if run_id or (isinstance(snapshot, dict) and isinstance(snapshot.get("monitor_audit"), dict)):
        state["active_alert_count"] = len(existing_ledger)

    if state_path.suffix.lower() == ".md":
        from monitor_checkpoint import save_checkpoint
        # The bounded block keeps fingerprints and timing only; details live in the report and snapshot.
        slim = {fp: {k: v for k, v in entry.items() if k in LEDGER_KEYS}
                for fp, entry in state["alert_ledger"].items()}
        save_checkpoint(state_path, {**state, "alert_ledger": slim})
        return state, new_alerts

    import os, tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(state_path.parent), prefix="monitor-state-", suffix=".tmp")
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as out:
        json.dump(state, out, indent=2)
        out.write(chr(10))
    os.replace(tmp_path, str(state_path))
    return state, new_alerts


def check_monitor_cycle(snapshot, now=None, *, current_run_id=None):
    """Check task evidence and whether this invocation completed a fresh, covered audit."""
    now = now or datetime.now(timezone.utc)
    result = check(snapshot, now)
    findings = list(result["findings"])
    audit = snapshot.get("monitor_audit") if isinstance(snapshot, dict) else None
    ids = {task.get("id") for task in snapshot.get("tasks", [])
           if isinstance(task, dict) and isinstance(task.get("id"), str)} if isinstance(snapshot, dict) else set()
    raw_covered = audit.get("covered_tasks", []) if isinstance(audit, dict) else []
    covered = {value for value in raw_covered if isinstance(value, str)} if isinstance(raw_covered, list) else set()
    complete = isinstance(audit, dict)

    def fail(code, detail):
        nonlocal complete
        complete = False
        findings.append({"code": code, "scope": "monitor", "detail": detail})

    if not isinstance(audit, dict):
        fail("audit_incomplete", "Monitor audit checkpoint is missing")
        audit = {}
    observed = None
    try:
        observed = datetime.fromisoformat(audit.get("observed_at", "").replace("Z", "+00:00"))
        if observed.tzinfo is None or observed > now or (now - observed).total_seconds() > 30 * 60:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        fail("audit_stale", "Monitor audit observation is missing, stale, or invalid")
    if not isinstance(current_run_id, str) or not current_run_id or audit.get("run_id") != current_run_id:
        fail("audit_run_mismatch", "Checkpoint does not belong to this invocation")
    if audit.get("status") != "complete":
        fail("audit_incomplete", "Monitor audit did not complete")
    if audit.get("coverage") != "complete" or not ids <= covered:
        fail("audit_incomplete", "Monitor task coverage is partial; unobserved findings were retained")
    unavailable = audit.get("unavailable_scopes")
    if not isinstance(unavailable, list) or not all(isinstance(value, str) and value.strip() for value in unavailable):
        fail("audit_incomplete", "Unavailable scopes must be explicitly listed")
    elif unavailable:
        fail("audit_incomplete", "Some project/task scopes were unavailable")
    try:
        next_check = datetime.fromisoformat(audit.get("next_check_at", "").replace("Z", "+00:00"))
        if next_check.tzinfo is None or next_check <= now:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        fail("audit_incomplete", "A future next-check time is required")
    if not isinstance(audit.get("evidence"), list) or not audit["evidence"] or not all(isinstance(value, str) and value.strip() for value in audit["evidence"]):
        fail("audit_incomplete", "Monitor audit has no evidence references")

    invocations = audit.get("invocations")
    current = [row for row in invocations if isinstance(row, dict) and row.get("run_id") == current_run_id] if isinstance(invocations, list) else []
    if len(current) != 1 or current[0].get("status") not in ("active", "completed"):
        fail("audit_incomplete", "Current invocation identity/status is not verified")
    else:
        try:
            current_time = datetime.fromisoformat(current[0].get("updated_at", "").replace("Z", "+00:00"))
            if current_time.tzinfo is None or current_time > now or (now - current_time).total_seconds() > 30 * 60:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            fail("audit_stale", "Current invocation metadata is not fresh")
    active = []
    for row in invocations if isinstance(invocations, list) else []:
        if not isinstance(row, dict) or row.get("status") not in ("active", "running") or row.get("run_id") == current_run_id:
            continue
        try:
            stamp = datetime.fromisoformat(row.get("updated_at", "").replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp > now:
                raise ValueError()
            if (now - stamp).total_seconds() <= 120:
                active.append(row)
        except (ValueError, TypeError, AttributeError):
            fail("concurrency_unknown", "Another invocation has no trustworthy fresh timestamp")
    if active:
        fail("concurrent_audit", "Another fresh monitor invocation is active")
    elif any(isinstance(row, dict) and row.get("status") in ("active", "running") and row.get("run_id") != current_run_id for row in invocations or []):
        fail("concurrency_unknown", "An older active record cannot prove current concurrency")

    audit_complete = complete
    if not audit_complete:
        findings.append({"code": "audit_incomplete", "scope": "monitor", "detail": "Audit did not complete with fresh invocation identity and full coverage"})
    return {"ok": audit_complete and not findings, "audit_complete": audit_complete,
            "findings": findings, "current_run_id": current_run_id,
            "limits": "Task findings apply only to listed snapshot evidence; stale invocation metadata is unknown, not concurrency proof."}



import re
from datetime import timedelta

from contextlib import nullcontext
from urllib.request import Request, build_opener, ProxyHandler

class PreconnectConfigurationError(ConnectionError):
    """Raised when relay or roster configuration is missing before connection initiation."""
    pass


class RouteLock:
    def __init__(self, lock_path, lease_seconds=60):
        self.lock_path = Path(lock_path)
        self.lease_seconds = lease_seconds
        self.acquired = False

    def acquire(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        now_ts = datetime.now(timezone.utc).timestamp()
        if self.lock_path.exists():
            try:
                data = json.loads(self.lock_path.read_text(encoding="utf-8"))
                lock_time = data.get("acquired_at", 0)
                if now_ts - lock_time < self.lease_seconds:
                    raise RuntimeError(f"Concurrent routing in progress by PID {data.get('pid')} since {data.get('acquired_at')}")
            except (ValueError, OSError):
                pass
        
        lock_data = {
            "pid": os.getpid(),
            "acquired_at": now_ts,
            "acquired_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self.lock_path.parent), prefix="route-lock-", suffix=".tmp")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(lock_data, f)
        os.replace(tmp_path, str(self.lock_path))
        self.acquired = True
        return self

    def release(self):
        if self.acquired and self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except OSError:
                pass
            self.acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def _relay_base_url(url):
    """Shared validation and normalization from team_room/relay_url.py (same rules as RelayBridge)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("cbm_relay_url", str(Path(__file__).resolve().parent / "team_room" / "relay_url.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.relay_base_url(url)


REQUEST_TEXT_FIELDS = ("link", "to", "from", "summary", "author", "candidate")


def clean_text(value):
    return value if isinstance(value, str) else ""


def clean_request(row):
    """Copy of a ledger row whose text fields are strings.

    requests.py open without --link used to store "link": null, and a hand-edited ledger can hold any value; every
    consumer of ledger text (lower(), re, startswith) must see "" instead of None."""
    row = dict(row)
    row["id"] = str(row.get("id") or "")
    for field in REQUEST_TEXT_FIELDS:
        value = row.get(field)
        row[field] = value if isinstance(value, str) else ""
    return row


def get_req_ledger(project_path):
    project_path = Path(project_path).resolve()
    req_file = project_path / ".bot-mode" / "requests.py"
    if not req_file.exists():
        req_file = Path(__file__).resolve().parent / "team_room" / "requests.py"
    if not req_file.exists():
        req_file = Path(__file__).resolve().parents[1] / ".bot-mode" / "requests.py"
    if not req_file.exists():
        raise FileNotFoundError(f"Cannot locate requests.py for {project_path}")
    import importlib.util
    spec = importlib.util.spec_from_file_location("cbm_requests_ledger", str(req_file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AUTHORITY_LINE = re.compile(r"^\s*(?:[-*]\s*)?\**(?:authority|tracking)\**\s*:\s*\**\s*(\S+?)\**\s*$", re.I | re.M)


def record_authority(txt):
    """The one explicit Authority/Tracking URL of a task record, or None when absent or when the lines disagree."""
    found = {m.group(1).rstrip("/") for m in AUTHORITY_LINE.finditer(txt)}
    return found.pop() if len(found) == 1 else None


def parse_markdown_metadata(txt):
    meta = {}
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("-") or line.startswith("*"):
            cleaned = line.lstrip("-* ").strip()
            if ":" in cleaned:
                k, v = cleaned.split(":", 1)
                meta[k.replace("*", "").strip().lower()] = v.replace("*", "").strip()
    return meta


def bound_thread(project_path, bot_id):
    """Thread appointed to a bot in the project roster (bindings file as fallback)."""
    project_path = Path(project_path).resolve()
    roster_file = project_path / ".bot-mode" / "roster.json"
    if not roster_file.exists():
        raise PreconnectConfigurationError(f"Roster not found for {project_path}")

    r_data = json.loads(roster_file.read_text(encoding="utf-8"))
    thread_id = None
    for p in r_data.get("participants", []):
        if p.get("bot") == bot_id or p.get("bot_id") == bot_id:
            thread_id = p.get("thread_id")
            break

    if not thread_id:
        bindings_file = project_path / ".bot-mode" / "local" / "bindings.json"
        if bindings_file.exists():
            try:
                b_data = json.loads(bindings_file.read_text(encoding="utf-8"))
                bound = b_data.get(bot_id) or b_data.get(f"{project_path.name}:{bot_id}")
                if isinstance(bound, dict):
                    thread_id = bound.get("thread_id")
                elif isinstance(bound, str):
                    thread_id = bound
            except Exception:
                pass

    if not thread_id:
        raise PreconnectConfigurationError(f"No appointed thread ID found in roster or bindings for bot {bot_id}")
    return thread_id


def relay_call(project_path, name, arguments, timeout=15):
    """POST one tool call to the project's local Desktop Relay and return the parsed response body.

    The relay only serves appointed threads and authenticates with the token in relay.json. If relay.json is missing
    or malformed, raises PreconnectConfigurationError before any connection is made."""
    project_path = Path(project_path).resolve()
    relay_file = project_path / ".bot-mode" / "desktop-relay" / "relay.json"
    if not relay_file.exists():
        relay_file = project_path / ".bot-mode" / "relay.json"
    if not relay_file.exists():
        relay_file = project_path / "relay.json"
    if not relay_file.exists():
        raise PreconnectConfigurationError("No Desktop Relay active (relay.json not found)")

    try:
        desc = json.loads(relay_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise PreconnectConfigurationError(f"Cannot read relay.json: {e}") from e

    url = desc.get("url")
    token = desc.get("token")
    if not url or not token:
        raise PreconnectConfigurationError("Malformed Desktop Relay descriptor")
    try:
        url = _relay_base_url(url)
    except ValueError as e:
        raise PreconnectConfigurationError(f"Untrusted Desktop Relay address: {e}") from e

    req = Request(
        f"{url}/call",
        data=json.dumps({"name": name, "arguments": arguments}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Office-Relay": token}
    )
    with build_opener(ProxyHandler({})).open(req, timeout=timeout) as resp:
        return json.load(resp)


def default_desktop_relay_adapter(project_path, bot_id, req_id, summary):
    """Attempt dispatch via local Desktop Relay if available.
    
    Reads appointed thread from roster.json. If relay.json exists in project,
    posts send_message_to_thread via POST /call. If configuration is missing before
    connect, raises PreconnectConfigurationError.
    """
    thread_id = bound_thread(project_path, bot_id)
    res = relay_call(project_path, "send_message_to_thread", {
        "threadId": thread_id,
        "prompt": f"Stage handoff [{req_id}]: {summary}"
    })

    if isinstance(res, dict) and "error" in res:
        raise RuntimeError(f"Relay error: {res['error']}")

    result = res.get("result") if isinstance(res, dict) else None
    if result is None or not isinstance(result, dict) or result.get("isError") is True:
        raise RuntimeError(f"Relay call failed: {result}")

    if result.get("threadId") or result.get("receipt_id"):
        return {
            "receipt_id": result.get("receipt_id") or result.get("threadId"),
            "status": "delivered",
            "result": result
        }
    else:
        raise RuntimeError("Relay call returned no delivery confirmation")


def produce_snapshot(project_dir, now=None, explicit_only=False):
    now = now or datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    future_str = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    project_path = Path(project_dir).resolve()

    roster_file = project_path / ".bot-mode" / "roster.json"
    participants = []
    manager = "manager"
    if roster_file.exists():
        try:
            r_data = json.loads(roster_file.read_text(encoding="utf-8"))
            participants = r_data.get("participants", [])
            for p in participants:
                if p.get("role") == "Manager":
                    manager = p.get("bot", "manager")
                    break
        except Exception:
            pass

    req_ledger = get_req_ledger(project_path)
    requests_data = req_ledger.load(project_path)
    requests_list = [clean_request(r) for r in requests_data.get("requests", []) if isinstance(r, dict)]

    tasks = []
    tasks_dir = project_path / "work" / "tasks"
    if tasks_dir.exists():
        for task_file in sorted(tasks_dir.glob("*.md")):
            txt = task_file.read_text(encoding="utf-8")
            meta = parse_markdown_metadata(txt)
            if explicit_only and not (meta.get("state") or meta.get("status")):
                continue

            task_id = meta.get("task id") or task_file.stem
            task_mgr = meta.get("responsible manager") or meta.get("owner") or manager
            executor = meta.get("assigned executor") or meta.get("assigned bot id")
            if executor and executor.lower() in ("unassigned", "none", "null"):
                executor = None

            raw_state = (meta.get("state") or meta.get("status") or "backlog").lower()
            state_map = {
                "in_progress": "in-progress",
                "in-progress": "in-progress",
                "completed": "done",
                "done": "done",
                "ready": "ready",
                "backlog": "backlog",
                "review": "review",
                "blocked": "blocked",
                "paused": "paused",
                "future": "future"
            }
            status = state_map.get(raw_state, "backlog")

            raw_approved = meta.get("start approved")
            start_approved = bool(raw_approved and raw_approved.lower() in ("true", "yes", "1"))
            # Auditable approval source for ready work: who, when, for which scope, on what evidence. start_approved
            # alone is a flag, not proof.
            approval_parts = {k: (meta.get("start approval " + k) or "").strip() for k in ("by", "at", "scope", "evidence")}
            start_approval = dict(approval_parts) if all(approval_parts.values()) else None

            next_action = meta.get("action")
            if not next_action:
                if "## Next Action" in txt:
                    part = txt.split("## Next Action", 1)[1].split("##", 1)[0].strip()
                    lines = [l.strip().lstrip("-* ") for l in part.splitlines() if l.strip()]
                    if lines:
                        next_action = lines[0]
            if not next_action:
                next_action = f"Progress task {task_id}"

            acc_list = []
            if "## Acceptance Criteria" in txt:
                part = txt.split("## Acceptance Criteria", 1)[1].split("##", 1)[0].strip()
                for l in part.splitlines():
                    cleaned = l.strip().lstrip("0123456789.-* ").strip()
                    if cleaned:
                        acc_list.append(cleaned)
            if not acc_list:
                acc_list = [f"Acceptance criteria defined for {task_id}"]

            ev_list = []
            if meta.get("latest evidence"):
                ev_list.append(meta.get("latest evidence"))
            if "## Evidence and Artifacts" in txt:
                part = txt.split("## Evidence and Artifacts", 1)[1].split("##", 1)[0].strip()
                for l in part.splitlines():
                    cleaned = l.strip().lstrip("0123456789.-* ").strip()
                    if cleaned and cleaned not in ev_list:
                        ev_list.append(cleaned)
            if not ev_list:
                ev_list = [f"ref:task:{task_id}"]

            mtime = datetime.fromtimestamp(task_file.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            local_req_id = meta.get("local request id")
            if local_req_id and local_req_id.lower() in ("none", "null"):
                local_req_id = None

            candidate = None
            cand_sha = meta.get("candidate sha")
            if cand_sha:
                candidate = {"id": cand_sha, "author": executor or "builder"}
            else:
                # Check matching request for candidate/author structured fields or summary fallback
                for r in reversed(requests_list):
                    if (local_req_id and r.get("id") == local_req_id) or (task_id.lower() in clean_text(r.get("id")).lower()) or (task_id.lower() in clean_text(r.get("link")).lower()):
                        sha = r.get("candidate")
                        auth = r.get("author")
                        s_text = r.get("summary") or ""
                        if not sha:
                            shas_40 = re.findall(r"(?<![0-9A-Za-z])[0-9a-f]{40}(?![0-9A-Za-z])", s_text)
                            unique_shas = list(dict.fromkeys(s.lower() for s in shas_40))
                            if len(unique_shas) == 1:
                                sha = unique_shas[0]
                            elif len(unique_shas) > 1:
                                marker_match = re.search(r"\b(?:candidate|commit)[:\s]+([0-9a-f]{40})\b", s_text, re.IGNORECASE)
                                if marker_match:
                                    sha = marker_match.group(1).lower()
                        if not auth:
                            m_auth = re.search(r"\bauthor[:\s]+([a-z][a-z0-9-]{0,47})\b", s_text, re.IGNORECASE)
                            if m_auth:
                                auth = m_auth.group(1).lower()
                        if not auth and r.get("from") and r.get("from") not in ("manager", "owner"):
                            auth = r.get("from")
                        if sha and auth:
                            candidate = {"id": sha, "author": auth}
                            break

            review = None
            rev_verdict = meta.get("review verdict")
            if rev_verdict:
                v = "approve" if rev_verdict.lower() in ("approve", "approved", "pass") else "request_changes"
                review = {
                    "candidate": candidate["id"] if candidate else "unknown",
                    "reviewer": "code-reviewer",
                    "verdict": v,
                    "reviewed_at": mtime,
                    "evidence": ["ref:review-evidence"]
                }

            accepted = None
            acc_sha = meta.get("accepted candidate")
            if acc_sha:
                accepted = {
                    "candidate": acc_sha,
                    "by": task_mgr,
                    "accepted_at": mtime,
                    "evidence": ["ref:manager-acceptance"]
                }

            blocker = None
            if status == "blocked":
                b_reason = meta.get("blocked reason")
                b_owner = meta.get("unblock owner")
                b_resume = meta.get("resume condition")
                if b_reason and b_owner and b_resume:
                    blocker = {
                        "reason": b_reason,
                        "owner": b_owner,
                        "resume_condition": b_resume,
                        "next_check_at": future_str,
                        "evidence": [f"ref:blocker:{task_id}"]
                    }

            handoffs = []
            matching_reqs = []
            for r in requests_list:
                r_id = clean_text(r.get("id"))
                r_link = clean_text(r.get("link"))
                r_sum = clean_text(r.get("summary"))
                if (local_req_id and r_id == local_req_id) or (task_id.lower() in r_id.lower()) or (task_id.lower() in r_link.lower()) or (task_id.lower() in r_sum.lower()):
                    matching_reqs.append(r)

            def parse_opened(r):
                val = r.get("opened")
                if not val:
                    return datetime.min.replace(tzinfo=timezone.utc)
                try:
                    return datetime.fromisoformat(val.replace("Z", "+00:00"))
                except Exception:
                    return datetime.min.replace(tzinfo=timezone.utc)
            matching_reqs.sort(key=parse_opened)
            for r in matching_reqs:
                w = clean_text(r.get("to")) or "builder"
                stg = "implementation"
                if w in ("code-reviewer", "reviewer") or "review" in clean_text(r.get("id")):
                    stg = "review"
                elif w in ("verifier", "qa") or "qa" in clean_text(r.get("id")):
                    stg = "qa"
                elif w == task_mgr or "integration" in clean_text(r.get("id")):
                    stg = "integration"

                ack = r.get("acknowledged_at")
                started = r.get("started_at")
                fin = r.get("reply", {}).get("at") if (r.get("state") == "done" and isinstance(r.get("reply"), dict)) else None

                handoffs.append({
                    "id": r.get("id"),
                    "worker": w,
                    "stage": stg,
                    "requested_at": r.get("opened"),
                    "acknowledged_at": ack,
                    "started_at": started,
                    "finished_at": fin,
                    "evidence": [r.get("link") or f"ref:request:{r.get('id')}"]
                })

            tasks.append({
                "id": task_id,
                "manager": task_mgr,
                "executor": executor,
                "status": status,
                "start_approved": start_approved,
                "start_approval": start_approval,
                "authority": record_authority(txt),
                "next_action": next_action,
                "acceptance": acc_list,
                "evidence": ev_list,
                "updated_at": mtime,
                "next_check_at": future_str,
                "handoffs": handoffs,
                "blocker": blocker,
                "candidate": candidate,
                "review": review,
                "accepted": accepted
            })

    snapshot = {
        "version": 1,
        "reconciled_at": now_str,
        "next_check_at": future_str,
        "runtime": {
            "scheduler": "healthy",
            "host": "online",
            "observed_at": now_str,
            "next_check_at": future_str,
            "evidence": ["ref:desktop-runtime-local"]
        },
        "tasks": tasks
    }
    return snapshot


def ledger_update(ledger, project_path, change):
    """One locked read-change-write of the request ledger (requests.update)."""
    return ledger.update(project_path, change)


def route_stage(project_dir, apply=False, allowlist=None, delivery_adapter=None, now=None):
    now = now or datetime.now(timezone.utc)
    project_path = Path(project_dir).resolve()

    if apply and allowlist is not None:
        allowed = [str(Path(p).resolve()) for p in allowlist]
        if str(project_path) not in allowed:
            raise PermissionError(f"Project {project_path} is not in configured routing allowlist: {allowed}")

    lock_file = project_path / ".bot-mode" / "local" / "route.lock"
    # Defect 1: Acquire RouteLock BEFORE reading live snapshot, checking project pause, loading requests.json, and computing actions.
    lock_ctx = RouteLock(lock_file) if apply else nullcontext()
    with lock_ctx:
        # Check project pause under lock
        if (project_path / ".bot-mode" / "PAUSED").exists():
            return {"ok": True, "dry_run": not apply, "routed": [], "deduplicated": [], "errors": [], "skipped": ["project_paused"], "plan": []}

        req_ledger = get_req_ledger(project_path)
        if apply and not callable(getattr(req_ledger, "update", None)):
            # Fail closed before any ledger write or delivery: only the locked update() is safe against concurrent writers.
            message = "Request helper has no update(); update the project's requests.py helper first, then run route --apply again"
            return {"ok": False, "dry_run": False, "routed": [], "deduplicated": [], "errors": [{"action": "error", "error": message}],
                    "skipped": ["unsupported_ledger_helper"], "plan": []}
        snapshot = produce_snapshot(project_path, now=now)

        roster_file = project_path / ".bot-mode" / "roster.json"
        active_bots = []
        if roster_file.exists():
            try:
                r_data = json.loads(roster_file.read_text(encoding="utf-8"))
                active_bots = [p["bot"] for p in r_data.get("participants", []) if p.get("status") == "active"]
            except Exception:
                pass

        existing_requests = [clean_request(r) for r in req_ledger.load(project_path).get("requests", []) if isinstance(r, dict)]
        open_requests = [r for r in existing_requests if r.get("state") == "open"]

        actions = []
        for task in snapshot.get("tasks", []):
            t_id = task.get("id")
            t_status = task.get("status")
            if t_status in ("paused", "backlog", "future", "done", "blocked"):
                continue
            if not task.get("start_approved"):
                continue

            handoffs = task.get("handoffs", [])
            if not handoffs:
                continue
            last_h = handoffs[-1]
            if not last_h.get("finished_at"):
                continue

            successor_started = False
            for h in handoffs[handoffs.index(last_h)+1:]:
                if h.get("started_at"):
                    successor_started = True
                    break
            if successor_started:
                continue

            stage = last_h.get("stage")
            if stage in ("implementation", "revision"):
                target = "code-reviewer"
                if target not in active_bots:
                    if "verifier" in active_bots:
                        target = "verifier"
                    else:
                        actions.append({
                            "task_id": t_id,
                            "action": "error",
                            "error": "No active reviewer found in roster",
                            "target": None
                        })
                        continue

                dup = any(r.get("to") == target and t_id.lower() in clean_text(r.get("id")).lower() for r in open_requests)
                if dup:
                    actions.append({
                        "task_id": t_id,
                        "action": "deduplicated",
                        "reason": f"Open review request already exists for {target}",
                        "target": target
                    })
                    continue

                actions.append({
                    "task_id": t_id,
                    "action": "route_review",
                    "target": target,
                    "sender": task.get("manager", "manager"),
                    "request_id": f"{t_id.lower()}-review-{now.strftime('%Y%m%d%H%M')}",
                    "summary": f"Review deliverable for {t_id}",
                    "link": f"work/tasks/{t_id}.md",
                    "next_state": "ready"
                })

            elif stage in ("review", "qa"):
                review = task.get("review") or {}
                verdict = review.get("verdict")
                if not verdict:
                    reply_outcome = None
                    for r in existing_requests:
                        if r.get("id") == last_h.get("id") and isinstance(r.get("reply"), dict):
                            reply_outcome = r["reply"].get("outcome")
                    if reply_outcome in ("request_changes", "changes_requested", "failed"):
                        verdict = "request_changes"
                    else:
                        verdict = "approve"

                if verdict in ("request_changes", "changes_requested"):
                    target = "developer"
                    dup = any(r.get("to") == target and t_id.lower() in clean_text(r.get("id")).lower() for r in open_requests)
                    if dup:
                        actions.append({
                            "task_id": t_id,
                            "action": "deduplicated",
                            "reason": f"Open corrections request already exists for {target}",
                            "target": target
                        })
                        continue

                    actions.append({
                        "task_id": t_id,
                        "action": "route_corrections",
                        "target": target,
                        "sender": task.get("manager", "manager"),
                        "request_id": f"{t_id.lower()}-corrections-{now.strftime('%Y%m%d%H%M')}",
                        "summary": f"Implement review corrections for {t_id}",
                        "link": f"work/tasks/{t_id}.md",
                        "next_state": "ready"
                    })
                elif verdict == "approve":
                    target = task.get("manager", "manager")
                    dup = any(r.get("to") == target and t_id.lower() in clean_text(r.get("id")).lower() for r in open_requests)
                    if dup:
                        actions.append({
                            "task_id": t_id,
                            "action": "deduplicated",
                            "reason": f"Open integration request already exists for {target}",
                            "target": target
                        })
                        continue

                    actions.append({
                        "task_id": t_id,
                        "action": "route_integration",
                        "target": target,
                        "sender": last_h.get("worker", "code-reviewer"),
                        "request_id": f"{t_id.lower()}-integration-{now.strftime('%Y%m%d%H%M')}",
                        "summary": f"Review approved; accept and integrate {t_id}",
                        "link": f"work/tasks/{t_id}.md",
                        "next_state": "ready"
                    })

        actually_routed = [a for a in actions if a.get("action") in ("route_review", "route_corrections", "route_integration")]
        deduped = [a for a in actions if a.get("action") == "deduplicated"]
        errs = [a for a in actions if a.get("action") == "error"]

        if not apply:
            return {"ok": True, "dry_run": True, "routed": actually_routed, "deduplicated": deduped, "errors": errs, "plan": actions}

        for act in actually_routed:
            # Each ledger write is one locked read-change-write step; no ledger data is held across delivery.
            created = []

            def add_request(data, act=act, created=created):
                if any(r.get("id") == act["request_id"] for r in data["requests"]):
                    return
                created.append(True)
                req_ledger.open_request(data, act["request_id"], act["sender"], act["target"], act["summary"],
                                        link=act["link"], at=now)
                next(r for r in data["requests"] if r["id"] == act["request_id"])["delivery_status"] = "outbox_queued"
            ledger_update(req_ledger, project_path, add_request)
            if not created:
                # Already recorded by an earlier run: never deliver it a second time.
                act["delivery"] = "already_recorded"
                continue

            # Defect 3: Task Markdown state transitions to 'ready' awaiting worker start (NEVER in_progress or review before start)
            t_file = project_path / "work" / "tasks" / f"{act['task_id']}.md"
            if t_file.exists():
                c = t_file.read_text(encoding="utf-8")
                lines = c.splitlines()
                new_lines = []
                for l in lines:
                    if l.strip().startswith("- **State**:") or l.strip().startswith("- State:"):
                        new_lines.append(f"- **State**: ready")
                    elif l.strip().startswith("- **Local Request ID**:") or l.strip().startswith("- Local Request ID:"):
                        new_lines.append(f"- **Local Request ID**: {act['request_id']}")
                    else:
                        new_lines.append(l)
                t_file.write_text(chr(10).join(new_lines) + chr(10), encoding="utf-8")

            adapter_to_call = delivery_adapter if delivery_adapter is not None else default_desktop_relay_adapter
            fields = {}
            try:
                receipt = adapter_to_call(project_path, act["target"], act["request_id"], act["summary"])
                if not isinstance(receipt, dict) or not receipt:
                    raise RuntimeError(f"Malformed or empty receipt from adapter: {receipt}")
                if receipt.get("isError") is True or "error" in receipt:
                    raise RuntimeError(f"Adapter reported error in receipt: {receipt}")
                status = receipt.get("status")
                receipt_id = receipt.get("receipt_id") or receipt.get("threadId")
                if status not in ("delivered", "sent") or not receipt_id:
                    raise RuntimeError(f"Receipt missing valid delivery confirmation: {receipt}")

                fields["delivery_status"] = "delivered"
                fields["delivered_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                fields["delivery_receipt"] = receipt
                act["delivery"] = "delivered"
            except PreconnectConfigurationError as conn_err:
                fields["delivery_status"] = "outbox_queued"
                fields["delivery_note"] = str(conn_err)
                act["delivery"] = "outbox_queued"
            except Exception as err:
                fields["delivery_status"] = "delivery_unknown"
                fields["delivery_error"] = str(err)
                act["delivery"] = "delivery_unknown"
                act["delivery_error"] = str(err)

            def record_delivery(data, act=act, fields=fields):
                item = next((r for r in data["requests"] if r.get("id") == act["request_id"]), None)
                if item is not None:
                    item.update(fields)
            ledger_update(req_ledger, project_path, record_delivery)

        return {"ok": True, "dry_run": False, "routed": actually_routed, "deduplicated": deduped, "errors": errs, "plan": actions}


def main(argv=None):
    raw_args = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="Explicit snapshot JSON file (direct check)")

    subparsers = parser.add_subparsers(dest="command")

    check_parser = subparsers.add_parser("check", help="Check snapshot consistency")
    check_parser.add_argument("--file", type=Path, required=True, help="Explicit snapshot JSON file")

    audit_parser = subparsers.add_parser("audit", help="Audit snapshot and atomically update monitor state")
    audit_parser.add_argument("--file", type=Path, required=True, help="Explicit snapshot JSON file")
    audit_parser.add_argument("--state-file", type=Path, default=Path("monitor-state.json"), help="State file path")

    monitor_parser = subparsers.add_parser("monitor-audit", help="Audit one scheduled monitor cycle and record truthful completion")
    monitor_parser.add_argument("--file", type=Path, required=True, help="Explicit reconciled monitor snapshot")
    monitor_parser.add_argument("--state-file", type=Path, required=True, help="Monitor-owned dedup/checkpoint file")
    monitor_parser.add_argument("--run-id", required=True, help="Fresh invocation ID from supported scheduler metadata")

    snap_parser = subparsers.add_parser("snapshot", help="Produce valid ODD snapshot from live project")
    snap_parser.add_argument("--project", type=Path, default=Path("."), help="Project root directory")
    snap_parser.add_argument("--output", type=Path, help="Optional output JSON path")

    route_parser = subparsers.add_parser("route", help="Safe authorized stage continuation router")
    route_parser.add_argument("--project", type=Path, default=Path("."), help="Project root directory")
    # Defect 2: CLI mutually exclusive group for --apply and --dry-run
    mode_group = route_parser.add_mutually_exclusive_group()
    mode_group.add_argument("--apply", action="store_true", help="Apply routing changes")
    mode_group.add_argument("--dry-run", action="store_true", help="Dry-run preview without changes (default)")
    route_parser.add_argument("--allowlist", action="append", help="Allowed project paths for apply")

    try:
        args = parser.parse_args(raw_args)
    except SystemExit as e:
        return e.code

    if args.command is None:
        if args.file is None:
            parser.error("Explicit task snapshot path is required (--file <path>)")
        args.command = "check"

    if args.command == "check":
        try:
            snapshot = json.loads(args.file.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
            result = check(snapshot)
        except (OSError, ValueError, UnicodeError) as error:
            result = {"ok": False, "findings": [{"code": "unreadable_snapshot", "scope": "snapshot",
                                                  "detail": type(error).__name__ + ": input unavailable or invalid"}]}
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    elif args.command in ("audit", "monitor-audit"):
        snapshot = None
        try:
            snapshot = json.loads(args.file.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
            result = check(snapshot)
        except (OSError, ValueError, UnicodeError) as error:
            result = {"ok": False, "findings": [{"code": "unreadable_snapshot", "scope": "snapshot",
                                                  "detail": type(error).__name__ + ": input unavailable or invalid"}]}
            if args.command == "audit":
                print(json.dumps(result, indent=2))
                return 1
        try:
            cycle = check_monitor_cycle(snapshot or {}, current_run_id=args.run_id) if args.command == "monitor-audit" else None
            findings = cycle["findings"] if cycle else result["findings"]
            complete = cycle["audit_complete"] if cycle else True
            state, new_alerts = update_monitor_state(args.state_file, findings, snapshot, complete=complete,
                                                     run_id=args.run_id if args.command == "monitor-audit" else None)
        except Exception as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        overall_ok = cycle["ok"] if cycle else result["ok"]
        print(json.dumps({
            "ok": overall_ok,
            "audit_complete": complete,
            "findings": findings,
            "new_alerts": new_alerts,
            "last_check": state["last_check"],
            "alert_ledger_count": len(state["alert_ledger"]),
            "new_alerts_count": len(new_alerts),
            "limits": cycle["limits"] if cycle else result.get("limits"),
        }, indent=2))
        return 0 if overall_ok else 1
    elif args.command == "snapshot":
        try:
            snap = produce_snapshot(args.project)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(snap, indent=2) + chr(10), encoding="utf-8")
                print(f"Snapshot written to {args.output}")
            else:
                print(json.dumps(snap, indent=2))
            return 0
        except Exception as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
    elif args.command == "route":
        try:
            res = route_stage(args.project, apply=bool(args.apply), allowlist=args.allowlist)
            print(json.dumps(res, indent=2))
            return 0 if res.get("ok") else 1
        except Exception as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
    else:
        parser.error(f"Unknown command: {args.command}")
        return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
