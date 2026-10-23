"""Read only the small Monitor checkpoint needed for initial recall."""
import argparse
import json
from pathlib import Path
import os
import tempfile

STATE_READ_LIMIT = 16 * 1024
DELIVERY_STATUSES = {"sent", "acknowledged", "unconfirmed", "delivery_unknown", "not_attempted", "not_required"}
FIELDS = ("version", "run_id", "audit_status", "coverage", "observed_at",
          "next_check_at", "active_alert_count", "delivery_status", "alert_ledger",
          "unavailable_scopes")
REQUIRED_FIELDS = set(FIELDS) - {"alert_ledger"}
MARKER = "<!-- monitor-checkpoint\n"
END_MARKER = "\n-->"


def _read_prefix(path):
    with Path(path).open("rb") as stream:
        return stream.read(STATE_READ_LIMIT + 1)


def load_checkpoint(path):
    """Load the bounded checkpoint object at the start of a markdown state file."""
    raw = _read_prefix(path)
    if len(raw) > STATE_READ_LIMIT:
        raw = raw[:STATE_READ_LIMIT]
    text = raw.decode("utf-8", errors="replace")
    start = text.find(MARKER)
    end = text.find(END_MARKER, start + len(MARKER)) if start >= 0 else -1
    if start < 0 or end < 0:
        return {}
    data = json.loads(text[start + len(MARKER):end])
    return data if isinstance(data, dict) else {}


def save_checkpoint(path, data):
    """Atomically replace/insert the bounded JSON block while streaming old history."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _read_prefix(path) if path.exists() else b""
    if len(raw) > STATE_READ_LIMIT:
        raw = raw[:STATE_READ_LIMIT]
    text = raw.decode("utf-8", errors="replace")
    start = text.find(MARKER)
    end = text.find(END_MARKER, start + len(MARKER)) if start >= 0 else -1
    if start >= 0 and end >= 0:
        prefix = text[:start].encode("utf-8")
        suffix_offset = len(text[:end + len(END_MARKER)].encode("utf-8"))
    else:
        prefix, suffix_offset = b"", 0
    block = (MARKER + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + END_MARKER + "\n").encode("utf-8")
    if len(block) > STATE_READ_LIMIT:
        raise ValueError("Monitor checkpoint exceeds the bounded size limit")
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix="monitor-checkpoint-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(prefix)
            output.write(block)
            if path.exists():
                with path.open("rb") as source:
                    source.seek(suffix_offset)
                    while chunk := source.read(64 * 1024):
                        output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def set_delivery_status(path, status):
    if status not in DELIVERY_STATUSES:
        raise ValueError("Invalid delivery status")
    data = load_checkpoint(path)
    if not data:
        raise ValueError("Monitor checkpoint is unavailable")
    data["delivery_status"] = status
    save_checkpoint(path, data)
    finish_lease(path, data.get("run_id"))


def finish_lease(state_path, run_id):
    """The Monitor's own run lease ends once the delivery result is recorded."""
    import datetime
    import re
    if not isinstance(run_id, str) or not run_id:
        return
    lease = Path(state_path).parent / "runs" / (re.sub(r"[^A-Za-z0-9._-]", "_", run_id)[:100] + ".lease.json")
    if lease.exists():
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lease.write_text(json.dumps({"run_id": run_id, "status": "finished", "updated_at": stamp}), encoding="utf-8")


def read_initial_context(state_path, memory_path=None):
    """Read a bounded checkpoint prefix; inspect memory size without reading its content."""
    state_path = Path(state_path)
    state_bytes_read = 0
    status = "missing"
    checkpoint = None
    if state_path.exists():
        raw = _read_prefix(state_path)
        state_bytes_read = min(len(raw), STATE_READ_LIMIT)
        if len(raw) > STATE_READ_LIMIT:
            raw = raw[:STATE_READ_LIMIT]
        text = raw.decode("utf-8", errors="replace")
        start = text.find(MARKER)
        end = text.find(END_MARKER, start + len(MARKER)) if start >= 0 else -1
        if start >= 0 and end >= 0:
            try:
                value = json.loads(text[start + len(MARKER):end])
                if isinstance(value, dict):
                    checkpoint = {key: value[key] for key in FIELDS if key in value}
                    status = "available" if REQUIRED_FIELDS <= checkpoint.keys() else "incomplete"
                else:
                    status = "invalid"
            except (ValueError, TypeError):
                status = "invalid"
        elif len(raw) >= STATE_READ_LIMIT:
            status = "unknown"
    try:
        memory_bytes = Path(memory_path).stat().st_size if memory_path is not None else None
    except OSError:
        memory_bytes = None
    return {"checkpoint_status": status, "checkpoint": checkpoint,
            "state_bytes_read": state_bytes_read, "state_read_limit": STATE_READ_LIMIT,
            "memory_loaded": False, "memory_bytes": memory_bytes}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--memory", type=Path)
    parser.add_argument("--set-delivery-status", choices=sorted(DELIVERY_STATUSES))
    args = parser.parse_args(argv)
    try:
        if args.set_delivery_status:
            set_delivery_status(args.state, args.set_delivery_status)
            print(json.dumps({"delivery_status": args.set_delivery_status}))
        else:
            print(json.dumps(read_initial_context(args.state, args.memory), ensure_ascii=False))
        return 0
    except OSError:
        print(json.dumps({"checkpoint_status": "unknown", "checkpoint": None,
                          "state_bytes_read": 0, "state_read_limit": STATE_READ_LIMIT,
                          "memory_loaded": False, "memory_bytes": None}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
