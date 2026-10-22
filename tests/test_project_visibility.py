"""Targeted tests for project visibility selection and persistence (Issue #186)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from team_room import registry, roster


class ProjectVisibilityBackendTests(unittest.TestCase):
    def test_overview_includes_projects_with_roster_validity_and_current_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p1 = root / 'proj_ok'
            p1.mkdir()
            r1 = roster.empty(str(p1), 'ProjOk')
            roster.add_team(r1, 'Dev', 'Dev team', 'development')
            roster.add_participant(r1, 'dev1', 'Developer', 'Dev', 'team/Dev/dev1', '0aa00000-0000-7000-8000-000000000001')
            roster.save(str(p1), r1, roster.version(roster.load(str(p1))))

            p2 = root / 'proj_noteams'
            p2.mkdir()
            r2 = roster.empty(str(p2), 'ProjNoTeams')
            roster.save(str(p2), r2, roster.version(roster.load(str(p2))))

            p3 = root / 'proj_missing'

            p4 = root / 'proj_esc'
            p4.mkdir()
            outside = root / 'outside_roster.json'
            outside.write_text('{}', encoding='utf-8')
            sym = p4 / '.bot-mode/roster.json'
            sym.parent.mkdir(parents=True)
            try:
                sym.symlink_to(outside)
            except (OSError, NotImplementedError):
                pass

            p_curr = root / 'proj_current'
            p_curr.mkdir()
            r_curr = roster.empty(str(p_curr), 'CurrentServed')
            roster.save(str(p_curr), r_curr, roster.version(roster.load(str(p_curr))))

            config = {
                'schema': 1,
                'projects': [
                    {'id': 'p1', 'name': 'Project Ok', 'root': str(p1)},
                    {'id': 'p2', 'name': 'Project No Teams', 'root': str(p2)},
                    {'id': 'p3', 'name': 'Project Missing', 'root': str(p3)},
                    {'id': 'p4', 'name': 'Project Escape', 'root': str(p4)},
                ],
                'standalone': [
                    {'id': 'chief', 'name': 'Chief of Staff', 'thread_id': '0aa00000-0000-7000-8000-000000000099'}
                ]
            }
            cfile = root / 'registry.json'
            cfile.write_text(json.dumps(config), encoding='utf-8')

            # Pass current_project_root which is NOT in registry.json -> should be explicitly handled
            overview = registry.build_overview(str(cfile), current_project_root=str(p_curr))
            self.assertEqual(overview['status'], 'ok')
            projects = {p['id']: p for p in overview['projects']}

            self.assertIn('p1', projects)
            self.assertEqual(projects['p1']['status'], 'ok')
            self.assertTrue(projects['p1']['applicable'])
            self.assertFalse(projects['p1']['is_current'])
            self.assertIsNone(projects['p1']['reason'])

            self.assertIn('p2', projects)
            self.assertEqual(projects['p2']['status'], 'no_teams')
            self.assertTrue(projects['p2']['applicable'])
            self.assertFalse(projects['p2']['is_current'])
            self.assertIn('No active teams', projects['p2']['reason'])

            self.assertIn('p3', projects)
            self.assertEqual(projects['p3']['status'], 'source_unavailable')
            self.assertFalse(projects['p3']['applicable'])
            self.assertIn('missing', projects['p3']['reason'].lower())

            if sym.is_symlink():
                self.assertIn('p4', projects)
                self.assertEqual(projects['p4']['status'], 'roster_error')
                self.assertFalse(projects['p4']['applicable'])
                self.assertIn('escape', projects['p4']['reason'].lower())

            # Current project explicitly handled
            self.assertIn('current', projects)
            self.assertTrue(projects['current']['is_current'])
            self.assertTrue(projects['current']['applicable'])

    def test_unconfigured_registry_includes_current_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            p_curr = Path(tmp) / 'curr'
            p_curr.mkdir()
            r_curr = roster.empty(str(p_curr), 'OnlyCurrent')
            roster.save(str(p_curr), r_curr, roster.version(roster.load(str(p_curr))))

            overview = registry.build_overview(None, current_project_root=str(p_curr))
            self.assertEqual(overview['status'], 'not_configured')
            self.assertEqual(len(overview['projects']), 1)
            curr = overview['projects'][0]
            self.assertTrue(curr['is_current'])
            self.assertTrue(curr['applicable'])
            self.assertEqual(curr['name'], 'OnlyCurrent')


class ProjectVisibilityFrontendContractTests(unittest.TestCase):
    def test_frontend_visibility_logic_in_node(self):
        node_script = r'''
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const projectsJsPath = path.resolve(process.argv[2]);
const roomJsPath = path.resolve(process.argv[3]);

let mockStorage = {};
const mockLocalStorage = {
  getItem: (k) => mockStorage[k] || null,
  setItem: (k, v) => { mockStorage[k] = String(v); },
  removeItem: (k) => { delete mockStorage[k]; },
  clear: () => { mockStorage = {}; }
};

const domListeners = {};
const elements = {};
function getEl(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      className: '',
      dataset: {},
      style: {},
      replaceChildren: function() { this.children = []; },
      children: [],
      append: function(...args) { this.children.push(...args); },
      setAttribute: function(k, v) { this[k] = v; },
      addEventListener: function(ev, fn) {},
      value: ''
    };
  }
  return elements[id];
}

const context = {
  window: {
    addEventListener: (ev, fn) => { domListeners[ev] = fn; }
  },
  document: {
    querySelector: (sel) => {
      if (sel.includes('room-token')) return { content: 'test-token' };
      return getEl(sel);
    },
    querySelectorAll: (sel) => [],
    getElementById: (id) => getEl(id),
    createElement: (tag) => ({
      tagName: tag,
      className: '',
      dataset: {},
      style: {},
      replaceChildren: function() { this.children = []; },
      children: [],
      append: function(...args) { this.children.push(...args); },
      setAttribute: function(k, v) { this[k] = v; },
      querySelectorAll: function() { return []; }
    })
  },
  localStorage: mockLocalStorage,
  setInterval: () => {},
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
  console
};
context.window.window = context.window;
context.window.__TAURI__ = { core: { invoke: () => Promise.resolve({}) } };

vm.createContext(context);

// Load room.js
const roomCode = fs.readFileSync(roomJsPath, 'utf-8');
vm.runInContext(roomCode, context);

// Load projects.js
const projectsCode = fs.readFileSync(projectsJsPath, 'utf-8');
vm.runInContext(projectsCode, context);

const officeProjects = context.window.officeProjects;
if (!officeProjects) throw new Error('officeProjects not exposed on window');

// Test 1: Initial state - no filter saved in localStorage -> all projects visible
if (officeProjects.getVisibleProjectIds() !== null) throw new Error('Initial visible ids must be null (all visible)');
if (!officeProjects.isProjectVisible('alpha')) throw new Error('alpha must be visible by default');
if (!officeProjects.isProjectVisible('beta')) throw new Error('beta must be visible by default');

// Test 2: Set selection
officeProjects.setVisibleProjectIds(['alpha']);
if (officeProjects.getVisibleProjectIds() === null) throw new Error('Visible ids must not be null after set');
if (!officeProjects.isProjectVisible('alpha')) throw new Error('alpha must be visible');
if (officeProjects.isProjectVisible('beta')) throw new Error('beta must be hidden');
if (mockStorage['office_visible_projects'] !== JSON.stringify(['alpha'])) throw new Error('localStorage mismatch');

// Test 3: Restore all
officeProjects.setVisibleProjectIds(null);
if (officeProjects.getVisibleProjectIds() !== null) throw new Error('Restore all must clear visible ids to null');
if (!officeProjects.isProjectVisible('alpha')) throw new Error('alpha must be visible after restore');
if (!officeProjects.isProjectVisible('beta')) throw new Error('beta must be visible after restore');
if (mockStorage['office_visible_projects']) throw new Error('localStorage must not have key after restore');

// Test 4: Directory bots and teams filtering integration
const mockBots = [
  { bot_key: 'alpha:dev', bot_id: 'dev', name: 'Alpha Dev', project_id: 'alpha' },
  { bot_key: 'beta:dev', bot_id: 'dev', name: 'Beta Dev', project_id: 'beta' },
  { bot_key: 'standalone:chief', bot_id: 'chief', name: 'Chief of Staff', project_id: null }
];

function filterBots(bots) {
  return bots.filter(b => {
    if (b.project_id && officeProjects && !officeProjects.isProjectVisible(b.project_id)) return false;
    return true;
  });
}

// When all visible:
if (filterBots(mockBots).length !== 3) throw new Error('All bots must be included when no filter');

// When only alpha visible:
officeProjects.setVisibleProjectIds(['alpha']);
const filtered = filterBots(mockBots);
if (filtered.length !== 2) throw new Error('Filtered bots must include alpha bot and standalone bot');
if (!filtered.some(b => b.project_id === 'alpha')) throw new Error('Alpha bot missing');
if (!filtered.some(b => b.project_id === null)) throw new Error('Standalone bot missing');
if (filtered.some(b => b.project_id === 'beta')) throw new Error('Beta bot must be excluded');

// Restore
officeProjects.setVisibleProjectIds(null);
if (filterBots(mockBots).length !== 3) throw new Error('All bots must return after restore');

// Test 5: Agents.js shell dropdown & renderCards filtering
context.window.officeAgents.open();

console.log('NODE_VISIBILITY_CONTRACT_PASS');
console.log('NODE_VISIBILITY_CONTRACT_PASS');
''';
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
            f.write(node_script)
            tmp_js = f.name
        try:
            res = subprocess.run([
                'node', tmp_js,
                str(ROOT / 'scripts/team_room/static/projects.js'),
                str(ROOT / 'scripts/team_room/static/room.js')
            ], capture_output=True, text=True, timeout=10)
            self.assertEqual(res.returncode, 0, f"{res.stdout}\n{res.stderr}")
            self.assertIn('NODE_VISIBILITY_CONTRACT_PASS', res.stdout)
        finally:
            Path(tmp_js).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()

"""Targeted tests for project visibility selection and persistence (Issue #186)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from team_room import registry, roster


class ProjectVisibilityBackendTests(unittest.TestCase):
    def test_overview_includes_projects_with_roster_validity_and_current_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p1 = root / 'proj_ok'
            p1.mkdir()
            r1 = roster.empty(str(p1), 'ProjOk')
            roster.add_team(r1, 'Dev', 'Dev team', 'development')
            roster.add_participant(r1, 'dev1', 'Developer', 'Dev', 'team/Dev/dev1', '0aa00000-0000-7000-8000-000000000001')
            roster.save(str(p1), r1, roster.version(roster.load(str(p1))))

            p2 = root / 'proj_noteams'
            p2.mkdir()
            r2 = roster.empty(str(p2), 'ProjNoTeams')
            roster.save(str(p2), r2, roster.version(roster.load(str(p2))))

            p3 = root / 'proj_missing'

            p4 = root / 'proj_esc'
            p4.mkdir()
            outside = root / 'outside_roster.json'
            outside.write_text('{}', encoding='utf-8')
            sym = p4 / '.bot-mode/roster.json'
            sym.parent.mkdir(parents=True)
            try:
                sym.symlink_to(outside)
            except (OSError, NotImplementedError):
                pass

            p_curr = root / 'proj_current'
            p_curr.mkdir()
            r_curr = roster.empty(str(p_curr), 'CurrentServed')
            roster.save(str(p_curr), r_curr, roster.version(roster.load(str(p_curr))))

            config = {
                'schema': 1,
                'projects': [
                    {'id': 'p1', 'name': 'Project Ok', 'root': str(p1)},
                    {'id': 'p2', 'name': 'Project No Teams', 'root': str(p2)},
                    {'id': 'p3', 'name': 'Project Missing', 'root': str(p3)},
                    {'id': 'p4', 'name': 'Project Escape', 'root': str(p4)},
                ],
                'standalone': [
                    {'id': 'chief', 'name': 'Chief of Staff', 'thread_id': '0aa00000-0000-7000-8000-000000000099'}
                ]
            }
            cfile = root / 'registry.json'
            cfile.write_text(json.dumps(config), encoding='utf-8')

            # Pass current_project_root which is NOT in registry.json -> should be explicitly handled
            overview = registry.build_overview(str(cfile), current_project_root=str(p_curr))
            self.assertEqual(overview['status'], 'ok')
            projects = {p['id']: p for p in overview['projects']}

            self.assertIn('p1', projects)
            self.assertEqual(projects['p1']['status'], 'ok')
            self.assertTrue(projects['p1']['applicable'])
            self.assertFalse(projects['p1']['is_current'])
            self.assertIsNone(projects['p1']['reason'])

            self.assertIn('p2', projects)
            self.assertEqual(projects['p2']['status'], 'no_teams')
            self.assertTrue(projects['p2']['applicable'])
            self.assertFalse(projects['p2']['is_current'])
            self.assertIn('No active teams', projects['p2']['reason'])

            self.assertIn('p3', projects)
            self.assertEqual(projects['p3']['status'], 'source_unavailable')
            self.assertFalse(projects['p3']['applicable'])
            self.assertIn('missing', projects['p3']['reason'].lower())

            if sym.is_symlink():
                self.assertIn('p4', projects)
                self.assertEqual(projects['p4']['status'], 'roster_error')
                self.assertFalse(projects['p4']['applicable'])
                self.assertIn('escape', projects['p4']['reason'].lower())

            # Current project explicitly handled
            self.assertIn('current', projects)
            self.assertTrue(projects['current']['is_current'])
            self.assertTrue(projects['current']['applicable'])

    def test_unconfigured_registry_includes_current_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            p_curr = Path(tmp) / 'curr'
            p_curr.mkdir()
            r_curr = roster.empty(str(p_curr), 'OnlyCurrent')
            roster.save(str(p_curr), r_curr, roster.version(roster.load(str(p_curr))))

            overview = registry.build_overview(None, current_project_root=str(p_curr))
            self.assertEqual(overview['status'], 'not_configured')
            self.assertEqual(len(overview['projects']), 1)
            curr = overview['projects'][0]
            self.assertTrue(curr['is_current'])
            self.assertTrue(curr['applicable'])
            self.assertEqual(curr['name'], 'OnlyCurrent')


class ProjectVisibilityFrontendContractTests(unittest.TestCase):
    def test_frontend_visibility_logic_in_node(self):
        node_script = r'''
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const projectsJsPath = path.resolve(process.argv[2]);
const roomJsPath = path.resolve(process.argv[3]);
const agentsJsPath = path.resolve(process.argv[4]);

let mockStorage = {};
const mockLocalStorage = {
  getItem: (k) => mockStorage[k] || null,
  setItem: (k, v) => { mockStorage[k] = String(v); },
  removeItem: (k) => { delete mockStorage[k]; },
  clear: () => { mockStorage = {}; }
};

const domListeners = {};
const elements = {};
function getEl(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      className: '',
      dataset: {},
      style: {},
      replaceChildren: function() { this.children = []; },
      children: [],
      append: function(...args) { this.children.push(...args); },
      setAttribute: function(k, v) { this[k] = v; },
      addEventListener: function(ev, fn) {},
      value: ''
    };
  }
  return elements[id];
}

const context = {
  window: {
    addEventListener: (ev, fn) => { domListeners[ev] = fn; }
  },
  document: {
    querySelector: (sel) => {
      if (sel.includes('room-token')) return { content: 'test-token' };
      return getEl(sel);
    },
    querySelectorAll: (sel) => [],
    getElementById: (id) => getEl(id),
    createElement: (tag) => ({
      tagName: tag,
      className: '',
      dataset: {},
      style: {},
      replaceChildren: function() { this.children = []; },
      children: [],
      append: function(...args) { this.children.push(...args); },
      setAttribute: function(k, v) { this[k] = v; },
      addEventListener: function(ev, fn) {},
      querySelectorAll: function() { return []; }
    }),
    body: {
      append: () => {}
    }
  },
  localStorage: mockLocalStorage,
  setInterval: () => {},
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
  console
};
context.window.window = context.window;
context.window.__TAURI__ = { core: { invoke: () => Promise.resolve({}) } };

vm.createContext(context);

// Load room.js
const roomCode = fs.readFileSync(roomJsPath, 'utf-8');
vm.runInContext(roomCode, context);

// Load projects.js
const projectsCode = fs.readFileSync(projectsJsPath, 'utf-8');
vm.runInContext(projectsCode, context);

// Load agents.js
const agentsCode = fs.readFileSync(agentsJsPath, 'utf-8');
vm.runInContext(agentsCode, context);

const officeProjects = context.window.officeProjects;
if (!officeProjects) throw new Error('officeProjects not exposed on window');

// Test 1: Initial state - no filter saved in localStorage -> all projects visible
if (officeProjects.getVisibleProjectIds() !== null) throw new Error('Initial visible ids must be null (all visible)');
if (!officeProjects.isProjectVisible('alpha')) throw new Error('alpha must be visible by default');
if (!officeProjects.isProjectVisible('beta')) throw new Error('beta must be visible by default');

// Test 2: Set selection
officeProjects.setVisibleProjectIds(['alpha']);
if (officeProjects.getVisibleProjectIds() === null) throw new Error('Visible ids must not be null after set');
if (!officeProjects.isProjectVisible('alpha')) throw new Error('alpha must be visible');
if (officeProjects.isProjectVisible('beta')) throw new Error('beta must be hidden');
if (mockStorage['office_visible_projects'] !== JSON.stringify(['alpha'])) throw new Error('localStorage mismatch');

// Test 3: Restore all
officeProjects.setVisibleProjectIds(null);
if (officeProjects.getVisibleProjectIds() !== null) throw new Error('Restore all must clear visible ids to null');
if (!officeProjects.isProjectVisible('alpha')) throw new Error('alpha must be visible after restore');
if (!officeProjects.isProjectVisible('beta')) throw new Error('beta must be visible after restore');
if (mockStorage['office_visible_projects']) throw new Error('localStorage must not have key after restore');

// Test 4: Directory bots and teams filtering integration
const mockBots = [
  { bot_key: 'alpha:dev', bot_id: 'dev', name: 'Alpha Dev', project_id: 'alpha' },
  { bot_key: 'beta:dev', bot_id: 'dev', name: 'Beta Dev', project_id: 'beta' },
  { bot_key: 'standalone:chief', bot_id: 'chief', name: 'Chief of Staff', project_id: null }
];

function filterBots(bots) {
  return bots.filter(b => {
    if (b.project_id && officeProjects && !officeProjects.isProjectVisible(b.project_id)) return false;
    return true;
  });
}

if (filterBots(mockBots).length !== 3) throw new Error('All bots must be included when no filter');

officeProjects.setVisibleProjectIds(['alpha']);
const filtered = filterBots(mockBots);
if (filtered.length !== 2) throw new Error('Filtered bots must include alpha bot and standalone bot');
if (!filtered.some(b => b.project_id === 'alpha')) throw new Error('Alpha bot missing');
if (!filtered.some(b => b.project_id === null)) throw new Error('Standalone bot missing');
if (filtered.some(b => b.project_id === 'beta')) throw new Error('Beta bot must be excluded');

// Restore
officeProjects.setVisibleProjectIds(null);
if (filterBots(mockBots).length !== 3) throw new Error('All bots must return after restore');

console.log('NODE_VISIBILITY_CONTRACT_PASS');
''';
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
            f.write(node_script)
            tmp_js = f.name
        try:
            res = subprocess.run([
                'node', tmp_js,
                str(ROOT / 'scripts/team_room/static/projects.js'),
                str(ROOT / 'scripts/team_room/static/room.js'),
                str(ROOT / 'scripts/team_room/static/agents.js')
            ], capture_output=True, text=True, timeout=10)
            self.assertEqual(res.returncode, 0, str(res.stdout) + str(res.stderr))
            self.assertIn('NODE_VISIBILITY_CONTRACT_PASS', res.stdout)
        finally:
            Path(tmp_js).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()

