"""Tests for multi-project profile resolution in scripts/team_room/settings.py and web.py (#324)."""
import http.client
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

import isolation
isolation.isolate()

from scripts.team_room import project as served
from scripts.team_room.settings import BotSettings, version
from scripts.team_room.web import create_server
from scripts.team_room.controller import Controller
try:
    from test_team_room_controller import FakeTransport
except ImportError:
    class FakeTransport:
        def __init__(self):
            self.owned_threads = {}; self.turns = {}; self.sent = []; self.failure = None; self.interrupts = []; self.instructions = {}; self.closed = False
        def initialize(self): self.closed = False
        def start_thread(self, cwd, model, instructions):
            tid = 'owned-' + getattr(cwd, 'name', str(cwd))
            self.owned_threads[tid] = cwd
            self.turns[tid] = []
            self.instructions[getattr(cwd, 'name', str(cwd))] = instructions
            return {'thread': {'id': tid}, 'model': model}
        def read_thread(self, tid):
            return {'thread': {'turns': self.turns.get(tid, [])}}
        def resume_thread(self, tid, model, instructions=None):
            return self.read_thread(tid)
        def close(self): self.closed = True


class MultiProjectProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.proj_a = self.base / "proj_a"
        self.proj_b = self.base / "proj_b"
        self.proj_a.mkdir()
        self.proj_b.mkdir()

        # Project A setup (current served project)
        (self.proj_a / ".bot-mode").mkdir(parents=True)
        roster_a = {
            "schema": 1,
            "project": {"id": "proj-a", "name": "Project A"},
            "teams": [{"name": "Development"}],
            "participants": [
                {"bot": "manager", "role": "Manager", "team": "Development", "workspace": "agents/project-manager", "status": "active"},
                {"bot": "developer", "role": "Developer", "team": "Development", "workspace": "team/Development/developer", "status": "active"}
            ]
        }
        (self.proj_a / ".bot-mode" / "roster.json").write_text(json.dumps(roster_a), encoding="utf-8")
        mgr_a = self.proj_a / "agents/project-manager"
        mgr_a.mkdir(parents=True)
        (mgr_a / "PROFILE.md").write_text("- Bot ID: `project-manager`\nRole: Proj A Manager\n", encoding="utf-8")
        (mgr_a / "SOUL.md").write_text("Proj A Manager Soul", encoding="utf-8")
        (mgr_a / "memory").mkdir()
        (mgr_a / "memory" / "README.md").write_text("Proj A Manager Memory Contract", encoding="utf-8")

        # Project B setup (RoutePort, registered project)
        (self.proj_b / ".bot-mode").mkdir(parents=True)
        roster_b = {
            "schema": 1,
            "project": {"id": "routeport", "name": "RoutePort"},
            "teams": [{"name": "Logistics"}],
            "participants": [
                {"bot": "manager", "role": "Manager", "team": "Logistics", "workspace": "agents/project-manager", "status": "active"},
                {"bot": "routeport-worker", "role": "Dispatcher", "team": "Logistics", "workspace": "team/Logistics/routeport-worker", "status": "active"}
            ]
        }
        (self.proj_b / ".bot-mode" / "roster.json").write_text(json.dumps(roster_b), encoding="utf-8")
        mgr_b = self.proj_b / "agents/project-manager"
        mgr_b.mkdir(parents=True)
        (mgr_b / "PROFILE.md").write_text("- Bot ID: `project-manager`\nRole: RoutePort Manager\n", encoding="utf-8")
        (mgr_b / "SOUL.md").write_text("RoutePort Manager Soul", encoding="utf-8")
        (mgr_b / "memory").mkdir()
        (mgr_b / "memory" / "README.md").write_text("RoutePort Manager Memory Contract", encoding="utf-8")

        wrk_b = self.proj_b / "team/Logistics/routeport-worker/agent"
        wrk_b.mkdir(parents=True)
        (wrk_b / "agent.yaml").write_text("parameters:\n  display_name:\n    value: RoutePort Dispatcher\n", encoding="utf-8")
        (wrk_b / "PROFILE.md").write_text("- Bot ID: `routeport-worker`\nRole: Dispatcher\n", encoding="utf-8")
        (wrk_b / "SOUL.md").write_text("RoutePort Dispatcher Soul", encoding="utf-8")
        (wrk_b / "memory").mkdir()
        (wrk_b / "memory" / "README.md").write_text("RoutePort Dispatcher Memory Contract", encoding="utf-8")

        # Registry file linking both projects
        self.reg_file = self.base / "registry.json"
        reg_data = {
            "schema": 1,
            "projects": [
                {"id": "proj-a", "name": "Project A", "root": str(self.proj_a)},
                {"id": "routeport", "name": "RoutePort", "root": str(self.proj_b)}
            ]
        }
        self.reg_file.write_text(json.dumps(reg_data), encoding="utf-8")
        self.settings = BotSettings(self.proj_a, registry_file=self.reg_file)

    def test_read_by_bot_key(self):
        res_soul = self.settings.read("routeport:manager", "SOUL.md")
        self.assertEqual(res_soul["content"], "RoutePort Manager Soul")
        self.assertEqual(res_soul["project_id"], "routeport")
        self.assertEqual(res_soul["bot"], "routeport:manager")
        self.assertEqual(res_soul["bot_id"], "manager")

        res_prof = self.settings.read("routeport:routeport-worker", "PROFILE.md")
        self.assertIn("Role: Dispatcher", res_prof["content"])
        self.assertEqual(res_prof["project_id"], "routeport")
        self.assertEqual(res_prof["bot_id"], "routeport-worker")

    def test_read_by_query_project(self):
        res_b = self.settings.read("manager", "SOUL.md", project="routeport")
        self.assertEqual(res_b["content"], "RoutePort Manager Soul")
        self.assertEqual(res_b["project_id"], "routeport")

        res_a = self.settings.read("manager", "SOUL.md", project="proj-a")
        self.assertEqual(res_a["content"], "Proj A Manager Soul")

    def test_read_unique_bot_without_project_param(self):
        # routeport-worker exists only in routeport
        res = self.settings.read("routeport-worker", "SOUL.md")
        self.assertEqual(res["content"], "RoutePort Dispatcher Soul")
        self.assertEqual(res["project_id"], "routeport")

    def test_read_ambiguous_defaults_to_current_project(self):
        # manager exists in both proj_a and routeport; defaults to current (proj_a)
        res = self.settings.read("manager", "SOUL.md")
        self.assertEqual(res["content"], "Proj A Manager Soul")

    def test_read_memory_and_memory_readme(self):
        lesson = {"key": "dispatch-rule", "text": "Route carefully.", "source": "docs/route.md", "quote": "carefully", "episode": "ep1"}
        with patch("scripts.team_room.profiles._profile", return_value=("Soul", "Profile", "Contract", [lesson])):
            mem = self.settings.read("routeport:routeport-worker", "memory")
        self.assertTrue(mem["readonly"])
        self.assertIn("RoutePort Dispatcher learned memory", mem["content"])
        self.assertIn("Route carefully.", mem["content"])
        self.assertEqual(mem["lessons"], [lesson])

        contract = self.settings.read("routeport:routeport-worker", "memory/README.md")
        self.assertTrue(contract["readonly"])
        self.assertEqual(contract["content"], "RoutePort Dispatcher Memory Contract")

    def test_save_across_registered_projects(self):
        before = self.settings.read("routeport:manager", "SOUL.md")
        saved = self.settings.save("routeport:manager", "SOUL.md", "Updated Soul", before["version"])
        self.assertEqual(saved["content"], "Updated Soul")
        self.assertEqual((self.proj_b / "agents/project-manager/SOUL.md").read_text(encoding="utf-8"), "Updated Soul")

        # Proj A must be untouched
        self.assertEqual((self.proj_a / "agents/project-manager/SOUL.md").read_text(encoding="utf-8"), "Proj A Manager Soul")

    def test_save_profile_requires_bot_id(self):
        before = self.settings.read("routeport:routeport-worker", "PROFILE.md")
        with self.assertRaises(ValueError):
            self.settings.save("routeport:routeport-worker", "PROFILE.md", "No bot id line", before["version"])
        self.assertIn("Role: Dispatcher", (self.proj_b / "team/Logistics/routeport-worker/agent/PROFILE.md").read_text(encoding="utf-8"))

    def test_save_memory_is_prohibited(self):
        with self.assertRaises(ValueError):
            self.settings.save("routeport:manager", "memory", "New memory", "any-version")

    def test_conflicting_bot_key_and_project_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.settings.read("routeport:manager", "SOUL.md", project="proj-a")
        self.assertIn("Conflicting project selectors", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            self.settings.save("routeport:manager", "SOUL.md", "Malicious", "fake-version", project="proj-a")
        self.assertIn("Conflicting project selectors", str(ctx.exception))

        self.assertEqual((self.proj_a / "agents/project-manager/SOUL.md").read_text(encoding="utf-8"), "Proj A Manager Soul")
        self.assertEqual((self.proj_b / "agents/project-manager/SOUL.md").read_text(encoding="utf-8"), "RoutePort Manager Soul")

    def test_unmocked_memory_helper_missing_reports_unavailable(self):
        with self.assertRaises(ValueError) as ctx:
            self.settings.read("routeport:routeport-worker", "memory")
        self.assertIn("Memory helper", str(ctx.exception))

    def test_unmocked_memory_junction_rejected_without_executing_helper(self):
        outside = self.base / "outside_scripts"
        outside.mkdir()
        marker = outside / "executed.txt"
        outside_helper = outside / "bot_mode.py"
        outside_helper.write_text(chr(10).join([
            "import pathlib",
            f"pathlib.Path(r'{marker}').write_text('EXPLOIT', encoding='utf-8')",
            "def load_profile(*a): return {'memory/README.md': ''}",
            "def recall(*a): return []"
        ]), encoding="utf-8")
        worker_scripts = self.proj_b / "team/Logistics/routeport-worker/scripts"
        try:
            import _winapi
            _winapi.CreateJunction(str(outside), str(worker_scripts))
        except (ImportError, OSError):
            worker_scripts.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(ValueError) as ctx:
            self.settings.read("routeport:routeport-worker", "memory")
        self.assertIn("Linked helper paths cannot be executed", str(ctx.exception))
        self.assertFalse(marker.exists())

    def test_unmocked_memory_successful_empty_lessons(self):
        worker_scripts = self.proj_b / "team/Logistics/routeport-worker/scripts"
        worker_scripts.mkdir(parents=True, exist_ok=True)
        helper_file = worker_scripts / "bot_mode.py"
        helper_file.write_text(chr(10).join([
            "def load_profile(project, bot_id):",
            "    return {'memory/README.md': 'Contract'}",
            "def recall(project, bot_id):",
            "    return []"
        ]), encoding="utf-8")
        res = self.settings.read("routeport:routeport-worker", "memory")
        self.assertTrue(res["readonly"])
        self.assertEqual(res["lessons"], [])
        self.assertIn("Current validated lessons: 0", res["content"])
        self.assertIn("No current validated lessons. This bot has no active learned memory.", res["content"])

    def test_save_version_conflict(self):
        before = self.settings.read("routeport:manager", "SOUL.md")
        self.settings.save("routeport:manager", "SOUL.md", "First edit", before["version"])
        with self.assertRaises(ValueError):
            self.settings.save("routeport:manager", "SOUL.md", "Stale edit", before["version"])

    def test_path_containment_and_symlinks_rejected(self):
        outside = self.base / "outside.md"
        outside.write_text("Secret", encoding="utf-8")
        link = self.proj_b / "agents/project-manager/SOUL.md"
        link.unlink()
        try:
            link.symlink_to(outside)
            with self.assertRaises(ValueError):
                self.settings.read("routeport:manager", "SOUL.md")
        except OSError:
            pass

    def test_non_writable_project_root_refuses_save(self):
        before = self.settings.read("routeport:manager", "SOUL.md")
        with patch("os.access", return_value=False):
            with self.assertRaises(ValueError):
                self.settings.save("routeport:manager", "SOUL.md", "Denied write", before["version"])


    def test_unknown_project_or_bot_refused(self):
        with self.assertRaises(ValueError):
            self.settings.read("manager", "SOUL.md", project="nonexistent")
        with self.assertRaises(ValueError):
            self.settings.read("routeport:ghost", "SOUL.md")

    def test_missing_file_raises_not_found(self):
        (self.proj_b / "agents/project-manager/SOUL.md").unlink()
        with self.assertRaises(FileNotFoundError):
            self.settings.read("routeport:manager", "SOUL.md")


class MultiProjectWebAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.proj_a = self.base / "proj_a"
        self.proj_b = self.base / "proj_b"
        self.proj_a.mkdir()
        self.proj_b.mkdir()

        (self.proj_a / ".bot-mode").mkdir(parents=True)
        roster_a = {"schema": 1, "project": {"id": "proj-a", "name": "Project A"}, "teams": [{"name": "T1"}], "participants": [
            {"bot": "manager", "role": "Manager", "workspace": "agents/project-manager", "status": "active"}
        ]}
        (self.proj_a / ".bot-mode" / "roster.json").write_text(json.dumps(roster_a), encoding="utf-8")
        (self.proj_a / "agents/project-manager").mkdir(parents=True)
        (self.proj_a / "agents/project-manager" / "SOUL.md").write_text("Soul A", encoding="utf-8")
        (self.proj_a / "agents/project-manager" / "PROFILE.md").write_text("- Bot ID: `project-manager`\nRole: Mgr A\n", encoding="utf-8")
        (self.proj_a / "agents/project-manager/memory").mkdir(parents=True, exist_ok=True)
        (self.proj_a / "agents/project-manager/memory/README.md").write_text("Memory Contract", encoding="utf-8")
        (self.proj_a / "scripts").mkdir(parents=True, exist_ok=True)
        (self.proj_a / "scripts" / "bot_mode.py").write_text(chr(10).join([
            "def load_profile(bot_id):",
            "    return {'memory/README.md': 'Contract'}",
            "def recall(p, b):",
            "    return []"
        ]), encoding="utf-8")

        (self.proj_b / ".bot-mode").mkdir(parents=True)
        roster_b = {"schema": 1, "project": {"id": "routeport", "name": "RoutePort"}, "teams": [{"name": "T2"}], "participants": [
            {"bot": "worker", "role": "Worker", "workspace": "team/worker", "status": "active"}
        ]}
        (self.proj_b / ".bot-mode" / "roster.json").write_text(json.dumps(roster_b), encoding="utf-8")
        (self.proj_b / "team/worker/agent").mkdir(parents=True)
        (self.proj_b / "team/worker/agent" / "SOUL.md").write_text("Worker Soul B", encoding="utf-8")
        (self.proj_b / "team/worker/agent" / "PROFILE.md").write_text("- Bot ID: `worker`\nRole: Worker B\n", encoding="utf-8")

        self.reg_file = self.base / "registry.json"
        reg_data = {"schema": 1, "projects": [
            {"id": "proj-a", "root": str(self.proj_a)},
            {"id": "routeport", "root": str(self.proj_b)}
        ]}
        self.reg_file.write_text(json.dumps(reg_data), encoding="utf-8")

        self.c = Controller(str(self.base / "runtime"), bots=["manager"], profile_project=self.proj_a, transport=FakeTransport(), start_worker=False)
        self.c.registry_file = self.reg_file
        # The server's read model worker writes under the served project; never under the real repository root.
        served.configure(self.proj_a)
        self.addCleanup(served.configure)
        self.server = create_server(self.c)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.token = re.search(r'name="room-token" content="([^"]+)"', self.request("GET", "/")[1].decode()).group(1)

    def test_server_never_serves_or_writes_the_real_repository_root(self):
        self.assertEqual(served.PROJECT, self.proj_a.resolve())
        self.assertNotEqual(served.PROJECT, ROOT)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        if hasattr(self.c, "rooms"): self.c.rooms.close()
        self.c.close(); self.temp.cleanup()

    def test_cold_agents_request_answers_before_a_slow_desktop_bridge(self):
        """#329: HTTP -> handler -> agent_visibility with a slow per-project Desktop read."""
        import time, types
        from unittest import mock
        from scripts.team_room import agent_visibility
        real = agent_visibility.build

        def slow(project_root, bridge=None, now=None):
            if bridge is not None:
                time.sleep(2.5)
            return real(project_root, bridge, now)

        agent_visibility._BUILDS.clear()
        self.c.project_managers = types.SimpleNamespace(bridge=object(), close=lambda: None)
        with mock.patch.object(agent_visibility, 'build', slow):
            started = time.monotonic()
            status, body = self.request("GET", "/api/registry/agents")
            self.assertEqual(status, 200)
            self.assertLess(time.monotonic() - started, 2.0)
            cold = json.loads(body)
            self.assertEqual(cold["enrichment"], "pending")
            self.assertIsInstance(cold["agents"], list)
            self.assertTrue(any("Unknown" in w for w in cold["source_warnings"]))
            for entry in list(agent_visibility._BUILDS.values()):
                entry["worker"].join(6)
            self.assertEqual(json.loads(self.request("GET", "/api/registry/agents")[1])["enrichment"], "fresh")

    def request(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(data) if data is not None else None
        h = {"Content-Type": "application/json", "X-Room-Token": getattr(self, "token", "")}
        h.update(headers or {})
        conn.request(method, path, body, h)
        r = conn.getresponse()
        result = (r.status, r.read())
        conn.close()
        return result

    def test_web_get_and_post_multi_project(self):
        # GET routeport:worker
        status, body = self.request("GET", "/api/settings/bots/routeport:worker?file=SOUL.md")
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["content"], "Worker Soul B")
        self.assertEqual(data["project_id"], "routeport")

        # GET with ?project=routeport
        status, body = self.request("GET", "/api/settings/bots/worker?file=SOUL.md&project=routeport")
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["content"], "Worker Soul B")

        # POST update
        status, body = self.request("POST", "/api/settings/bots/routeport:worker", {
            "file": "SOUL.md", "content": "Updated Soul B", "version": data["version"]
        })
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["content"], "Updated Soul B")
        self.assertEqual((self.proj_b / "team/worker/agent/SOUL.md").read_text(encoding="utf-8"), "Updated Soul B")

        # POST update with query param ?project=routeport
        status, body = self.request("POST", "/api/settings/bots/worker?project=routeport", {
            "file": "SOUL.md", "content": "Updated Soul B via query", "version": data["version"]
        })
        self.assertEqual(status, 200)
        self.assertEqual((self.proj_b / "team/worker/agent/SOUL.md").read_text(encoding="utf-8"), "Updated Soul B via query")

        # POST reject memory edit
        status, body = self.request("POST", "/api/settings/bots/routeport:worker", {
            "file": "memory", "content": "Bad", "version": data["version"]
        })
        self.assertEqual(status, 400)

    def test_web_conflicting_bot_key_and_project_rejected(self):
        # GET with conflicting ?project=proj-a
        status, body = self.request("GET", "/api/settings/bots/routeport:worker?file=SOUL.md&project=proj-a")
        self.assertEqual(status, 400)
        self.assertIn(b"Conflicting project selectors", body)

        # POST with conflicting query ?project=proj-a
        status, body = self.request("POST", "/api/settings/bots/routeport:worker?project=proj-a", {
            "file": "SOUL.md", "content": "Hacked", "version": "dummy"
        })
        self.assertEqual(status, 400)
        self.assertIn(b"Conflicting project selectors", body)

        # POST with conflicting body {"project": "proj-a"}
        status, body = self.request("POST", "/api/settings/bots/routeport:worker", {
            "file": "SOUL.md", "content": "Hacked", "version": "dummy", "project": "proj-a"
        })
        self.assertEqual(status, 400)
        self.assertIn(b"Conflicting project selectors", body)

        # Assert neither project was modified
        self.assertEqual((self.proj_a / "agents/project-manager/SOUL.md").read_text(encoding="utf-8"), "Soul A")
        self.assertEqual((self.proj_b / "team/worker/agent/SOUL.md").read_text(encoding="utf-8"), "Worker Soul B")

    def test_web_memory_missing_helper_returns_400(self):
        status, body = self.request("GET", "/api/settings/bots/routeport:worker?file=memory")
        self.assertEqual(status, 400)
        self.assertIn(b"Memory helper", body)

    def test_web_memory_junction_rejected_without_executing_helper(self):
        outside = self.base / "outside_web_scripts"
        outside.mkdir()
        marker = outside / "executed_web.txt"
        outside_helper = outside / "bot_mode.py"
        outside_helper.write_text(chr(10).join([
            "import pathlib",
            f"pathlib.Path(r'{marker}').write_text('EXPLOIT', encoding='utf-8')",
            "def load_profile(*a): return {'memory/README.md': ''}",
            "def recall(*a): return []"
        ]), encoding="utf-8")
        worker_scripts = self.proj_b / "team/worker/scripts"
        try:
            import _winapi
            _winapi.CreateJunction(str(outside), str(worker_scripts))
        except (ImportError, OSError):
            worker_scripts.symlink_to(outside, target_is_directory=True)

        status, body = self.request("GET", "/api/settings/bots/routeport:worker?file=memory")
        self.assertEqual(status, 400)
        self.assertIn(b"Linked helper paths cannot be executed", body)
        self.assertFalse(marker.exists())



    def test_frontend_agents_js_multi_project_contract(self):
        js = (ROOT / 'scripts/team_room/static/agents.js').read_text(encoding='utf-8')
        # Does not restrict profilePanel to currentProfile(a)
        self.assertNotIn('if(!currentProfile(a))', js)
        # Passes targetKey (a.bot_key || a.bot_id) to api
        self.assertIn("const targetKey=a.bot_key||a.bot_id;", js)
        self.assertIn("api('/api/settings/bots/'+encodeURIComponent(targetKey)+'?file='+encodeURIComponent(file))", js)
        self.assertIn("api('/api/settings/bots/'+encodeURIComponent(targetKey)", js)
        # Validates response identity with matchBot()
        self.assertIn("function matchBot(row)", js)
        self.assertIn("!matchBot(row)||row.file!==file", js)
        # Clean friendly note when profile is not installed or found
        self.assertIn("/not found|not installed/i.test(msg)", js)

    def test_frontend_agents_js_response_identity_guard(self):
        js = (ROOT / 'scripts/team_room/static/agents.js').read_text(encoding='utf-8')
        node_script = r'''
const js = require('fs').readFileSync(process.argv[1], 'utf8');
const matchBotSource = js.match(/function matchBot\(row\)\{[\s\S]*?return false;\s*\}/)[0];
const testMatch = (a, targetKey, row) => {
  const fn = new Function('a', 'targetKey', 'row', matchBotSource + '; return matchBot(row);');
  return fn(a, targetKey, row);
};

// 1. Current / local agent (is_current: true)
const currentAgent = {bot_key: 'alpha:dev', bot_id: 'dev', project_id: 'alpha', is_current: true};
const currentKey = 'alpha:dev';

if (!testMatch(currentAgent, currentKey, {bot: 'alpha:dev', project_id: 'alpha'})) throw new Error('Current: exact qualified key failed');
if (!testMatch(currentAgent, currentKey, {bot: 'dev', project_id: 'alpha'})) throw new Error('Current: bare bot with matching project failed');
if (!testMatch(currentAgent, currentKey, {bot: 'dev', project_id: 'current'})) throw new Error('Current: bare bot with current project failed');
if (!testMatch(currentAgent, currentKey, {bot: 'dev'})) throw new Error('Current: bare bot without project failed');
if (testMatch(currentAgent, currentKey, {bot: 'beta:dev', project_id: 'beta'})) throw new Error('Current: wrong qualified key should fail');
if (testMatch(currentAgent, currentKey, {bot: 'dev', project_id: 'beta'})) throw new Error('Current: wrong project_id should fail');

// 2. Foreign agent (is_current: false)
const foreignAgent = {bot_key: 'beta:dev', bot_id: 'dev', project_id: 'beta', is_current: false};
const foreignKey = 'beta:dev';

if (!testMatch(foreignAgent, foreignKey, {bot: 'beta:dev', project_id: 'beta'})) throw new Error('Foreign: exact qualified key failed');
if (!testMatch(foreignAgent, foreignKey, {bot: 'beta:dev'})) throw new Error('Foreign: exact qualified key without project_id failed');
if (!testMatch(foreignAgent, foreignKey, {bot: 'dev', project_id: 'beta'})) throw new Error('Foreign: bare bot with positive matching project failed');

// Critical security check: Foreign agent must NOT accept missing project or project_id: 'current'
if (testMatch(foreignAgent, foreignKey, {bot: 'dev'})) throw new Error('Foreign: bare bot without project must fail');
if (testMatch(foreignAgent, foreignKey, {bot: 'dev', project_id: 'current'})) throw new Error('Foreign: bare bot with current project must fail');
if (testMatch(foreignAgent, foreignKey, {bot: 'alpha:dev', project_id: 'alpha'})) throw new Error('Foreign: wrong project should fail');
if (testMatch(foreignAgent, foreignKey, {bot: 'other', project_id: 'beta'})) throw new Error('Foreign: wrong bot id should fail');
if (testMatch(foreignAgent, foreignKey, null)) throw new Error('Null response should fail');

console.log('PASS_MATCH_BOT');
''';
        res = subprocess.run(['node', '-e', node_script, str(ROOT / 'scripts/team_room/static/agents.js')],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, f"{res.stdout}\n{res.stderr}")
        self.assertIn('PASS_MATCH_BOT', res.stdout)


if __name__ == "__main__":
    unittest.main()
