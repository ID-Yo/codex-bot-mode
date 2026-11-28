"""Project-level persistent outbox queue with a zero-token background pump (#399).

Agents queue a short report with `send` and end their turn. A deterministic local pump (no model calls) delivers the
queued reports to the target Manager thread one at a time, in order, only while that thread is idle, and only after the
previous delivery has produced a registered Manager turn that has finished.

States of a queue row: pending, sending, delivered, failed, ambiguous, needs-attention, skipped, expired.
Rules the pump keeps:
- A send whose outcome is unknown (error or timeout after the relay was called, or a pump that stopped mid send) becomes
  `ambiguous`. The pump checks the actual recipient thread for the unique marker again and again: a matching user
  message proves delivery (delivered). Only a complete thread history without the marker, read after a grace period, proves
  that it did not arrive (the row goes back to pending and is sent again, #422). Without proof it is never resent. While it
  is unresolved it holds back later rows for the same recipient only (order is kept); other recipients are not affected.
- An ambiguous row still unresolved after the attention time (default 30 min) becomes `needs-attention`: status and list
  show it, it no longer holds anything back, and a later proof still turns it into delivered. An operator may resolve it:
  `outbox.py resolve <id> --as delivered|skipped|retry`.
- Several rows waiting for the same recipient are delivered as one digest message (one Manager turn); each row keeps its
  own delivered state.
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
import re
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
DEFAULT_ATTENTION_MINUTES = 30.0  # an unresolved ambiguous row becomes needs-attention after this long
DEFAULT_RETRY_GRACE_MINUTES = 10.0  # a complete history without the marker proves non-delivery only after this long (0: never)
DEFAULT_DIGEST_MAX = 20  # most rows in one digest message (1: no digest)
DIGEST_MAX_CHARS = 24000  # a digest stops growing past this many characters of report text
RECONCILE_EVERY = 30.0  # seconds between checks of the thread history while rows are unresolved
RECONCILE_TURNS = 10  # Desktop read_thread accepts at most 10 turns per call (#429)
RECONCILE_PAGES = 5  # history pages read per thread and check: about 50 turns in total
DEFAULT_KEEP_ROWS = 200  # delivered (and skipped, expired) rows kept by prune
DEFAULT_MAX_BACKUPS = 5
UNSURE_STATES = ("ambiguous", "needs-attention")
DEFAULT_RECHECKS = 3  # rereads of unresolved rows by a pump that has nothing else to deliver
STALE_SECONDS = 600  # a pending row older than this although relay and pump are up is a problem
RELAY_WAIT_MAX = 30.0  # longest pause between relay descriptor checks while the relay is down
HEARTBEAT_EVERY = 10.0
RESOLVE_CACHE_SECONDS = 1800  # the Manager thread rarely changes; the send re-resolves it anyway
CANARY_KEEP = 20  # canary records kept in the queue file
DEFAULT_CANARY_EVERY_MINUTES = 10.0  # a new canary is queued when the last one is older than this
DEFAULT_CANARY_LIMIT_MINUTES = 5.0  # a canary that waited longer than this without being processed is a problem


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_config(project_root):
    """Optional numeric settings from .bot-mode/outbox-config.json (attention_minutes, retry_grace_minutes, digest_max,
    keep_rows, max_backups). Anything unreadable or negative is ignored."""
    try:
        data = json.loads((Path(project_root) / ".bot-mode" / "outbox-config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def setting(argument, config, key, default):
    """Command line value first, then the config file, then the default."""
    if argument is not None:
        return argument
    value = config.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return value
    return default


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
                    error TEXT,
                    ambiguous_at TEXT
                )
            """)
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(outbox)")}
            if "dedupe_key" not in columns:
                self.conn.execute("ALTER TABLE outbox ADD COLUMN dedupe_key TEXT")
            if "ambiguous_at" not in columns:
                self.conn.execute("ALTER TABLE outbox ADD COLUMN ambiguous_at TEXT")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_state ON outbox(state, created_at)")
            # The canary (#426) is not an outbox row: it is never a report, never in a digest and never in the counts of the queue.
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS canary (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    target TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    done_at TEXT,
                    latency REAL,
                    read_seconds REAL,
                    chat_status TEXT,
                    error TEXT
                )
            """)

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

    def canary_start(self, target, every_seconds):
        """Queue a canary for target unless one is pending or the newest one is younger than every_seconds. Returns its id or None."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")  # the check and the insert are one step: two callers at once queue one canary
            if self.conn.execute("SELECT 1 FROM canary WHERE state = 'pending' LIMIT 1").fetchone():
                return None
            newest = self.conn.execute("SELECT MAX(created_at) FROM canary").fetchone()[0]
            if newest and time.time() - datetime.fromisoformat(newest).timestamp() < every_seconds:
                return None
            canary_id = uuid.uuid4().hex
            self.conn.execute("INSERT INTO canary (id, created_at, target) VALUES (?, ?, ?)", (canary_id, now_iso(), target))
            self.conn.execute("DELETE FROM canary WHERE state != 'pending' AND id NOT IN "
                              "(SELECT id FROM canary ORDER BY created_at DESC, rowid DESC LIMIT ?)", (CANARY_KEEP,))
        return canary_id

    def canary_pending(self):
        row = self.conn.execute("SELECT * FROM canary WHERE state = 'pending' ORDER BY created_at ASC, rowid ASC LIMIT 1").fetchone()
        return dict(row) if row else None

    def canary_last_done(self):
        row = self.conn.execute("SELECT * FROM canary WHERE state = 'done' ORDER BY done_at DESC, rowid DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def canary_done(self, canary_id, latency, read_seconds, chat_status):
        with self.conn:
            self.conn.execute("UPDATE canary SET state = 'done', done_at = ?, latency = ?, read_seconds = ?, chat_status = ?, error = NULL "
                              "WHERE id = ?", (now_iso(), latency, read_seconds, chat_status, canary_id))

    def canary_error(self, canary_id, error):
        with self.conn:
            self.conn.execute("UPDATE canary SET error = ? WHERE id = ?", (str(error)[:300], canary_id))

    def mark_sending(self, msg_id, target_thread=None):
        self.mark_sending_many([msg_id], target_thread)

    def mark_sending_many(self, ids, target_thread=None):
        """One transaction for a whole digest, so a crash never leaves part of it pending."""
        with self.conn:
            self.conn.executemany("UPDATE outbox SET state = 'sending', target_thread_id = ? WHERE id = ?",
                                  [(target_thread, msg_id) for msg_id in ids])

    def recover_sending(self):
        """Only the pump that holds the run lock calls this: a row left 'sending' belongs to a dead pump."""
        with self.conn:
            self.conn.execute("UPDATE outbox SET state = 'ambiguous', error = 'pump stopped during delivery', ambiguous_at = ? "
                              "WHERE state = 'sending'", (now_iso(),))

    def held_by(self):
        """The oldest ambiguous row, which holds later rows for its recipient until it is resolved, or None."""
        row = self.conn.execute("SELECT * FROM outbox WHERE state = 'ambiguous' ORDER BY rowid ASC LIMIT 1").fetchone()
        return dict(row) if row else None

    def escalate_ambiguous(self, attention_minutes):
        """Ambiguous rows unresolved for attention_minutes become 'needs-attention' and stop holding later rows (#422).
        The clock starts when the row became ambiguous; a row from an older copy without that time starts now."""
        with self.conn:
            self.conn.execute("UPDATE outbox SET ambiguous_at = ? WHERE state = 'ambiguous' AND ambiguous_at IS NULL", (now_iso(),))
            cutoff = datetime.fromtimestamp(time.time() - attention_minutes * 60, timezone.utc).isoformat()
            cur = self.conn.execute(
                "UPDATE outbox SET state = 'needs-attention', error = ? || COALESCE(error, '') "
                "WHERE state = 'ambiguous' AND ambiguous_at <= ?",
                (f"needs attention: unresolved for {attention_minutes:g} min; ", cutoff))
        return cur.rowcount

    def recheckable_count(self, hours):
        """Unresolved rows (ambiguous or needs-attention) younger than the TTL: a late marker can still prove them.
        Older ones are left to the operator, so a row that never resolves cannot cause rereads for ever."""
        cutoff = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).isoformat()
        return self.conn.execute("SELECT COUNT(*) FROM outbox WHERE state IN ('ambiguous', 'needs-attention') AND created_at >= ?",
                                 (cutoff,)).fetchone()[0]

    def auto_retry(self, msg_id, minutes):
        """Proof of non-delivery was found: the row goes back to pending at its place. Only an ambiguous row."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE outbox SET state = 'pending', ambiguous_at = NULL, error = ? WHERE id = ? AND state = 'ambiguous'",
                (f"auto-retry: the complete thread history has no delivery marker {minutes:g} min after the unconfirmed send", msg_id))
        return cur.rowcount == 1

    def next_batch(self, exclude_targets=(), max_rows=1, max_chars=DIGEST_MAX_CHARS):
        """The next rows to deliver as one message: the oldest deliverable row and the later pending rows of the same
        recipient, in creation order. A row is not deliverable while an earlier ambiguous row exists for the same
        recipient (same target name or same thread); other recipients are never held. Rows for excluded targets are
        skipped. At least one row is returned when any is deliverable; the digest stops at max_rows or max_chars of text."""
        marks = ",".join("?" * len(exclude_targets))
        rows = self.conn.execute(
            "SELECT * FROM outbox o WHERE o.state = 'pending'" + (f" AND o.target_bot NOT IN ({marks})" if marks else "") +
            " AND NOT EXISTS (SELECT 1 FROM outbox a WHERE a.state = 'ambiguous' AND a.rowid < o.rowid AND "
            "(a.target_bot = o.target_bot OR (a.target_thread_id IS NOT NULL AND a.target_thread_id = o.target_thread_id)))"
            " ORDER BY o.rowid ASC", tuple(exclude_targets)).fetchall()
        batch, chars = [], 0
        for row in rows:
            row = dict(row)
            if batch and row["target_bot"] != batch[0]["target_bot"]:
                continue
            if batch and (len(batch) >= max_rows or chars + len(row["text"]) > max_chars):
                break
            batch.append(row)
            chars += len(row["text"])
        return batch

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

    def last_delivered_at(self):
        """ISO time of the newest delivered row, or None when nothing was delivered yet (#424)."""
        row = self.conn.execute("SELECT MAX(delivered_at) FROM outbox WHERE state = 'delivered'").fetchone()
        return row[0] if row and row[0] else None

    def last_delivered_age(self):
        stamp = self.last_delivered_at()
        if not stamp:
            return None
        try:
            return max(0.0, time.time() - datetime.fromisoformat(stamp).timestamp())
        except ValueError:
            return None

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
        self.mark_delivered_many([msg_id])

    def mark_delivered_many(self, ids):
        with self.conn:
            self.conn.executemany("UPDATE outbox SET state = 'delivered', delivered_at = ?, error = NULL WHERE id = ?",
                                  [(now_iso(), msg_id) for msg_id in ids])

    def note(self, msg_id, text):
        with self.conn:
            self.conn.execute("UPDATE outbox SET error = ? WHERE id = ?", (str(text)[:500], msg_id))

    def mark_failed(self, msg_id, error):
        with self.conn:
            self.conn.execute("UPDATE outbox SET state = 'failed', error = ? WHERE id = ?", (str(error)[:500], msg_id))

    def mark_unsent_many(self, ids, note):
        """Rows that were marked 'sending' go back to pending, in their place, because Desktop refused the message outright (#426).
        Only rows still in 'sending' change."""
        with self.conn:
            self.conn.executemany("UPDATE outbox SET state = 'pending', error = ? WHERE id = ? AND state = 'sending'",
                                  [(str(note)[:500], msg_id) for msg_id in ids])

    def mark_ambiguous(self, msg_id, error):
        self.mark_ambiguous_many([msg_id], error)

    def mark_ambiguous_many(self, ids, error):
        with self.conn:
            self.conn.executemany("UPDATE outbox SET state = 'ambiguous', error = ?, ambiguous_at = ? WHERE id = ?",
                                  [(str(error)[:500], now_iso(), msg_id) for msg_id in ids])

    def resolve(self, msg_id, action, note=""):
        """Operator decision on an unknown or failed row. Returns the new state; raises ValueError when not allowed.

        delivered: the operator saw the report arrive. skipped: do not send it. retry: it did not arrive, send it again
        (the row keeps its place in the order). delivered and skipped need an ambiguous or needs-attention row; retry also
        takes a failed row.
        """
        if action not in RESOLUTIONS:
            raise ValueError("action must be one of: " + ", ".join(sorted(RESOLUTIONS)))
        allowed = UNSURE_STATES + (("failed",) if action == "retry" else ())
        new_state = RESOLUTIONS[action]
        text = ("resolved by operator: " + action + (": " + note if note else ""))[:500]
        delivered_at = now_iso() if new_state == "delivered" else None
        with self.conn:
            cur = self.conn.execute(
                "UPDATE outbox SET state = ?, error = ?, ambiguous_at = NULL, delivered_at = COALESCE(?, delivered_at) "
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


def backup_database(db, backup_dir, max_backups=DEFAULT_MAX_BACKUPS):
    """Consistent copy of the queue database (sqlite online backup) into backup_dir; only the newest max_backups stay."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / ("queue-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite")
    copy = sqlite3.connect(str(target))
    try:
        db.conn.backup(copy)
    finally:
        copy.close()
    old = sorted(backup_dir.glob("queue-*.sqlite"), key=lambda p: p.name)[:-max(1, int(max_backups))]
    for path in old:
        try:
            path.unlink()
        except OSError:
            pass
    return target


def prune_outbox(project_root, keep_rows=DEFAULT_KEEP_ROWS, ttl_hours=DEFAULT_TTL_HOURS, max_backups=DEFAULT_MAX_BACKUPS,
                 dry_run=False):
    """Bound the queue file (#422). Expires pending rows older than the TTL and deletes all but the newest keep_rows
    delivered rows (and the same for skipped and expired rows). Pending, sending, ambiguous, needs-attention and failed
    rows are never deleted. The database is copied to .bot-mode/outbox/backups first, whenever anything would change.
    A deleted row frees its dedupe key. Returns a summary dict."""
    outbox_dir = Path(project_root) / ".bot-mode" / "outbox"
    db_path = outbox_dir / "queue.sqlite"
    summary = {"expired": 0, "deleted": 0, "backup": None, "dry_run": bool(dry_run)}
    if not db_path.is_file():
        return summary
    keep_rows = max(0, int(keep_rows))
    db = OutboxDB(db_path)
    try:
        cutoff = datetime.fromtimestamp(time.time() - ttl_hours * 3600, timezone.utc).isoformat()
        summary["expired"] = db.conn.execute("SELECT COUNT(*) FROM outbox WHERE state = 'pending' AND created_at < ?", (cutoff,)).fetchone()[0]
        doomed = []
        for state in ("delivered", "skipped", "expired"):
            doomed += [r[0] for r in db.conn.execute(
                "SELECT id FROM outbox WHERE state = ? ORDER BY COALESCE(delivered_at, created_at) DESC, rowid DESC LIMIT -1 OFFSET ?",
                (state, keep_rows))]
        summary["deleted"] = len(doomed)
        if dry_run or not (summary["expired"] or doomed):
            return summary
        summary["backup"] = str(backup_database(db, outbox_dir / "backups", max_backups))
        summary["expired"] = db.expire_stale(ttl_hours)
        with db.conn:
            db.conn.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in doomed])
        if doomed:
            try:
                db.conn.execute("VACUUM")
            except sqlite3.Error:
                pass  # the file keeps its size until the next vacuum; the rows are gone
        return summary
    finally:
        db.close()


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
    """Health of one project's outbox ('problems' is empty when everything is fine). It only changes rows to promote an
    ambiguous row that is past the attention time to needs-attention."""
    outbox_dir = Path(project_root) / ".bot-mode" / "outbox"
    db_path = outbox_dir / "queue.sqlite"
    status = {"project": str(project_root), "pending": 0, "oldest_pending_seconds": None, "ambiguous": 0,
              "needs_attention": 0, "recheckable": 0, "expired": 0, "last_delivered_at": None,
              "relay_reachable": relay_reachable(project_root), "pump_alive": False, "canary": None, "route_refused": [], "problems": []}
    if not db_path.is_file():
        return status
    status["pump_alive"] = pump_alive(outbox_dir)
    db = OutboxDB(db_path)
    try:
        db.escalate_ambiguous(setting(None, load_config(project_root), "attention_minutes", DEFAULT_ATTENTION_MINUTES))
        status["pending"] = db.pending_count()
        age = db.oldest_pending_age()
        status["oldest_pending_seconds"] = None if age is None else round(age)
        status["ambiguous"] = db.count_state("ambiguous")
        status["needs_attention"] = db.count_state("needs-attention")
        status["recheckable"] = db.recheckable_count(DEFAULT_TTL_HOURS)
        status["expired"] = db.count_state("expired")
        status["last_delivered_at"] = db.last_delivered_at()
        status["canary"] = canary_report(db)  # information only: the canary command raises its own problem
    finally:
        db.close()
    status["route_refused"] = [{"recipient": name, **entry} for name, entry in sorted(read_route_status(project_root).items())]
    for entry in status["route_refused"]:
        status["problems"].append(route_problem_text(entry["recipient"], entry.get("cause")))
    waiting = status["pending"] > 0
    if status["ambiguous"]:
        status["problems"].append("unconfirmed delivery is being reconciled; later reports for the same recipient wait for it, "
                                  "other recipients are not held: outbox.py list --state ambiguous")
    if status["needs_attention"]:
        status["problems"].append(f"{status['needs_attention']} report(s) could not be confirmed and need a decision "
                                  "(they no longer hold anything): outbox.py list --state needs-attention, then resolve <id> --as delivered|skipped|retry")
    if waiting and not status["relay_reachable"]:
        status["problems"].append("work is waiting and the Desktop relay is not reachable; restart it from the Manager chat: powershell -File scripts/start_codex_office.ps1 -RelayOnly")
    if waiting and not status["pump_alive"]:
        status["problems"].append("work is waiting and no pump is running (outbox.py status --heal starts one)")
    if (waiting and age is not None and age > STALE_SECONDS and status["relay_reachable"] and status["pump_alive"]
            and not status["ambiguous"]):
        status["problems"].append(f"oldest pending row is {round(age)} s old although relay and pump are up; check the Manager thread")
    return status


def human_age(seconds):
    """'45 s', '12 min', '3 h 05 min' or '2 d 4 h'; None means no such event yet."""
    if seconds is None:
        return None
    seconds = int(round(seconds))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"
    return f"{seconds // 86400} d {seconds % 86400 // 3600} h"


CANARY_CHAT_STATUSES = ("active", "idle", "notLoaded")
CANARY_TURN_STATUSES = ("inProgress", "completed", "interrupted", "failed")


def canary_chat_status(history):
    """(chat status, None) when the read_thread answer is one the pump understands, else (status text or None, why not).
    Understood: a text status of active or idle, or notLoaded with readable turns (none, or a newest turn with a known status)."""
    info = history.get("thread") if isinstance(history, dict) else None
    status = info.get("status") if isinstance(info, dict) else None
    kind = status.get("type") if isinstance(status, dict) else None
    if not isinstance(kind, str):
        return None, "no text status"
    if kind not in CANARY_CHAT_STATUSES:
        return kind, f"the status {kind!r}, which is not active, idle or notLoaded"
    if kind == "notLoaded":
        turns = history.get("turns")
        newest = turns[0] if isinstance(turns, list) and turns else None
        newest_status = newest.get("status") if isinstance(newest, dict) else None
        if not isinstance(turns, list):
            return kind, "the status 'notLoaded' and no readable turns"
        if turns and newest_status not in CANARY_TURN_STATUSES:
            return kind, f"the status 'notLoaded' and a newest turn with the unknown status {newest_status!r}"
    return kind, None


def probe_canary(db, client, project_root, canary):
    """Process one canary without a model turn (#426): resolve the recipient chat, read its status with the same read-only call
    the pump uses before every delivery, and record the time from queueing to now. Nothing is sent. It proves the queue, the
    pump, the relay descriptor and token, the roster and Desktop's answer; it does not prove that Desktop accepts a message.
    Returns None when the canary is done, else the failure (also recorded on the canary)."""
    thread = resolve_target_thread(project_root, canary["target"])
    if not thread:
        failure = RuntimeError(f"cannot resolve the {canary['target']} chat from the roster")
        db.canary_error(canary["id"], failure)
        return failure
    started = time.time()
    try:
        history = client.call("read_thread", {"threadId": thread, "turnLimit": 1, "includeOutputs": False})
    except Exception as error:
        refusal = route_refusal_kind(error)
        if refusal:
            note_route_refused(project_root, canary["target"], refusal)
        db.canary_error(canary["id"], error)
        return error
    clear_route_refused(project_root, canary["target"])
    read_seconds = time.time() - started
    chat_status, reason = canary_chat_status(history)
    if reason:
        # An answer the pump would not deliver into is not a success: the canary stays pending, is tried again and becomes a
        # problem after the limit, with the status in the text. Unknown never becomes good.
        failure = RuntimeError(f"the {canary['target']} chat answered with {reason}")
        db.canary_error(canary["id"], failure)
        return failure
    try:
        latency = max(0.0, time.time() - datetime.fromisoformat(canary["created_at"]).timestamp())
    except ValueError:
        latency = read_seconds
    db.canary_done(canary["id"], round(latency, 3), round(read_seconds, 3), chat_status)
    return None


def canary_report(db, limit_minutes=DEFAULT_CANARY_LIMIT_MINUTES):
    """What the canary shows: the newest finished one (latency, time, chat status) and, when one waits longer than
    limit_minutes without being processed, a problem with the last error. Unknown stays unknown: no canary yet is null."""
    pending, last = db.canary_pending(), db.canary_last_done()
    report = {"pending_seconds": None, "last_ok_at": None, "last_latency_seconds": None, "last_read_seconds": None,
              "last_chat_status": None, "last_error": None, "problems": []}
    if last:
        report.update(last_ok_at=last["done_at"], last_latency_seconds=last["latency"], last_read_seconds=last["read_seconds"],
                      last_chat_status=last["chat_status"])
    if pending:
        try:
            report["pending_seconds"] = round(max(0.0, time.time() - datetime.fromisoformat(pending["created_at"]).timestamp()))
        except ValueError:
            report["pending_seconds"] = None
        report["last_error"] = pending["error"]
        if report["pending_seconds"] is not None and report["pending_seconds"] > limit_minutes * 60:
            report["problems"].append(f"outbox canary has waited {human_age(report['pending_seconds'])} without being processed"
                                      + (f" (last error: {pending['error']})" if pending["error"] else "")
                                      + ": check the relay, the pump and the Manager chat (outbox.py status)")
    return report


SEND_FALLBACKS = ("Fallbacks, so the Manager still gets your result: (1) write it directly in the Manager thread with the thread tool; "
                  "(2) keep the closed request in the ledger (requests.py reply); (3) comment on the linked issue.")


def send_feedback(depth, oldest, last_delivery, relay_up, pump_running, held=None, refused=None):
    """What `send` tells the sender after queueing: (lines for stdout, lines for stderr). The first two stdout lines keep
    their original wording because other tools read them (doctor reads the receipt)."""
    out = [f"Queue: {depth} pending, oldest {0 if oldest is None else round(oldest)} s.",
           "Relay: {}. Pump: {}. Last delivery: {}.".format(
               "up" if relay_up else "DOWN", "running" if pump_running else "started now",
               (human_age(last_delivery) + " ago") if last_delivery is not None else "none recorded")]
    err = []
    if not relay_up:
        err.append("WARNING: the Desktop relay is not reachable, so this report is NOT delivered yet. It stays queued and is sent "
                   "when the relay is back (the supervisor restarts it; outbox.py status shows the state).")
        err.append(SEND_FALLBACKS)
    elif oldest is not None and oldest > STALE_SECONDS:
        err.append(f"WARNING: the oldest report has waited {human_age(oldest)} although the relay is up; check the Manager thread. "
                   + SEND_FALLBACKS)
    if held:
        err.append(held_message(held))
    if refused:
        err.append("WARNING: " + route_problem_text(*refused) + ". This report stays queued. " + SEND_FALLBACKS)
    return out, err


RELAY_REFUSAL = "Unappointed Desktop recipient refused"  # the relay's HTTP 400 text for a chat that is not appointed (#454)
ROUTE_STATUS = "route-status.json"
ROUTE_CONSENT = ("registering a project in Office needs the owner's consent (the project onboarding in the Office panel); "
                 "this tool, the doctor and the supervisor never write the Office registry, and doctor.py shows the registration state")


def route_refusal_kind(error):
    """'named' when the relay says it refused an unappointed chat, 'probable' for any other HTTP 400 (a relay that does not say
    so, or another invalid request), None for every other failure."""
    text = str(error)
    if not isinstance(error, RuntimeError) or not text.startswith("HTTP Error 400"):
        return None
    return "named" if RELAY_REFUSAL in text else "probable"


def route_problem_text(recipient, cause):
    if cause == "named":
        why = f"the relay refused the {recipient} chat: the project is not in the Office registry or the chat is not bound in its roster"
    else:
        why = (f"the relay answered HTTP 400 to a read of the {recipient} chat: probably the project is not in the Office registry or the "
               "chat is not bound in its roster (this relay does not say which)")
    return f"reports to {recipient} wait because {why}; {ROUTE_CONSENT}"


def route_status_file(project_root):
    return Path(project_root) / ".bot-mode" / "outbox" / ROUTE_STATUS


def read_route_status(project_root):
    """{recipient: {cause, first_at, last_at, count}} of the recipients whose chat the relay refused at the last read. Empty
    when there is no record or it cannot be read."""
    try:
        recipients = json.loads(route_status_file(project_root).read_text(encoding="utf-8"))["recipients"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return {name: entry for name, entry in recipients.items() if isinstance(entry, dict)} if isinstance(recipients, dict) else {}


def write_route_status(project_root, recipients):
    path = route_status_file(project_root)
    try:
        if not recipients:
            path.unlink(missing_ok=True)
            return
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"schema": 1, "recipients": recipients}), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass  # the record is information: failing to write it never stops a delivery


def note_route_refused(project_root, recipient, cause):
    recipients = read_route_status(project_root)
    entry = recipients.get(recipient, {})
    now = now_iso()
    count = entry.get("count") if isinstance(entry.get("count"), int) else 0
    recipients[recipient] = {"cause": cause, "first_at": entry.get("first_at") or now, "last_at": now, "count": count + 1}
    write_route_status(project_root, recipients)


def clear_route_refused(project_root, recipient):
    """The relay answered a read of this recipient's chat: the refusal is over."""
    recipients = read_route_status(project_root)
    if recipients.pop(recipient, None) is not None:
        write_route_status(project_root, recipients)


REFUSED_FOR_MISSING_TURN = re.compile(r"HTTP Error 503: [^()]*\(Cannot steer conversation \S+ without an active turn id\)")


def refused_for_missing_turn(error):
    """True only for the one refusal Desktop gives when it tries to steer a turn it believes is running and finds no turn id:
    HTTP 503 with 'Cannot steer conversation <id> without an active turn id' (#426). It is a definite refusal before anything
    starts, so the message was not taken (not proven: the Desktop code is not in this repository). Every other failure of a
    send, including 'outcome unconfirmed', stays unconfirmed. The whole error must be the refusal and nothing else: the text of an
    unconfirmed outcome can end with a stderr tail that quotes the same line, so a phrase somewhere inside is not enough."""
    text = str(error)
    if not isinstance(error, RuntimeError) or "outcome unconfirmed" in text or "do not resend" in text:
        return False
    return REFUSED_FOR_MISSING_TURN.fullmatch(text) is not None


def relay_answered_about_the_chat(error):
    """True when the relay itself answered one call with an error (HTTP 400 or 5xx, or an error in the reply): that is about
    the chat or Desktop, not about us. False when the relay refused us (401 or 403: a restarted relay has a new token) or
    could not be reached at all: then the descriptor must be read again."""
    return isinstance(error, RuntimeError) and not str(error).startswith(("HTTP Error 401", "HTTP Error 403"))


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
    return (f"Outbox pump: unconfirmed item {row['id']} ({row.get('error') or 'unknown outcome'}); later reports for the same recipient wait for it. "
            f"Check the target thread, then run: outbox.py resolve {row['id']} --as delivered|skipped|retry")


def delivery_marker(msg_id):
    return f"[Outbox delivery: {msg_id}]"


DIGEST_HEADER = "[Outbox digest: "


def build_prompt(rows):
    """One row is sent exactly as before: the marker line, then the text. Several rows become one digest message in
    creation order; every report keeps its own marker line, sender and time, so each row can be confirmed on its own."""
    if len(rows) == 1:
        return delivery_marker(rows[0]["id"]) + "\n" + rows[0]["text"]
    parts = [f"{DIGEST_HEADER}{len(rows)} reports, oldest first]"]
    for row in rows:
        parts.append(f"{delivery_marker(row['id'])}\nFrom: {row['sender_bot']} ({row['created_at']})\n{row['text'].strip()}")
    return "\n\n".join(parts)


def _marker_lines(text):
    """Marker lines of a message the outbox itself produced (it starts with a marker or a digest header), else nothing."""
    text = text.lstrip("\ufeff").replace("\r", "")
    if not (text.startswith("[Outbox delivery: ") or text.startswith(DIGEST_HEADER)):
        return set()
    return {line for line in text.split("\n") if line.startswith("[Outbox delivery: ")}


def _read_history(client, thread, wanted):
    """Read up to RECONCILE_PAGES pages of RECONCILE_TURNS turns of a thread, looking for the marker lines in wanted.
    Returns (found, complete, readable).

    found: the wanted marker lines seen in user messages. A marker seen on a page that was read proves delivery whatever
    happens later; reading stops as soon as every wanted marker is found.
    complete: every reply had the expected shape (turns and items as lists, page a mapping), every user message had
    readable text content (a list of parts, text parts holding text), no request failed, and the last page named no
    further cursor. A request that fails (an older page, say) only makes the read incomplete; it never erases a marker
    already found.
    readable: some user message came back with more than one character of text. The relay reduces a chat that is only
    read-only reachable to metadata with one character per item (desktop_relay.prepare), and a marker cannot be found
    in that; a reply without any longer user text is therefore never taken for a full read.
    A reply that is malformed, redacted or cut off is never evidence that a message is missing."""
    found, cursor, readable = set(), None, False
    for _ in range(RECONCILE_PAGES):
        args = {"threadId": thread, "turnLimit": RECONCILE_TURNS, "includeOutputs": False}
        if cursor:
            args["cursor"] = cursor
        try:
            page = client.call("read_thread", args)
        except Exception as error:
            print(f"Outbox reconciliation unavailable for thread {thread}: {error}", file=sys.stderr)
            return found, False, readable
        turns = page.get("turns") if isinstance(page, dict) else None
        info = page.get("page") if isinstance(page, dict) else None
        if not isinstance(turns, list):
            return found, False, readable
        complete = isinstance(info, dict)
        for turn in turns:
            items = turn.get("items") if isinstance(turn, dict) else None
            if not isinstance(items, list):
                complete = False
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "userMessage":
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    complete = False  # a user message whose content cannot be read hides what it said
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        complete = False
                    elif part.get("type") == "text":
                        text = part.get("text")
                        if not isinstance(text, str):
                            complete = False
                            continue
                        found |= _marker_lines(text) & wanted
                        readable = readable or len(text) > 1
        if found >= wanted:
            return found, True, readable  # everything asked for is proven; nothing is left to decide
        if not complete:
            return found, False, readable
        cursor = info.get("nextCursor")
        if cursor in (None, ""):
            return found, True, readable
        if not isinstance(cursor, str):
            return found, False, readable
    return found, False, readable  # more history than the bound: not complete


def reconcile_ambiguous(db, client, retry_grace_minutes=None, states=UNSURE_STATES, recheck_hours=None):
    """Resolve unconfirmed rows from the recipient thread's history (#401, #422). One history read per thread serves all
    its rows. recheck_hours limits the rows to those younger than that.

    Only positive evidence in a recipient's user message proves delivery (the row becomes delivered). A missing marker
    proves non-delivery only when the read was complete, well formed and unredacted (see _read_history), the row is
    ambiguous (not needs-attention) and retry_grace_minutes have passed since it became ambiguous; then the row goes
    back to pending (auto-retry). Everything else stays as it is. Never raises for a relay or history problem."""
    marks = ",".join("?" * len(states))
    sql = f"SELECT id, target_thread_id, state, ambiguous_at FROM outbox WHERE state IN ({marks})"
    args = list(states)
    if recheck_hours is not None:
        sql += " AND created_at >= ?"
        args.append(datetime.fromtimestamp(time.time() - recheck_hours * 3600, timezone.utc).isoformat())
    by_thread = {}
    for row in db.conn.execute(sql, args).fetchall():
        if row["target_thread_id"]:  # legacy rows cannot be bound safely to an actual destination
            by_thread.setdefault(row["target_thread_id"], []).append(row)
    for thread, group in by_thread.items():
        wanted = {delivery_marker(row["id"]): row for row in group}
        found, complete, readable = _read_history(client, thread, set(wanted))
        for marker, row in wanted.items():
            if marker in found:
                db.mark_delivered(row["id"])
            elif complete and readable and retry_grace_minutes and row["state"] == "ambiguous" and row["ambiguous_at"]:
                try:
                    age = time.time() - datetime.fromisoformat(row["ambiguous_at"]).timestamp()
                except ValueError:
                    continue
                if age >= retry_grace_minutes * 60:
                    db.auto_retry(row["id"], retry_grace_minutes)


def pump_outbox(project_root, max_wait_sec=600, poll_interval=3.0, turn_timeout=120.0, ttl_hours=DEFAULT_TTL_HOURS,
                attention_minutes=None, retry_grace_minutes=None, digest_max=None, reconcile_every=RECONCILE_EVERY, rechecks=None):
    """Deliver pending rows in order: one message at a time, only to an idle thread, and only after the previous message
    produced a registered turn that has finished. Rows waiting for the same recipient go out as one digest. An ambiguous
    row holds back later rows of its own recipient only, until it is resolved or becomes needs-attention (#422).
    Returns 0 when it ended normally (it waits for a missing relay), EXIT_NOT_REGISTERED when a delivery showed no
    Manager turn."""
    config = load_config(project_root)
    attention_minutes = setting(attention_minutes, config, "attention_minutes", DEFAULT_ATTENTION_MINUTES)
    retry_grace_minutes = setting(retry_grace_minutes, config, "retry_grace_minutes", DEFAULT_RETRY_GRACE_MINUTES)
    digest_max = max(1, int(setting(digest_max, config, "digest_max", DEFAULT_DIGEST_MAX)))
    rechecks = int(setting(rechecks, config, "rechecks", DEFAULT_RECHECKS))
    outbox_dir = Path(project_root) / ".bot-mode" / "outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    lock = PumpLock(outbox_dir / "pump.lock")
    if not lock.acquire():
        return 0  # another pump is running

    try:
        db = OutboxDB(outbox_dir / "queue.sqlite")
        try:
            db.recover_sending()
            keep = int(setting(None, config, "keep_rows", DEFAULT_KEEP_ROWS))
            if db.count_state("delivered") > 2 * keep:  # the file stays bounded without anyone running prune
                try:
                    prune_outbox(project_root, keep, ttl_hours, int(setting(None, config, "max_backups", DEFAULT_MAX_BACKUPS)))
                except Exception as error:
                    print(f"Outbox pump: automatic prune failed ({error})", file=sys.stderr)
            client = None
            reconciled = False
            last_reconcile = 0.0
            idle_passes = 0  # rereads made while nothing was pending
            resolved = {}
            busy_until = {}  # target -> time until which it is not asked again; the queue moves on to other targets
            unreadable = {}  # target -> consecutive reads of its chat that the relay refused with an error about that chat
            canary_retry_at = 0.0  # a canary that failed is tried again after this time
            not_ready = {}  # target -> consecutive sends Desktop refused because the chat had no active turn id to steer
            relay_wait = poll_interval
            warned = False
            last_beat = 0.0
            start_time = time.time()
            while time.time() - start_time < max_wait_sec:
                db.expire_stale(ttl_hours)
                db.escalate_ambiguous(attention_minutes)
                now = time.time()
                busy_until = {key: until for key, until in busy_until.items() if until > now}
                pending = db.pending_count()
                unsure = db.recheckable_count(ttl_hours)
                canary = db.canary_pending()
                if pending:
                    idle_passes = 0
                if not pending and not canary and (not unsure or (reconciled and idle_passes >= rechecks)):
                    break  # nothing pending and nothing left to recheck

                if time.time() - last_beat >= HEARTBEAT_EVERY:
                    last_beat = time.time()
                    write_heartbeat(outbox_dir, pending=pending, relay=client is not None)

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

                if canary and time.time() >= canary_retry_at:
                    # The canary (#426) is not a report: it is processed at once, also while the Manager is busy, and sends nothing.
                    failure = probe_canary(db, client, project_root, canary)
                    if failure is None:
                        canary = None
                    else:
                        print(f"Outbox pump: canary not processed ({failure})", file=sys.stderr)
                        canary_retry_at = time.time() + 5 * poll_interval
                        if not relay_answered_about_the_chat(failure):
                            client = None  # the relay may have restarted: read its descriptor again
                            time.sleep(relay_wait)
                            relay_wait = min(relay_wait * 2, RELAY_WAIT_MAX)
                            continue

                if not reconciled or (unsure and time.time() - last_reconcile >= reconcile_every):
                    reconcile_ambiguous(db, client, retry_grace_minutes, recheck_hours=ttl_hours)
                    reconciled = True
                    last_reconcile = time.time()  # rows may now be delivered or back in pending: the batch below reads the queue afresh
                    pending = db.pending_count()
                    if not pending:
                        idle_passes += 1
                if not pending:
                    if canary:
                        time.sleep(poll_interval)  # only a canary is waiting: it is tried again
                        continue
                    if db.recheckable_count(ttl_hours) and idle_passes < rechecks:
                        time.sleep(poll_interval)  # only unresolved rows are left: look again after reconcile_every
                        continue
                    break
                batch = db.next_batch(busy_until, digest_max)
                if not batch:
                    time.sleep(poll_interval)  # every pending recipient is busy or held behind an unconfirmed row
                    continue
                item = batch[0]

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
                    if relay_answered_about_the_chat(e):
                        # The relay is up and refused this one chat (Desktop answers 503, e.g. thread not found): only this
                        # recipient is set aside, with a growing pause; the others keep moving. The row stays pending.
                        unreadable[key] = unreadable.get(key, 0) + 1
                        refusal = route_refusal_kind(e)
                        if refusal:
                            note_route_refused(project_root, key, refusal)
                        if unreadable[key] == 1:
                            print(f"Outbox pump: read_thread of {item['target_bot']} failed ({e}); setting it aside, "
                                  "other recipients are not held.", file=sys.stderr)
                            if refusal:
                                print("Outbox pump: " + route_problem_text(item["target_bot"], refusal), file=sys.stderr)
                        busy_until[key] = time.time() + min(poll_interval * 2 ** unreadable[key], RELAY_WAIT_MAX)
                        relay_wait = poll_interval
                        continue
                    print(f"Outbox pump: read_thread failed ({e}); backing off...", file=sys.stderr)
                    client = None  # the relay may have restarted: read its descriptor again
                    time.sleep(relay_wait)
                    relay_wait = min(relay_wait * 2, RELAY_WAIT_MAX)
                    continue
                relay_wait = poll_interval
                unreadable.pop(key, None)
                clear_route_refused(project_root, key)

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
                ids = [row["id"] for row in batch]
                try:
                    db.mark_sending_many(ids, target_thread)
                    client.call("send_message_to_thread", {"threadId": target_thread, "prompt": build_prompt(batch)})
                except Exception as e:
                    if refused_for_missing_turn(e):
                        # Desktop refused before starting anything: the chat is not ready. The rows stay pending in their place,
                        # only this recipient is set aside with a growing pause, nothing else is held. If Desktop had taken the
                        # message anyway the next try repeats it with the same delivery marker line (one harmless duplicate).
                        db.mark_unsent_many(ids, f"not sent: {e}")
                        not_ready[key] = not_ready.get(key, 0) + 1
                        pause = min(poll_interval * 2 ** not_ready[key], RELAY_WAIT_MAX)
                        busy_until[key] = time.time() + pause
                        print(f"Outbox pump: {item['target_bot']} refused the message because its chat has no active turn id to steer "
                              f"({e}); the report stays pending, trying again in {pause:g} s.", file=sys.stderr)
                        continue
                    # The relay may or may not have accepted it: never sent again without proof; it holds later rows of this recipient.
                    print(f"Outbox pump: send_message_to_thread failed ({e})", file=sys.stderr)
                    db.mark_ambiguous_many(ids, e)
                    print(f"Outbox pump: {', '.join(ids)} unconfirmed; later reports for this recipient wait until it is resolved.", file=sys.stderr)
                    client = None
                    reconciled = False
                    continue
                db.mark_delivered_many(ids)
                not_ready.pop(key, None)
                print(f"Delivered {', '.join(ids)} to {item['target_bot']} ({target_thread})" + (" as one digest." if len(ids) > 1 else "."))
                if not await_turn(client, target_thread, before, poll_interval, turn_timeout):
                    for msg_id in ids:
                        db.note(msg_id, f"delivered; no new Manager turn seen within {turn_timeout:g}s")
                    print(f"Outbox pump: no new Manager turn seen within {turn_timeout:g}s after {', '.join(ids)}; stopping so the next "
                          "report is not sent into an unconfirmed turn. Check the Manager thread, then run the pump again.", file=sys.stderr)
                    return EXIT_NOT_REGISTERED
                # The next loop pass waits for that turn to finish (thread idle) before the next message.
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
    p.add_argument("--attention-minutes", type=float, default=None,
                   help="An unconfirmed row older than this becomes needs-attention and stops holding later rows (default 30)")
    p.add_argument("--retry-grace-minutes", type=float, default=None,
                   help="A complete thread history without the marker proves non-delivery after this long; 0 never retries (default 10)")
    p.add_argument("--digest-max", type=int, default=None,
                   help="Most reports delivered as one digest message; 1 sends them one by one (default 20)")

    # status
    st = sub.add_parser("status", help="Health of the outbox, relay and pump (exit 5 when something needs attention).")
    st.add_argument("--heal", action="store_true", help="Start the pump when work is waiting and no pump runs")

    # list
    l = sub.add_parser("list", help="List recent outbox rows.")
    l.add_argument("--state", choices=["pending", "sending", "delivered", "failed", "ambiguous", "needs-attention", "skipped", "expired"],
                   help="Only rows in this state")

    # resolve
    r = sub.add_parser("resolve", help="Operator decision on an ambiguous row (or retry of a failed row).")
    r.add_argument("id")
    r.add_argument("--as", dest="resolution", required=True, choices=sorted(RESOLUTIONS),
                   help="delivered: it arrived; skipped: never send it; retry: it did not arrive, send it again in its place")
    r.add_argument("--note", default="", help="Short reason kept on the row")

    # prune
    pr = sub.add_parser("prune", help="Bound the queue file: expire old pending rows, keep the newest delivered rows, back up first.")
    pr.add_argument("--keep", type=int, default=None, help="Delivered (and skipped, expired) rows to keep (default 200)")
    pr.add_argument("--max-backups", type=int, default=None, help="Database backups to keep in .bot-mode/outbox/backups (default 5)")
    pr.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS, help="Pending rows older than this expire (default 12)")
    pr.add_argument("--dry-run", action="store_true", help="Only report what would change")

    # canary
    ca = sub.add_parser("canary", help="Zero-token canary: queue a synthetic probe of the whole delivery path (nothing is sent to "
                                      "the Manager), print its latency and say whether it is processed (exit 5 when not).")
    ca.add_argument("--to", default="manager", help="Recipient whose chat is probed (default: manager)")
    ca.add_argument("--every-minutes", type=float, default=DEFAULT_CANARY_EVERY_MINUTES,
                    help="Queue a new canary when the last one is older than this (default 10)")
    ca.add_argument("--limit-minutes", type=float, default=DEFAULT_CANARY_LIMIT_MINUTES,
                    help="A canary not processed within this time is a problem (default 5)")

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
            last = db.last_delivered_age()
        finally:
            db.close()

        print(f"Queued outbox handoff {msg_id} for {args.to}. Turn complete.")
        refusal = read_route_status(project_root).get(args.to)
        lines, warnings = send_feedback(depth, age, last, relay_reachable(project_root), pump_alive(outbox_dir), held,
                                        (args.to, refusal.get("cause")) if refusal else None)
        for line in lines:
            print(line)
        for line in warnings:
            print(line, file=sys.stderr)
        spawn_background_pump(project_root)
        return 0

    elif args.action == "pump":
        return pump_outbox(project_root, max_wait_sec=args.max_wait, turn_timeout=args.turn_timeout, ttl_hours=args.ttl_hours,
                           attention_minutes=args.attention_minutes, retry_grace_minutes=args.retry_grace_minutes,
                           digest_max=args.digest_max)

    elif args.action == "canary":
        outbox_dir = project_root / ".bot-mode" / "outbox"
        db = OutboxDB(outbox_dir / "queue.sqlite")
        try:
            started = db.canary_start(args.to, args.every_minutes * 60)
            report = canary_report(db, args.limit_minutes)
            waiting = db.canary_pending() is not None
        finally:
            db.close()
        report["queued"] = started is not None
        if waiting and (started or not pump_alive(outbox_dir)):
            spawn_background_pump(project_root)  # a pump that is already running leaves it alone
        print(json.dumps(report, ensure_ascii=False))
        return EXIT_UNHEALTHY if report["problems"] else 0

    elif args.action == "prune":
        config = load_config(project_root)
        summary = prune_outbox(project_root, int(setting(args.keep, config, "keep_rows", DEFAULT_KEEP_ROWS)), args.ttl_hours,
                               int(setting(args.max_backups, config, "max_backups", DEFAULT_MAX_BACKUPS)), args.dry_run)
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    elif args.action == "status":
        status = queue_status(project_root)
        if args.heal and (status["pending"] or status["recheckable"] or (status["canary"] or {}).get("pending_seconds") is not None) and not status["pump_alive"]:
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
