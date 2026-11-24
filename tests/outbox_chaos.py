"""A scripted stand-in for Codex Desktop and its relay, for chaos tests of the outbox (#426, S6).

Nothing here touches a live relay, queue or task. Desktop is a few in-memory chats. The relay is a real HTTP server on
127.0.0.1 that speaks the protocol the outbox client uses (POST /call, the X-Office-Relay token, a relay.json
descriptor), so the real outbox.py runs against it as separate processes and a kill is a real process kill.

What this can show is that our side follows the protocol as it is recorded in the code and in the tests of #401, #415,
#422 and #429. It cannot show that a live Desktop behaves that way."""
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = Path(__file__).resolve().parents[1]
MANAGER = "00000000-0000-0000-0000-0000000000a1"
DEVELOPER = "00000000-0000-0000-0000-0000000000a2"


class Desktop:
    """The chats behind the relay: their status, their user messages and what was sent to them."""

    def __init__(self):
        self.lock = threading.RLock()
        self.chats = {}
        self.sends = []  # (thread, prompt) that Desktop accepted
        self.status_reads = {}
        self.history_reads = 0
        self.down_reads = 0  # the next N read_thread calls fail: Desktop is restarting
        self.unknown = set()  # chats Desktop does not know
        self.refusals = {}  # thread -> [times, message]: Desktop refuses the next sends with a 503 and takes nothing

    def add_chat(self, thread, status="idle", prompts=("earlier message from the owner",), readable=True):
        """status: idle, active or notLoaded. readable False: a chat that is not loaded and shows no turns."""
        self.chats[thread] = {"status": status, "prompts": list(prompts), "turns": 1, "readable": readable}

    def set(self, thread, **changes):
        with self.lock:
            self.chats[thread].update(changes)

    def refuse_next(self, thread, times, message):
        with self.lock:
            self.refusals[thread] = [times, message]

    def accept(self, thread, prompt):
        with self.lock:
            refusal = self.refusals.get(thread)
            if refusal and refusal[0] > 0:
                refusal[0] -= 1
                raise LookupError(refusal[1])  # the relay answers 503 with this text
            chat = self.chats[thread]
            chat["prompts"].append(prompt)
            chat["turns"] += 1
            self.sends.append((thread, prompt))

    def sent_to(self, thread):
        with self.lock:
            return [prompt for t, prompt in self.sends if t == thread]

    def read(self, arguments):
        with self.lock:
            thread = arguments["threadId"]
            if self.down_reads > 0:
                self.down_reads -= 1
                raise ConnectionError("Desktop is not answering")
            if thread in self.unknown or thread not in self.chats:
                raise LookupError("thread not found")
            chat = self.chats[thread]
            limit = arguments.get("turnLimit")
            if limit == 1:
                self.status_reads[thread] = self.status_reads.get(thread, 0) + 1
                if "reply" in chat:  # a scripted answer, whatever the chat is (for example {} or an unknown status)
                    return chat["reply"]
                reply = {"thread": {"status": {"type": chat["status"]}}}
                if chat["status"] != "notLoaded" or chat["readable"]:
                    n = chat["turns"]
                    reply["turns"] = [{"id": "t%d" % n, "startedAt": n, "status": "inProgress" if chat["status"] == "active" else "completed"}]
                return reply
            self.history_reads += 1
            if limit > 10:  # the real Desktop tool refuses more than 10 turns per call (#429)
                raise ValueError("turnLimit: Too big: expected number to be <=10")
            if not chat["readable"]:
                raise ConnectionError("history unavailable")
            start = int(arguments.get("cursor") or 0)
            part = chat["prompts"][start:start + limit]
            turns = [{"id": "h%d" % n, "items": [{"type": "userMessage", "content": [{"type": "text", "text": prompt}]}]}
                     for n, prompt in enumerate(part, start)]
            more = start + limit < len(chat["prompts"])
            return {"turns": turns, "page": {"nextCursor": str(start + limit)} if more else {}}


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # a caller that was killed mid-request is part of the scenario, not an error to print


class Relay:
    """One life of the relay. Another Relay on the same Desktop is a restarted relay with a new port and a new token.

    Faults, each for the next send_message_to_thread only:
    die_after_taking: Desktop gets the message, the relay dies without answering.
    die_before_taking: the relay dies before Desktop gets the message.
    hold_after_taking / hold_before_taking: the call stays open until release is set (the test kills the caller meanwhile)."""

    def __init__(self, desktop):
        self.desktop = desktop
        self.token = secrets.token_hex(8)
        self.tokens_seen = set()
        self.die_after_taking = self.die_before_taking = self.hold_after_taking = self.hold_before_taking = False
        self.held = threading.Event()
        self.release = threading.Event()
        self.alive = True
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def drop(self):
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            def do_POST(self):
                token = self.headers.get("X-Office-Relay")
                relay.tokens_seen.add(token)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if token != relay.token:
                    return self.reply(403, {"error": "Desktop relay authorization required"})
                if self.path != "/call":
                    return self.reply(400, {"error": "Invalid Desktop relay request"})
                name, arguments = body.get("name"), body.get("arguments") or {}
                try:
                    if name == "read_thread":
                        return self.reply(200, {"result": relay.desktop.read(arguments)})
                    if name != "send_message_to_thread":
                        return self.reply(400, {"error": "Invalid Desktop relay request"})
                    thread, prompt = arguments["threadId"], arguments["prompt"]
                    if relay.die_before_taking or relay.hold_before_taking:
                        if relay.hold_before_taking:
                            relay.held.set()
                            relay.release.wait(30)
                        relay.die_before_taking = relay.hold_before_taking = False
                        relay.kill_later()
                        return self.drop()
                    relay.desktop.accept(thread, prompt)
                    if relay.die_after_taking or relay.hold_after_taking:
                        if relay.hold_after_taking:
                            relay.held.set()
                            relay.release.wait(30)
                        relay.die_after_taking = relay.hold_after_taking = False
                        relay.kill_later()
                        return self.drop()
                    return self.reply(200, {"result": {}})
                except (ConnectionError, LookupError, ValueError) as error:
                    return self.reply(503, {"error": str(error)})  # what the real relay answers when Desktop fails a call

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def write_descriptor(self, project_root):
        folder = Path(project_root) / ".bot-mode" / "desktop-relay"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "relay.json").write_text(json.dumps({"url": self.url, "token": self.token}), encoding="utf-8")

    def kill_later(self):
        threading.Thread(target=self.kill, daemon=True).start()

    def kill(self):
        if self.alive:
            self.alive = False
            self.server.shutdown()
            self.server.server_close()


PUMP = """
import json, sys
sys.path.insert(0, sys.argv[1])
from scripts.team_room import outbox
sys.exit(outbox.pump_outbox(sys.argv[2], **json.loads(sys.argv[3])))
"""

SEND = """
import sys
sys.path.insert(0, sys.argv[1])
from scripts.team_room import outbox
outbox.spawn_background_pump = lambda root: None  # a real send starts a detached pump; the test runs the pump itself
sys.exit(outbox.main(["--project", sys.argv[2], "send", "--to", sys.argv[3], "--by", sys.argv[4], "--dedupe-key", sys.argv[5], "--text", sys.argv[6]]))
"""


CANARY = """
import sys
sys.path.insert(0, sys.argv[1])
from scripts.team_room import outbox
outbox.spawn_background_pump = lambda root: None  # the test runs the pump itself
sys.exit(outbox.main(["--project", sys.argv[2], "canary"] + sys.argv[3:]))
"""


class Project:
    """A throw-away project with a roster (Manager and Developer chats), an outbox and the processes started for it."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="outbox_chaos_"))
        folder = self.root / ".bot-mode"
        folder.mkdir()
        roster = {"participants": [{"bot": "manager", "role": "Manager", "thread_id": MANAGER},
                                   {"bot": "developer", "role": "Developer", "thread_id": DEVELOPER}]}
        (folder / "roster.json").write_text(json.dumps(roster), encoding="utf-8")
        self.db_path = folder / "outbox" / "queue.sqlite"
        self.processes = []
        self.relays = []
        self.logs = 0

    def close(self):
        for relay in self.relays:
            relay.release.set()
            relay.kill()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.log.close()
        shutil.rmtree(self.root, True)

    def relay(self, desktop, write=True):
        relay = Relay(desktop)
        self.relays.append(relay)
        if write:
            relay.write_descriptor(self.root)
        return relay

    def _db(self):
        from scripts.team_room import outbox
        return outbox.OutboxDB(self.db_path)

    def enqueue(self, sender, target, text, key=None):
        db = self._db()
        try:
            return db.enqueue(sender, target, None, text, key)
        finally:
            db.close()

    def rows(self):
        db = self._db()
        try:
            return {row["id"]: dict(row) for row in db.conn.execute("SELECT * FROM outbox ORDER BY rowid")}
        finally:
            db.close()

    def state(self, row_id):
        return self.rows()[row_id]["state"]

    def pump(self, **options):
        """The real pump in its own process, with short waits so a scenario takes seconds."""
        options = {"max_wait_sec": 25, "poll_interval": 0.05, "turn_timeout": 3, "reconcile_every": 0.2,
                   "retry_grace_minutes": 0.002, **options}
        return self._start([sys.executable, "-B", "-c", PUMP, str(REPO), str(self.root), json.dumps(options)])

    def send(self, sender, target, text, key):
        """What an agent runs: outbox.py send, in its own process (without the detached pump a real send starts)."""
        return self._start([sys.executable, "-B", "-c", SEND, str(REPO), str(self.root), target, sender, key, text])

    def _start(self, command):
        self.logs += 1
        out = open(self.root / ("process%d.log" % self.logs), "w+b")
        process = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, cwd=str(REPO))
        process.log = out
        self.processes.append(process)
        return process

    def output(self, process):
        process.log.flush()
        process.log.seek(0)
        return process.log.read().decode("utf-8", "replace")

    def canary(self, *arguments):
        """outbox.py canary in its own process: (exit code, the printed report)."""
        process = self._start([sys.executable, "-B", "-c", CANARY, str(REPO), str(self.root), *arguments])
        code = process.wait(timeout=60)
        return code, json.loads(self.output(process).strip().splitlines()[-1])

    def canaries(self):
        db = self._db()
        try:
            return [dict(row) for row in db.conn.execute("SELECT * FROM canary ORDER BY created_at, rowid")]
        finally:
            db.close()

    def age_canary(self, seconds):
        """Make every canary look that much older (a wait that would take minutes in real time)."""
        db = self._db()
        try:
            with db.conn:
                for row in db.conn.execute("SELECT id, created_at FROM canary").fetchall():
                    old = datetime.fromtimestamp(datetime.fromisoformat(row["created_at"]).timestamp() - seconds, timezone.utc).isoformat()
                    db.conn.execute("UPDATE canary SET created_at = ? WHERE id = ?", (old, row["id"]))
        finally:
            db.close()

    def settle_canary(self, timeout=40):
        """Run pumps, as the supervisor's heal does, until no canary is pending."""
        deadline = time.time() + timeout
        pump = self.pump()
        while time.time() < deadline:
            if not any(c["state"] == "pending" for c in self.canaries()):
                return
            if pump.poll() is not None:
                time.sleep(0.3)
                pump = self.pump()
            time.sleep(0.05)
        raise AssertionError("the canary was not processed in %d s" % timeout)

    def settle(self, row_ids, timeout=40):
        """Run pumps the way the supervisor's heal does (a new pump whenever none is running, here every 0.3 s at most)
        until every row is delivered. Returns the number of pumps that were started."""
        deadline = time.time() + timeout
        pump, started = self.pump(), 1
        while time.time() < deadline:
            if all(self.state(row) == "delivered" for row in row_ids):
                return started
            if pump.poll() is not None:
                time.sleep(0.3)
                pump, started = self.pump(), started + 1
            time.sleep(0.05)
        raise AssertionError("not delivered in %d s: %s" % (timeout, {r: self.state(r) for r in row_ids}))


def wait_for(condition, timeout=20.0, what="the condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for " + what)
