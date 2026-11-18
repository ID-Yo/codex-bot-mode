"""Project-level persistent outbox queue with a zero-token background pump (#399).

Agents queue a short report with `send` and end their turn. A deterministic local pump (no model calls) delivers the
queued reports to the target Manager thread one at a time, in order, only while that thread is idle, and only after the
previous delivery has produced a registered Manager turn that has finished.

States of a queue row: pending, sending, delivered, failed, ambiguous, skipped, expired.
Rules the pump keeps:
- A send whose outcome is unknown (error or timeout after the relay was called, or a pump that stopped mid send) becomes
  `ambiguous`. It is never automatically resent. Later messages may overtake it. The pump checks for the unique marker
  in the actual recipient thread; only a matching user message confirms delivery. An operator may resolve it:
  `outbox.py resolve <id> --as delivered|skipped|retry`.
- `failed` is terminal (for example the target thread could not be resolved). Its dedupe key stays taken, so the same
  report is not queued again; `resolve <id> --as retry` puts it back at its original position after the cause is fixed.
- A pending row older than the TTL (default 12 h) becomes 'expired' and is reported by 'status'; the queue never grows
  silently (#401). While the relay is down the pump keeps waiting and re-reads the relay descriptor, because a restarted
  relay has a new port and token.
- The relay token is only sent to a loopback relay: http to 127.0.0.1, localhost or ::1.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
RESOLUTIONS = {"delivered": "delivered", "skipped": "skipped", "retry": "pending"}
EXIT_HELD = 3  # the queue is held by an ambiguous row
EXIT_NOT_REGISTERED = 4  # a delivered message did not produce a Manager turn in time
EXIT_UNHEALTHY = 5  # status found a problem: relay down with work waiting, no pump, or a held queue
DEFAULT_TTL_HOURS = 12.0
STALE_SECONDS = 600  # a pending row older than this although relay and pump are up is a problem
RELAY_WAIT_MAX = 30.0  # longest pause between relay descriptor checks while the relay is down
HEARTBEAT_EVERY = 10.0
RESOLVE_CACHE_SECONDS = 1800  # the Manager thread rarely changes; the send re-resolves it anyway


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def validate_relay_url(url):
    """Return the relay base URL when it is plain http to a loopback host; raise ValueError otherwise."""
    parts = urllib.parse.urlsplit(str(url))
    try:
        parts.port
    except ValueError:
        raise ValueError("relay URL has an invalid port")
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError("relay URL must be http to 127.0.0.1, localhost or ::1; the relay token is never sent elsewhere")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("relay URL must not carry credentials, a query or a fragment")
    return str(url).rstrip("/")


def find_relay_descriptor(project_root):
    config = Path(project_root) / ".bot-mode" / "outbox-config.json"
    configured = None
    if config.is_file():
        try:
            configured = json.loads(config.read_text(encoding="utf-8")).get("relay_root")
        except (OSError, ValueError):
            configured = None
    candidates = [
        Path(configured) if configured else None,
        Path(project_root) / ".bot-mode" / "desktop-relay",
        Path(project_root) / ".bot-mode",
        Path(os.environ["CODEX_RELAY_ROOT"]) if os.environ.get("CODEX_RELAY_ROOT") else None,
    ]
    for c in candidates:
        if c is None:
            continue
        descriptor = c if c.name == "relay.json" else c / "relay.json"
        if not descriptor.is_file():
            continue
        try:
            data = json.loads(descriptor.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not (data.get("url") and data.get("token")):
            continue
        try:
            validate_relay_url(data["url"])
        except ValueError as error:
            print(f"Outbox pump: ignoring relay descriptor {descriptor}: {error}", file=sys.stderr)
            continue
        return descriptor.parent, data
    return None, None


def resolve_target_thread(project_root, target):
    """Resolve target bot name (e.g. 'manager') to thread_id via roster.py/roster.json."""
    if len(target) == 36 and target.count("-") == 4:
        return target  # already a UUID thread ID

    roster_file = Path(project_root) / ".bot-mode" / "roster.json"
    if not roster_file.is_file():
        roster_file = Path(project_root) / "roster.json"

    # Try using roster.py links if available
    roster_py = Path(project_root) / ".bot-mode" / "roster.py"
    if roster_py.is_file():
        try:
            cmd = [sys.executable, "-B", str(roster_py), "--project", str(project_root), "links"]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0  # no console flash from the pump
            out = subprocess.check_output(cmd, text=True, encoding="utf-8", timeout=5, creationflags=flags)
            links = json.loads(out)
            for item in links:
                if item.get("bot") == target or item.get("role", "").lower() == target.lower():
                    if item.get("thread_id"):
                        return item["thread_id"]
        except Exception:
            pass

    # Direct parse fallback
    if roster_file.is_file():
        try:
            data = json.loads(roster_file.read_text(encoding="utf-8"))
            for p in data.get("participants", []):
                if p.get("bot") == target or p.get("role", "").lower() == target.lower():
                    if p.get("thread_id"):
                        return p["thread_id"]
        except Exception:
            pass
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect would forward the token header to another host


class RelayClient:
    """Self-contained HTTP client for the loopback Desktop Relay (zero dependencies)."""
    def __init__(self, relay_dir):
        data = json.loads((Path(relay_dir) / "relay.json").read_text(encoding="utf-8"))
        self.url = validate_relay_url(data["url"])
        self.token = data["token"]

    def call(self, name, arguments, timeout=40):
        url = f"{self.url}/call"
        payload = json.dumps({"name": name, "arguments": arguments}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Office-Relay": self.token
            }
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(2000).decode("utf-8")).get("error", "")
            except (OSError, ValueError, AttributeError):
                detail = ""
            raise RuntimeError(f"HTTP Error {error.code}: {error.reason}" + (f" ({str(detail)[:200]})" if detail else "")) from None
        with resp as response:
            data = json.loads(response.read().decode("utf-8"))
            if "error" in data:
                raise RuntimeError(data["error"])
            return data.get("result", {})


class OutboxDB:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        # Opening the database never changes a row state: a 'sending' row may belong to a live pump.
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    sender_bot TEXT NOT NULL,
                    target_bot TEXT NOT NULL,
                    target_thread_id TEXT,
                    text TEXT NOT NULL,
                    dedupe_key TEXT UNIQUE,
                    state TEXT NOT NULL DEFAULT 'pending',
                    delivered_at TEXT,
                    error TEXT
                )
            """)
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(outbox)")}
            if "dedupe_key" not in columns:
                self.conn.execute("ALTER TABLE outbox ADD COLUMN dedupe_key TEXT")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_state ON outbox(state, created_at)")

    def enqueue(self, sender_bot, target_bot, target_thread_id, text, dedupe_key=None):
        """Queue a row; the same dedupe key returns the existing row, also when two writers race."""
        def existing():
            row = self.conn.execute("SELECT id FROM outbox WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
            return row[0] if row else None

        if dedupe_key and existing():
            return existing()
        msg_id = uuid.uuid4().hex
        try:
            with self.conn:
                self.conn.execute("""
                    INSERT INTO outbox (id, created_at, sender_bot, target_bot, target_thread_id, text, dedupe_key, state)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                """, (msg_id, now_iso(), sender_bot, target_bot, target_thread_id, text, dedupe_key))
        except sqlite3.IntegrityError:
            found = existing() if dedupe_key else None
            if found:
                return found
            raise
        return msg_id

    def mark_sending(self, msg_id, target_thread=None):
        with self.conn:
            self.conn.execute("UPDATE outbox SET state = 'sending', target_thread_id = ? WHERE id = ?", (target_thread, msg_id))

    def recover_sending(self):
        """Only the pump that holds the run lock calls this: a row left 'sending' belongs to a dead pump."""
        with self.conn:
            self.conn.execute("UPDATE outbox SET state = 'ambiguous', error = 'pump stopped during delivery' WHERE state = 'sending'")

    def held_by(self):
        """The oldest ambiguous row, which holds the whole queue until an operator resolves it, or None."""
        row = self.conn.execute("SELECT * FROM outbox WHERE state = 'ambiguous' ORDER BY rowid ASC LIMIT 1").fetchone()
        return dict(row) if row else None

    def expire_stale(self, ttl_hours):
        """Pending rows older than the TTL become 'expired' so a long outage cannot leave an unbounded, outdated queue."""
        cutoff = datetime.fromtimestamp(time.time() - ttl_hours * 3600, timezone.utc).isoformat()
        with self.conn:
            cur = self.conn.execute(
                "UPDATE outbox SET state = 'expired', error = ? WHERE state = 'pending' AND created_at < ?",
                (f"expired: not delivered within {ttl_hours:g} h", cutoff))
        return cur.rowcount

    def oldest_pending_age(self):
        row = self.conn.execute("SELECT MIN(created_at) FROM outbox WHERE state = 'pending'").fetchone()
        if not row or not row[0]:
            return None
        return max(0.0, time.time() - datetime.fromisoformat(row[0]).timestamp())

    def count_state(self, state):
        return self.conn.execute("SELECT COUNT(*) FROM outbox WHERE state = ?", (state,)).fetchone()[0]

    def pending_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM outbox WHERE state = 'pending'").fetchone()[0]

    def next_pending(self, exclude_targets=()):
        """The next pending row; an ambiguous earlier row may be overtaken. Rows for the excluded targets are
        skipped, so a busy recipient cannot hold up messages to others (order within one target is unchanged)."""
        marks = ",".join("?" * len(exclude_targets))
        row = self.conn.execute(
            "SELECT * FROM outbox WHERE state = 'pending'" + (f" AND target_bot NOT IN ({marks})" if marks else "") +
            " ORDER BY rowid ASC LIMIT 1", tuple(exclude_targets)).fetchone()
        return dict(row) if row else None

    def mark_delivered(self, msg_id):
        with self.conn:
            self.conn.execute("UPDATE outbox SET state = 'delivered', delivered_at = ?, error = NULL WHERE id = ?", (now_iso(), msg_id))

    def note(self, msg_id, text):
        with self.conn:
            self.conn.execute("UPDATE outbox SET error = ? WHERE id = ?", (str(text)[:500], msg_id))

    def mark_failed(self, msg_id, error):
        with self.conn:
            self.conn.execute("UPDATE outbox SET state = 'failed', error = ? WHERE id = ?", (str(error)[:500], msg_id))

    def mark_ambiguous(self, msg_id, error):
        with self.conn:
            self.conn.execute("UPDATE outbox SET state = 'ambiguous', error = ? WHERE id = ?", (str(error)[:500], msg_id))

    def resolve(self, msg_id, action, note=""):
        """Operator decision on an unknown or failed row. Returns the new state; raises ValueError when not allowed.

        delivered: the operator saw the report arrive. skipped: do not send it. retry: it did not arrive, send it again
        (the row keeps its place in the order). delivered and skipped need an ambiguous row; retry also takes a failed row.
        """
        if action not in RESOLUTIONS:
            raise ValueError("action must be one of: " + ", ".join(sorted(RESOLUTIONS)))
        allowed = ("ambiguous", "failed") if action == "retry" else ("ambiguous",)
        new_state = RESOLUTIONS[action]
        text = ("resolved by operator: " + action + (": " + note if note else ""))[:500]
        delivered_at = now_iso() if new_state == "delivered" else None
        with self.conn:
            cur = self.conn.execute(
                "UPDATE outbox SET state = ?, error = ?, delivered_at = COALESCE(?, delivered_at) "
                "WHERE id = ? AND state IN (%s)" % ",".join("?" * len(allowed)),
                (new_state, text, delivered_at, msg_id) + allowed)
        if cur.rowcount != 1:
            raise ValueError(f"no {' or '.join(allowed)} row with id {msg_id}")
        return new_state

    def list_recent(self, limit=20, state=None):
        sql = "SELECT id, created_at, sender_bot, target_bot, state, delivered_at, error FROM outbox"
        args = []
        if state:
            sql += " WHERE state = ?"
            args.append(state)
        rows = self.conn.execute(sql + " ORDER BY rowid DESC LIMIT ?", args + [limit]).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()


def relay_reachable(project_root, timeout=2.0):
    """True when a usable relay descriptor exists and its loopback port accepts a connection. Costs no model tokens."""
    relay_root, data = find_relay_descriptor(project_root)
    if not relay_root:
        return False
    parts = urllib.parse.urlsplit(data["url"])
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=timeout):
            return True
    except OSError:
        return False


def pump_alive(outbox_dir):
    """True when another process holds the pump lock."""
    Path(outbox_dir).mkdir(parents=True, exist_ok=True)
    lock = PumpLock(Path(outbox_dir) / "pump.lock")
    if lock.acquire():
        lock.release()
        return False
    return True


def write_heartbeat(outbox_dir, **fields):
    try:
        data = {"at": now_iso(), "pid": os.getpid(), **fields}
        tmp = Path(outbox_dir) / "pump.heartbeat.json.tmp"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, Path(outbox_dir) / "pump.heartbeat.json")
    except OSError:
        pass


def queue_status(project_root):
    """Read-only health of one project's outbox. 'problems' is empty when everything is fine."""
    outbox_dir = Path(project_root) / ".bot-mode" / "outbox"
    db_path = outbox_dir / "queue.sqlite"
    status = {"project": str(project_root), "pending": 0, "oldest_pending_seconds": None, "ambiguous": 0,
              "expired": 0, "relay_reachable": relay_reachable(project_root), "pump_alive": False, "problems": []}
    if not db_path.is_file():
        return status
    status["pump_alive"] = pump_alive(outbox_dir)
    db = OutboxDB(db_path)
    try:
        status["pending"] = db.pending_count()
        age = db.oldest_pending_age()
        status["oldest_pending_seconds"] = None if age is None else round(age)
        status["ambiguous"] = db.count_state("ambiguous")
        status["expired"] = db.count_state("expired")
    finally:
        db.close()
    waiting = status["pending"] > 0
    if status["ambiguous"]:
        status["problems"].append("unconfirmed delivery requires reconciliation; later reports can pass it: outbox.py list --state ambiguous")
    if waiting and not status["relay_reachable"]:
        status["problems"].append("work is waiting and the Desktop relay is not reachable; restart it from the Manager chat: powershell -File scripts/start_codex_office.ps1 -RelayOnly")
    if waiting and not status["pump_alive"]:
        status["problems"].append("work is waiting and no pump is running (outbox.py status --heal starts one)")
    if (waiting and age is not None and age > STALE_SECONDS and status["relay_reachable"] and status["pump_alive"]
            and not status["ambiguous"]):
        status["problems"].append(f"oldest pending row is {round(age)} s old although relay and pump are up; check the Manager thread")
    return status


def is_thread_busy(history):
    if not isinstance(history, dict):
        return True
    thread_info = history.get("thread")
    status = thread_info.get("status") if isinstance(thread_info, dict) else None
    status_type = status.get("type") if isinstance(status, dict) else None
    if not isinstance(status_type, str):
        return True  # an unreadable or non-text status never reaches the status checks below
    turns = history.get("turns", [])
    newest = turns[0] if isinstance(turns, list) and turns else None
    newest_status = newest.get("status") if isinstance(newest, dict) else None
    turn_in_prog = newest_status == "inProgress"
    if status_type == "notLoaded":
        # A chat that Desktop has not loaded has no running turn. It is deliverable only when its history is readable:
        # no turns at all, or a newest turn with a known finished status. Anything else fails closed (#415).
        if "turns" not in history or not isinstance(turns, list):
            return True
        return bool(turns) and not (isinstance(newest_status, str) and newest_status in ("completed", "interrupted", "failed"))
    if status_type not in {"active", "idle"}:
        return True
    return status_type == "active" or turn_in_prog


def turn_marker(history):
    """Identity of the newest turn of a read_thread answer: (id, startedAt)."""
    turns = history.get("turns") if isinstance(history, dict) else None
    first = turns[0] if isinstance(turns, list) and turns and isinstance(turns[0], dict) else None
    return (first.get("id"), first.get("startedAt")) if first else (None, None)


def turn_registered(history, before):
    """A new Manager turn is proven by an active thread or by a newest turn that differs from the one before the send."""
    if not isinstance(history, dict):
        return False
    if history.get("thread", {}).get("status", {}).get("type") == "active":
        return True
    return turn_marker(history) != before


def await_turn(client, thread, before, poll_interval, timeout):
    """Wait until the message sent into `thread` shows up as a registered turn. False when it did not in time."""
    deadline = time.time() + timeout
    while True:
        try:
            if turn_registered(client.call("read_thread", {"threadId": thread, "turnLimit": 1, "includeOutputs": False}), before):
                return True
        except Exception as e:
            print(f"Outbox pump: read_thread failed while waiting for the Manager turn ({e})", file=sys.stderr)
        if time.time() >= deadline:
            return False
        time.sleep(poll_interval)


class PumpLock:
    """Single pump per project: an operating system lock on pump.lock, released by the system when the pump dies.

    There is no stale lock to take over and no process id to reuse. The file itself stays in place.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        handle = open(self.path, "a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


def held_message(row):
    return (f"Outbox pump: unconfirmed item {row['id']} ({row.get('error') or 'unknown outcome'}); later reports can overtake it. "
            f"Check the target thread, then run: outbox.py resolve {row['id']} --as delivered|skipped|retry")


def delivery_marker(msg_id):
    return f"[Outbox delivery: {msg_id}]"


def reconcile_ambiguous(db, client):
    """Only positive evidence in a recipient's user message proves delivery; absence never proves non-delivery."""
    rows = db.conn.execute("SELECT id, target_thread_id FROM outbox WHERE state = 'ambiguous'").fetchall()
    for row in rows:
        if not row['target_thread_id']:
            continue  # legacy rows cannot be bound safely to an actual destination
        try:
            cursor = None
            for _ in range(5):
                args = {'threadId': row['target_thread_id'], 'turnLimit': 50, 'includeOutputs': False}
                if cursor:
                    args['cursor'] = cursor
                page = client.call('read_thread', args)
                found = any(
                    item.get('type') == 'userMessage' and any(
                        part.get('type') == 'text' and part.get('text', '').startswith(delivery_marker(row['id']) + '\n')
                        for part in item.get('content', []) if isinstance(part, dict))
                    for turn in page.get('turns', []) for item in turn.get('items', []) if isinstance(item, dict))
                if found:
                    db.mark_delivered(row['id'])
                    break
                cursor = page.get('page', {}).get('nextCursor')
                if not cursor:
                    break
        except Exception as error:
            print(f"Outbox reconciliation unavailable for {row['id']}: {error}", file=sys.stderr)


def pump_outbox(project_root, max_wait_sec=600, poll_interval=3.0, turn_timeout=120.0, ttl_hours=DEFAULT_TTL_HOURS):
    """Deliver pending rows in order: one at a time, only to an idle thread, and only after the previous message
    produced a registered turn that has finished. Returns 0 when it ended normally (it waits for a missing relay),
    EXIT_NOT_REGISTERED when a delivery showed no Manager turn."""
    outbox_dir = Path(project_root) / ".bot-mode" / "outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    lock = PumpLock(outbox_dir / "pump.lock")
    if not lock.acquire():
        return 0  # another pump is running

    try:
        db = OutboxDB(outbox_dir / "queue.sqlite")
        try:
            db.recover_sending()
            client = None
            reconciled = False
            resolved = {}
            busy_until = {}  # target -> time until which it is not asked again; the queue moves on to other targets
            relay_wait = poll_interval
            warned = False
            last_beat = 0.0
            start_time = time.time()
            while time.time() - start_time < max_wait_sec:
                db.expire_stale(ttl_hours)
                now = time.time()
                busy_until = {key: until for key, until in busy_until.items() if until > now}
                any_pending = db.next_pending()
                item = db.next_pending(busy_until) if busy_until else any_pending
                if not any_pending and (reconciled or not db.held_by()):
                    break  # Nothing pending

                if time.time() - last_beat >= HEARTBEAT_EVERY:
                    last_beat = time.time()
                    write_heartbeat(outbox_dir, pending=db.pending_count(), relay=client is not None)

                # A restarted relay has a new port and token: (re)read the descriptor whenever there is no working client.
                if client is None:
                    relay_root, _desc = find_relay_descriptor(project_root)
                    try:
                        client = RelayClient(relay_root) if relay_root else None
                    except (ValueError, OSError, KeyError) as e:
                        print(f"Outbox pump: relay descriptor refused ({e}).", file=sys.stderr)
                        client = None
                    if client is None:
                        if not warned:
                            print("Outbox pump: no usable loopback Desktop relay descriptor; waiting for the relay.", file=sys.stderr)
                            warned = True
                        time.sleep(relay_wait)
                        relay_wait = min(relay_wait * 2, RELAY_WAIT_MAX)
                        continue
                    warned = False
                    relay_wait = poll_interval

                if not reconciled:
                    reconcile_ambiguous(db, client)
                    reconciled = True
                if not item:
                    if any_pending:
                        time.sleep(poll_interval)  # every pending recipient is busy: ask again shortly
                        continue
                    break

                # Resolve logical roles at delivery time, including legacy rows with a cached ID.
                # Explicit thread IDs in target_bot remain fixed targets.
                # The busy check may use a thread resolved up to 30 min ago (no helper process every poll); the send never does.
                key = item["target_bot"]
                if key not in resolved or time.time() - resolved[key][1] > RESOLVE_CACHE_SECONDS:
                    resolved[key] = (resolve_target_thread(project_root, key), time.time())
                target_thread = resolved[key][0]
                if not target_thread:
                    db.mark_failed(item["id"], f"Could not resolve target thread for {item['target_bot']}")
                    continue

                # Query thread status
                try:
                    history = client.call("read_thread", {"threadId": target_thread, "turnLimit": 1, "includeOutputs": False})
                except Exception as e:
                    print(f"Outbox pump: read_thread failed ({e}); backing off...", file=sys.stderr)
                    client = None  # the relay may have restarted: read its descriptor again
                    time.sleep(relay_wait)
                    relay_wait = min(relay_wait * 2, RELAY_WAIT_MAX)
                    continue
                relay_wait = poll_interval

                if is_thread_busy(history):
                    # Destination is busy: set it aside for a moment and deliver to the others meanwhile.
                    busy_until[key] = time.time() + poll_interval
                    continue

                # Thread is idle: resolve the target afresh (a replaced Manager must win), then deliver.
                fresh = resolve_target_thread(project_root, item["target_bot"])
                if fresh != target_thread:
                    resolved[item["target_bot"]] = (fresh, time.time())
                    continue
                before = turn_marker(history)
                try:
                    db.mark_sending(item["id"], target_thread)
                    client.call("send_message_to_thread", {
                        "threadId": target_thread,
                        "prompt": delivery_marker(item['id']) + '\n' + item["text"]
                    })
                except Exception as e:
                    # The relay may or may not have accepted it: never sent again by the pump, and it holds the queue.
                    print(f"Outbox pump: send_message_to_thread failed ({e})", file=sys.stderr)
                    db.mark_ambiguous(item["id"], e)
                    print(f"Outbox pump: {item['id']} quarantined; later reports may arrive out of order.", file=sys.stderr)
                    client = None
                    reconciled = False
                    continue
                db.mark_delivered(item["id"])
                print(f"Delivered {item['id']} to {item['target_bot']} ({target_thread}).")
                if not await_turn(client, target_thread, before, poll_interval, turn_timeout):
                    db.note(item["id"], f"delivered; no new Manager turn seen within {turn_timeout:g}s")
                    print(f"Outbox pump: no new Manager turn seen within {turn_timeout:g}s after {item['id']}; stopping so the next "
                          "report is not sent into an unconfirmed turn. Check the Manager thread, then run the pump again.", file=sys.stderr)
                    return EXIT_NOT_REGISTERED
                # The next loop pass waits for that turn to finish (thread idle) before the next row.
        finally:
            db.close()
    finally:
        lock.release()
    return 0


def spawn_background_pump(project_root):
    """Launches detached pump process on Windows without visible console."""
    script_path = str(Path(__file__).resolve())
    py_exec = sys.executable
    cmd = [py_exec, "-B", script_path, "--project", str(project_root), "pump", "--max-wait", "86400"]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)

    log = Path(project_root) / ".bot-mode" / "outbox" / "pump.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = log.open("a", encoding="utf-8")
        subprocess.Popen(cmd, creationflags=creationflags, close_fds=True,
                         stdout=stream, stderr=subprocess.STDOUT)
        stream.close()
    except Exception as e:
        print(f"Warning: Failed to spawn background pump ({e})", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Zero-token asynchronous outbox for agent handoffs.")
    parser.add_argument("--project", default=".", help="Project root directory")
    sub = parser.add_subparsers(dest="action", required=True)

    # send / enqueue
    s = sub.add_parser("send", help="Enqueue a message and exit immediately (0-token wait).")
    s.add_argument("--to", default="manager", help="Target bot or role (default: manager)")
    s.add_argument("--by", default="", help="Sender bot ID")
    s.add_argument("--dedupe-key", default="", help="Stable key preventing duplicate enqueue")
    group = s.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="Message text")
    group.add_argument("--body-file", help="File containing message text")

    # pump
    p = sub.add_parser("pump", help="Run the outbox delivery pump.")
    p.add_argument("--max-wait", type=int, default=600, help="Max wait duration in seconds")
    p.add_argument("--turn-timeout", type=float, default=120.0,
                   help="Seconds to wait for a delivered message to show up as a Manager turn before stopping")

    p.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS,
                   help="Pending rows older than this become 'expired' (default 12)")

    # status
    st = sub.add_parser("status", help="Health of the outbox, relay and pump (exit 5 when something needs attention).")
    st.add_argument("--heal", action="store_true", help="Start the pump when work is waiting and no pump runs")

    # list
    l = sub.add_parser("list", help="List recent outbox rows.")
    l.add_argument("--state", choices=["pending", "sending", "delivered", "failed", "ambiguous", "skipped", "expired"],
                   help="Only rows in this state")

    # resolve
    r = sub.add_parser("resolve", help="Operator decision on an ambiguous row (or retry of a failed row).")
    r.add_argument("id")
    r.add_argument("--as", dest="resolution", required=True, choices=sorted(RESOLUTIONS),
                   help="delivered: it arrived; skipped: never send it; retry: it did not arrive, send it again in its place")
    r.add_argument("--note", default="", help="Short reason kept on the row")

    args = parser.parse_args(argv)
    project_root = Path(args.project).resolve()

    if args.action == "send":
        text = args.text
        if getattr(args, "body_file", None):
            text = Path(args.body_file).read_text(encoding="utf-8")

        sender = args.by or os.environ.get("CODEX_BOT_ID") or "agent"
        target_thread = resolve_target_thread(project_root, args.to)

        outbox_dir = project_root / ".bot-mode" / "outbox"
        db = OutboxDB(outbox_dir / "queue.sqlite")
        try:
            msg_id = db.enqueue(sender, args.to, target_thread, text, args.dedupe_key or None)
            held = db.held_by()
            depth = db.pending_count()
            age = db.oldest_pending_age()
        finally:
            db.close()

        print(f"Queued outbox handoff {msg_id} for {args.to}. Turn complete.")
        print(f"Queue: {depth} pending, oldest {0 if age is None else round(age)} s.")
        if not relay_reachable(project_root):
            print("WARNING: the Desktop relay is not reachable, so this report is NOT delivered yet. "
                  "Also write your result directly in the Manager thread with the thread tool.", file=sys.stderr)
        if held:
            print(held_message(held), file=sys.stderr)
        spawn_background_pump(project_root)
        return 0

    elif args.action == "pump":
        return pump_outbox(project_root, max_wait_sec=args.max_wait, turn_timeout=args.turn_timeout, ttl_hours=args.ttl_hours)

    elif args.action == "status":
        status = queue_status(project_root)
        if args.heal and (status["pending"] or status["ambiguous"]) and not status["pump_alive"]:
            spawn_background_pump(project_root)
            status["healed"] = "pump started"
            status["problems"] = [x for x in status["problems"] if "no pump is running" not in x]
        print(json.dumps(status, ensure_ascii=False))
        return EXIT_UNHEALTHY if status["problems"] else 0

    elif args.action == "list":
        outbox_dir = project_root / ".bot-mode" / "outbox"
        db_path = outbox_dir / "queue.sqlite"
        if not db_path.is_file():
            print("No outbox queue initialized.")
            return 0
        db = OutboxDB(db_path)
        try:
            items = db.list_recent(state=args.state)
            print(json.dumps(items, indent=2, ensure_ascii=False))
        finally:
            db.close()
        return 0

    elif args.action == "resolve":
        db_path = project_root / ".bot-mode" / "outbox" / "queue.sqlite"
        if not db_path.is_file():
            print("refused: no outbox queue initialized.", file=sys.stderr)
            return 2
        db = OutboxDB(db_path)
        try:
            try:
                state = db.resolve(args.id, args.resolution, args.note)
            except ValueError as error:
                print(f"refused: {error}", file=sys.stderr)
                return 2
            more = db.pending_count()
        finally:
            db.close()
        print(f"Resolved {args.id}: {state}.")
        if more:
            spawn_background_pump(project_root)
        return 0


def install_cli(project, relay_root=None):
    """Install this file as <project>/.bot-mode/outbox.py and, when missing, the relay config next to it (#415)."""
    target = Path(project) / ".bot-mode" / "outbox.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(Path(__file__).read_bytes())
    config = target.with_name("outbox-config.json")
    if relay_root and not config.exists():
        config.write_text(json.dumps({"relay_root": Path(relay_root).as_posix()}) + "\n", encoding="utf-8")
    return target


if __name__ == "__main__":
    sys.exit(main())
