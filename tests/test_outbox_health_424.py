"""The Office delivery panel (#424, S4-D): read-only model, API endpoint and the panel script."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.team_room import outbox_health, supervisor_status

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'scripts' / 'team_room' / 'static'
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
A, B = 'a' * 32, 'b' * 32  # row ids of the decision tests
PROJECT = {'project': 'X:/secret/path/alpha', 'name': 'alpha', 'pending': 2, 'oldest_pending_seconds': 150, 'ambiguous': 1, 'needs_attention': 3,
           'relay_reachable': True, 'pump_alive': True, 'last_delivered_at': (NOW - timedelta(minutes=5)).isoformat(), 'problems': ['p1']}


def document(**changes):
    data = {'schema': 1, 'at': (NOW - timedelta(seconds=30)).isoformat(), 'pass_seconds': 1.5, 'interval_seconds': 120,
            'supervisor': {'pid': 5, 'version': 'abc123def456'}, 'desktop_running': True, 'office_ok': True,
            'relay': {'ok': True, 'manager_changed': False,
                      'last_restart': {'at': (NOW - timedelta(hours=1)).isoformat(), 'reason': 'relay down', 'ok': True, 'detail': 'started'}},
            'last_pump_action': {'at': NOW.isoformat(), 'project': 'X:/secret/path/alpha', 'action': 'pump started', 'reason': 'work was waiting'},
            'projects': [PROJECT], 'problems': []}
    data.update(changes)
    return data


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def put(self, data=None, health=None):
        supervisor_status.write(self.root / '.bot-mode' / 'supervisor-status.json', document() if data is None else data)
        if health is not None:
            (self.root / '.bot-mode' / 'relay-health.json').write_text(json.dumps(health), encoding='utf-8')

    def build(self):
        return outbox_health.build(self.root, NOW)

    def test_no_file_is_unknown_with_a_reason_and_no_numbers(self):
        model = self.build()
        self.assertEqual((model['state'], model['projects'], model['relay'], model['incidents']), ('unknown', [], None, []))
        self.assertIn('has not written', model['reason'])

    def test_a_fresh_healthy_document_is_ok_with_the_panel_fields(self):
        self.put()
        model = self.build()
        self.assertEqual((model['schema'], model['state'], model['reason'], model['age_seconds']), (1, 'ok', 'healthy', 30))
        self.assertEqual(model['relay']['ok'], True)
        self.assertEqual(model['relay']['last_restart']['reason'], 'relay down')
        self.assertEqual(model['supervisor'], {'at': document()['at'], 'interval_seconds': 120, 'pass_seconds': 1.5, 'version': 'abc123def456'})
        row = model['projects'][0]
        self.assertEqual((row['name'], row['pending'], row['oldest_pending_seconds'], row['ambiguous'], row['needs_attention'], row['pump_alive']),
                         ('alpha', 2, 150, 1, 3, True))
        self.assertEqual(row['last_delivery_age_seconds'], 300)

    def test_problems_make_the_state_problems_and_are_listed(self):
        self.put(document(problems=['alpha: something']))
        model = self.build()
        self.assertEqual((model['state'], model['problems']), ('problems', ['alpha: something']))

    def test_an_old_file_is_unknown_and_shows_no_old_numbers(self):
        self.put(document(at=(NOW - timedelta(minutes=10)).isoformat()))
        model = self.build()
        self.assertEqual((model['state'], model['projects'], model['relay']), ('unknown', [], None))
        self.assertIn('may not be running', model['reason'])

    def test_no_file_path_leaves_the_module(self):
        self.put()
        text = json.dumps(self.build())
        self.assertNotIn('secret', text)
        self.assertNotIn('X:/', text)
        self.assertIsNone(self.build()['last_pump_action'].get('project'))
        self.assertEqual(self.build()['last_pump_action']['action'], 'pump started')

    def test_wrong_types_become_unknown_values_not_errors(self):
        self.put(document(projects=[{'name': 5, 'pending': 'many', 'pump_alive': 'yes', 'last_delivered_at': 'yesterday', 'problems': 'x'}, 'junk'],
                          relay='broken', last_pump_action='broken', problems='broken'))
        model = self.build()
        row = model['projects'][0]
        self.assertEqual((row['name'], row['pending'], row['pump_alive'], row['last_delivery_age_seconds'], row['problems']), ('?', None, None, None, []))
        self.assertEqual(len(model['projects']), 1)
        self.assertEqual((model['relay']['ok'], model['last_pump_action'], model['problems']), (False, None, []))

    def test_incidents_come_from_a_fresh_health_file_only(self):
        incident = {'key': 'relay-down', 'severity': 'owner', 'opened': NOW.isoformat(), 'reopened': 1, 'text': 'down', 'remedy': 'restart', 'clearing': False,
                    'internal': 'x'}
        self.put(health={'at': (NOW - timedelta(seconds=20)).isoformat(), 'incidents': [incident, {'no': 'key'}, 'junk']})
        rows = self.build()['incidents']
        self.assertEqual([r['key'] for r in rows], ['relay-down'])
        self.assertNotIn('internal', rows[0])
        self.put(health={'at': (NOW - timedelta(hours=1)).isoformat(), 'incidents': [incident]})
        self.assertEqual(self.build()['incidents'], [])
        self.put(health={'at': NOW.isoformat()})
        self.assertEqual(self.build()['incidents'], [])
        (self.root / '.bot-mode' / 'relay-health.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(self.build()['incidents'], [])

    def test_the_canary_of_a_project_reaches_the_model_with_unknown_kept_unknown(self):
        facts = {'last_ok_at': (NOW - timedelta(minutes=3)).isoformat(), 'last_latency_seconds': 1.5, 'last_read_seconds': 0.2,
                 'last_chat_status': 'idle', 'pending_seconds': None, 'secret': 'x'}
        self.put(document(projects=[{**PROJECT, 'canary': facts}, {**PROJECT, 'name': 'beta', 'canary': None},
                                    {**PROJECT, 'name': 'gamma', 'canary': {'last_latency_seconds': 'fast', 'pending_seconds': True}}]))
        first, second, third = self.build()['projects']
        self.assertEqual(first['canary'], {'last_ok_age_seconds': 180, 'last_latency_seconds': 1.5, 'last_chat_status': 'idle', 'pending_seconds': None})
        self.assertIsNone(second['canary'])
        self.assertEqual((third['canary']['last_latency_seconds'], third['canary']['pending_seconds'], third['canary']['last_ok_age_seconds']), (None, None, None))

    def test_a_failed_incident_bookkeeping_reaches_the_model(self):
        self.put(document(incidents_error='KeyError: opened'))
        self.assertEqual(self.build()['incidents_error'], 'KeyError: opened')
        self.put(document())
        self.assertIsNone(self.build()['incidents_error'])
        self.put(document(incidents_error=5))
        self.assertIsNone(self.build()['incidents_error'])

    def test_building_the_model_changes_nothing_on_disk(self):
        self.put()
        before = sorted((p.name, p.stat().st_mtime_ns) for p in (self.root / '.bot-mode').iterdir())
        self.build()
        self.assertEqual(sorted((p.name, p.stat().st_mtime_ns) for p in (self.root / '.bot-mode').iterdir()), before)


HARNESS = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const staticDir = process.argv[2];
const spec = JSON.parse(process.argv[3]);
class FakeNode {
  constructor(tag) { this.tagName = tag; this.className = ''; this.dataset = {}; this.children = []; this.textContent = ''; this.attrs = {}; this.parent = null; this.handlers = {}; }
  addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); }
  append(...items) { for (const item of items) { if (typeof item === 'string') { const t = new FakeNode('#text'); t.textContent = item; item = t; } item.parent = this; this.children.push(item); } }
  setAttribute(k, v) { this.attrs[k] = v; }
  replaceWith(node) { const i = this.parent.children.indexOf(this); node.parent = this.parent; this.parent.children[i] = node; panels.splice(panels.indexOf(this), 1, node); }
}
const text = n => n.textContent + n.children.map(text).join(' ');
const find = (n, cls) => (n.className === cls ? [n] : []).concat(...n.children.map(c => find(c, cls)));
const panels = [];
const calls = [];
const store = {};
let nextReply = null;
let rowsReply = {};
let resolveReply = null;
const context = {
  window: { addEventListener: () => {}, dispatchEvent: () => true },
  document: {
    documentElement: {}, createElement: tag => { const n = new FakeNode(tag); return n; },
    querySelectorAll: sel => sel === '.delivery-panel' ? panels.slice() : [], querySelector: sel => sel === '.delivery-panel' ? panels[0] || null : null
  },
  localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); } },
  setInterval: () => 0, console, Event,
  api: async (url, options) => {
    calls.push({ url, options: options || null, body: options && options.body ? JSON.parse(options.body) : null });
    let r;
    if (url.startsWith('/api/outbox-rows')) { const name = decodeURIComponent(url.split('project=')[1]); r = rowsReply[name] !== undefined ? rowsReply[name] : { project: name, rows: [], cut: false, limit: 20 }; }
    else if (url === '/api/outbox-resolve') r = resolveReply;
    else r = nextReply;
    if (r instanceof Error) throw r;
    if (r && r.__error) { const failure = new Error(r.__error); if (r.status) failure.status = r.status; throw failure; }
    return r;
  }
};
context.window.window = context.window;
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(staticDir, 'i18n.js'), 'utf-8'), context);
context.officeI18n = context.window.officeI18n;
const originalCreate = context.document.createElement;
context.document.createElement = tag => { const n = originalCreate(tag); return n; };
vm.runInContext(fs.readFileSync(path.join(staticDir, 'delivery.js'), 'utf-8'), context);
const panel = context.window.officeDelivery;
const results = [];
(async () => {
  for (const step of spec) {
    if (step.language) { context.officeI18n.setLanguage(step.language); }
    if (step.reply !== undefined) nextReply = step.reply === 'ERROR' ? new Error('boom') : step.reply;
    if (step.rows !== undefined) rowsReply = step.rows;
    if (step.resolve !== undefined) resolveReply = step.resolve;
    const parent = new FakeNode('aside');
    panels.length = 0;
    const before = panel.build();
    parent.append(before); panels.push(before); parent.children.length = 0; panels.length = 0;
    panel.paint(parent); panels.push(parent.children[0]);
    const loading = text(parent.children[0]);
    await panel.refresh(); await new Promise(r => setTimeout(r, 0));
    const clickTexts = [];
    for (const click of step.clicks || []) {
      const inRow = node => { for (let n = node; n; n = n.parent) if (n.dataset && n.dataset.row === click.row) return true; return false; };
      const targets = find(parent.children[0], click.cls).filter(node => !click.row || inRow(node));
      if (!targets.length) throw new Error('no ' + click.cls + ' button to click');
      for (const fn of targets[0].handlers.click || []) fn();
      for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0));
      clickTexts.push(text(parent.children[0]));
    }
    const shown = parent.children[0];
    const count = cls => find(shown, cls).length;
    results.push({ name: step.name, loading, text: text(shown), state: shown.dataset.state, rows: find(shown, 'delivery-project').length,
                   incidents: find(shown, 'delivery-incident').length, calls: calls.splice(0), clickTexts,
                   buttons: { retry: count('delivery-retry'), skip: count('delivery-skip'), yes: count('delivery-confirm-yes'), no: count('delivery-confirm-no') } });
  }
  process.stdout.write(JSON.stringify(results));
})();
"""


@unittest.skipUnless(shutil.which('node'), 'node is needed to run the panel script')
class PanelScriptTests(unittest.TestCase):
    def run_steps(self, steps):
        with tempfile.TemporaryDirectory() as folder:
            harness = Path(folder) / 'harness.js'
            harness.write_text(HARNESS, encoding='utf-8')
            result = subprocess.run(['node', str(harness), str(STATIC), json.dumps(steps)], capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {row['name']: row for row in json.loads(result.stdout)}

    def model(self, **changes):
        base = {'schema': 1, 'observed_at': NOW.isoformat(), 'state': 'ok', 'reason': 'healthy', 'age_seconds': 30,
                'supervisor': {'at': NOW.isoformat(), 'interval_seconds': 120, 'pass_seconds': 1, 'version': 'v'}, 'desktop_running': True, 'office_ok': True,
                'relay': {'ok': True, 'manager_changed': False, 'last_restart': {'at': NOW.isoformat(), 'action': None, 'reason': 'relay down', 'detail': 'x', 'ok': True}},
                'last_pump_action': {'at': NOW.isoformat(), 'action': 'pump started', 'reason': 'r', 'detail': None, 'ok': None},
                'projects': [{'name': 'alpha', 'pending': 2, 'oldest_pending_seconds': 150, 'ambiguous': 1, 'needs_attention': 3, 'relay_reachable': True,
                              'pump_alive': True, 'last_delivery_age_seconds': 300, 'problems': []},
                             {'name': 'beta', 'pending': 0, 'oldest_pending_seconds': None, 'ambiguous': 0, 'needs_attention': 0, 'relay_reachable': True,
                              'pump_alive': False, 'last_delivery_age_seconds': None, 'problems': []}],
                'problems': [], 'incidents': [], 'incidents_error': None}
        base.update(changes)
        return base

    def test_a_healthy_model_shows_relay_projects_ages_and_never_posts(self):
        rows = self.run_steps([{'name': 'ok', 'reply': self.model()}])
        row = rows['ok']
        self.assertIn('Loading delivery status', row['loading'])
        self.assertEqual((row['state'], row['rows']), ('ok', 2))
        for expected in ('Healthy', 'Checked 30 s ago', 'Relay up', 'Last restart: relay down', 'worked', '2 waiting', 'oldest 3 min',
                         'Last delivery 5 min ago', 'Pump running', '3 need attention', '1 unconfirmed', 'Nothing waiting', 'No delivery recorded', 'Pump stopped',
                         'Last pump action: pump started'):
            self.assertIn(expected, row['text'])
        self.assertEqual([c['url'] for c in row['calls']], ['/api/outbox-health', '/api/outbox-rows?project=alpha'])
        self.assertEqual([c['options'] for c in row['calls']], [None, None])  # plain GETs: showing the panel changes nothing

    def test_unknown_shows_the_reason_and_no_numbers_or_projects(self):
        rows = self.run_steps([{'name': 'unknown', 'reply': self.model(state='unknown', reason='the supervisor last ran 900 s ago', age_seconds=900,
                                                                     relay=None, projects=[], supervisor=None, last_pump_action=None)}])
        row = rows['unknown']
        self.assertEqual((row['state'], row['rows']), ('unknown', 0))
        self.assertIn('Unknown', row['text'])
        self.assertIn('the supervisor last ran 900 s ago', row['text'])
        self.assertNotIn('Healthy', row['text'])
        self.assertNotIn('Relay up', row['text'])
        self.assertNotIn('Nothing waiting', row['text'])

    def test_problems_and_incidents_are_listed(self):
        incident = {'key': 'needs-attention:alpha', 'severity': 'manager', 'opened': NOW.isoformat(), 'reopened': 0, 'text': '3 reports need a decision',
                    'remedy': 'outbox.py resolve', 'clearing': False}
        rows = self.run_steps([{'name': 'bad', 'reply': self.model(state='problems', problems=['alpha: 3 reports need a decision'], incidents=[incident])}])
        row = rows['bad']
        self.assertEqual((row['state'], row['incidents']), ('problems', 1))
        self.assertIn('Problems', row['text'])
        self.assertIn('alpha: 3 reports need a decision', row['text'])
        self.assertIn('needs-attention:alpha', row['text'])
        self.assertIn('Remedy: outbox.py resolve', row['text'])

    def test_a_failed_or_malformed_reply_is_unavailable_never_healthy(self):
        rows = self.run_steps([{'name': 'error', 'reply': 'ERROR'}, {'name': 'schema', 'reply': {'schema': 2, 'state': 'ok'}},
                               {'name': 'state', 'reply': self.model(state='fine')}])
        for name in ('error', 'schema', 'state'):
            self.assertEqual(rows[name]['state'], 'unknown', name)
            self.assertIn('Delivery status unavailable', rows[name]['text'])
            self.assertNotIn('Healthy', rows[name]['text'])
            self.assertEqual(rows[name]['rows'], 0)

    def test_text_comes_from_the_server_as_text_and_the_only_write_is_the_confirmed_decision(self):
        source = (STATIC / 'delivery.js').read_text(encoding='utf-8')
        self.assertNotIn('innerHTML', source)
        self.assertNotIn('method:"POST"', source)
        self.assertEqual(source.count("method:'POST'"), 1)  # one POST, and it goes only to the decision endpoint
        self.assertEqual(source.count("'/api/outbox-resolve'"), 1)
        self.assertEqual(source.count('confirm:true'), 1)
        for word in ('/desktop', 'send_message', 'fetch(', 'XMLHttpRequest'):
            self.assertNotIn(word, source)

    def test_the_canary_tag_shows_latency_waiting_or_unknown(self):
        def project(name, canary):
            return {'name': name, 'pending': 0, 'oldest_pending_seconds': None, 'ambiguous': 0, 'needs_attention': 0, 'relay_reachable': True,
                    'pump_alive': True, 'last_delivery_age_seconds': 60, 'problems': [], 'canary': canary}
        ok = {'last_ok_age_seconds': 180, 'last_latency_seconds': 1.54, 'last_chat_status': 'idle', 'pending_seconds': None}
        waiting = {**ok, 'pending_seconds': 400}
        odd_status = {**ok, 'last_chat_status': 'systemError'}
        no_status = {**ok, 'last_chat_status': None}
        bad_time = {**ok, 'last_ok_age_seconds': None}
        rows = self.run_steps([{'name': 'ok', 'reply': self.model(projects=[project('a', ok)])},
                               {'name': 'waiting', 'reply': self.model(projects=[project('a', waiting)])},
                               {'name': 'none', 'reply': self.model(projects=[project('a', None)])},
                               {'name': 'odd', 'reply': self.model(projects=[project('a', odd_status)])},
                               {'name': 'nostatus', 'reply': self.model(projects=[project('a', no_status)])},
                               {'name': 'badtime', 'reply': self.model(projects=[project('a', bad_time)])},
                               {'name': 'notloaded', 'reply': self.model(projects=[project('a', {**ok, 'last_chat_status': 'notLoaded'})])},
                               {'name': 'empty', 'reply': self.model(projects=[project('a', {'last_ok_age_seconds': None, 'last_latency_seconds': None,
                                                                                           'last_chat_status': None, 'pending_seconds': None})])}])
        self.assertIn('Canary 1.5 s, 3 min ago', rows['ok']['text'])
        self.assertIn('Canary waiting 7 min', rows['waiting']['text'])
        self.assertNotIn('Canary 1.5', rows['waiting']['text'])
        self.assertIn('Canary 1.5 s, 3 min ago', rows['notloaded']['text'])
        for name in ('none', 'empty', 'odd', 'nostatus', 'badtime'):
            self.assertIn('Canary unknown', rows[name]['text'])
            self.assertNotIn(' s, ', rows[name]['text'])
            self.assertNotIn('unknown time', rows[name]['text'])

    def reports(self, count=2, **changes):
        rows = [{'id': A, 'state': 'ambiguous', 'sender': 'developer', 'target': 'manager', 'age_seconds': 600},
                {'id': B, 'state': 'needs-attention', 'sender': 'reviewer', 'target': 'manager', 'age_seconds': 7200}][:count]
        return {'project': 'alpha', 'rows': rows, 'cut': False, 'limit': 20, **changes}

    def posts(self, row):
        return [call for call in row['calls'] if call['options']]

    def test_waiting_reports_get_retry_and_skip_buttons_and_show_no_report_text(self):
        rows = self.run_steps([{'name': 'a', 'reply': self.model(), 'rows': {'alpha': self.reports()}}])['a']
        self.assertEqual(rows['buttons'], {'retry': 2, 'skip': 2, 'yes': 0, 'no': 0})
        for expected in ('Waiting for a decision', 'aaaaaaaa', 'bbbbbbbb', 'Unconfirmed', 'Needs attention', 'developer → manager', 'Retry', 'Skip'):
            self.assertIn(expected, rows['text'])
        self.assertEqual([c['url'] for c in rows['calls']], ['/api/outbox-health', '/api/outbox-rows?project=alpha'])
        self.assertEqual(self.posts(rows), [])

    def test_a_project_without_waiting_reports_asks_for_no_list(self):
        rows = self.run_steps([{'name': 'a', 'reply': self.model(projects=[self.model()['projects'][1]])}])['a']
        self.assertEqual([c['url'] for c in rows['calls']], ['/api/outbox-health'])
        self.assertEqual(rows['buttons']['retry'], 0)

    def test_retry_asks_first_warns_about_a_duplicate_and_posts_only_after_the_confirmation(self):
        steps = [{'name': 'a', 'reply': self.model(), 'rows': {'alpha': self.reports()},
                  'clicks': [{'cls': 'delivery-retry', 'row': A}, {'cls': 'delivery-confirm-no'}, {'cls': 'delivery-retry', 'row': A}]}]
        row = self.run_steps(steps)['a']
        asked, cancelled, asked_again = row['clickTexts']
        for text in (asked, asked_again):
            self.assertIn('same report twice', text)
            self.assertIn('wait behind other unconfirmed reports for the same recipient', text)
            self.assertIn('Confirm retry', text)
        self.assertNotIn('same report twice', cancelled)
        self.assertEqual(self.posts(row), [])  # asking and cancelling send nothing
        self.assertEqual(row['buttons']['yes'], 1)

    def test_a_confirmed_retry_posts_once_with_confirm_true_and_shows_the_servers_answer(self):
        reply = {'id': A, 'action': 'retry', 'state': 'pending', 'journal_written': True}
        row = self.run_steps([{'name': 'a', 'reply': self.model(), 'rows': {'alpha': self.reports()}, 'resolve': reply,
                               'clicks': [{'cls': 'delivery-retry', 'row': A}, {'cls': 'delivery-confirm-yes'}]}])['a']
        posts = self.posts(row)
        self.assertEqual([(p['url'], p['options']['method'], p['body']) for p in posts],
                         [('/api/outbox-resolve', 'POST', {'project': 'alpha', 'id': A, 'action': 'retry', 'confirm': True})])
        self.assertIn('Report aaaaaaaa is queued to be sent again.', row['text'])
        self.assertEqual([c['url'] for c in row['calls']].count('/api/outbox-rows?project=alpha'), 2)  # the list is read again
        self.assertEqual(row['buttons']['yes'], 0)

    def test_a_confirmed_skip_posts_skip_and_shows_skipped(self):
        reply = {'id': B, 'action': 'skip', 'state': 'skipped', 'journal_written': True}
        row = self.run_steps([{'name': 'a', 'reply': self.model(), 'rows': {'alpha': self.reports()}, 'resolve': reply,
                               'clicks': [{'cls': 'delivery-skip', 'row': B}]}])['a']
        self.assertIn('This report will never be sent.', row['clickTexts'][0])
        self.assertIn('Confirm skip', row['clickTexts'][0])
        row = self.run_steps([{'name': 'a', 'reply': self.model(), 'rows': {'alpha': self.reports()}, 'resolve': reply,
                               'clicks': [{'cls': 'delivery-skip', 'row': B}, {'cls': 'delivery-confirm-yes'}]}])['a']
        self.assertEqual([p['body']['action'] for p in self.posts(row)], ['skip'])
        self.assertIn('Report bbbbbbbb was skipped.', row['text'])

    def test_a_refusal_shows_the_servers_reason_and_never_success(self):
        failure = {'__error': 'That report is not waiting for a decision', 'status': 409}
        row = self.run_steps([{'name': 'a', 'reply': self.model(), 'rows': {'alpha': self.reports()}, 'resolve': failure,
                               'clicks': [{'cls': 'delivery-retry', 'row': A}, {'cls': 'delivery-confirm-yes'}]}])['a']
        self.assertIn('Not done: That report is not waiting for a decision', row['text'])
        self.assertNotIn('queued', row['text'])

    def test_a_503_a_lost_connection_or_an_odd_answer_is_unknown_and_never_success(self):
        answers = {'503': {'__error': 'The outcome is unknown', 'status': 503}, 'network': {'__error': 'Failed to fetch'},
                   'wrong state': {'id': A, 'action': 'retry', 'state': 'delivered', 'journal_written': True},
                   'wrong row': {'id': B, 'action': 'retry', 'state': 'pending', 'journal_written': True}, 'empty': {}}
        for name, answer in answers.items():
            row = self.run_steps([{'name': 'a', 'reply': self.model(), 'rows': {'alpha': self.reports()}, 'resolve': answer,
                                   'clicks': [{'cls': 'delivery-retry', 'row': A}, {'cls': 'delivery-confirm-yes'}]}])['a']
            self.assertIn('Unknown outcome. The list was read again', row['text'], name)
            for success in ('queued', 'was skipped', 'Not done'):
                self.assertNotIn(success, row['text'], name)
            self.assertEqual(len(self.posts(row)), 1, name)

    def test_a_journal_that_could_not_be_written_is_said(self):
        reply = {'id': A, 'action': 'retry', 'state': 'pending', 'journal_written': False}
        row = self.run_steps([{'name': 'a', 'reply': self.model(), 'rows': {'alpha': self.reports()}, 'resolve': reply,
                               'clicks': [{'cls': 'delivery-retry', 'row': A}, {'cls': 'delivery-confirm-yes'}]}])['a']
        self.assertIn('The journal could not be written.', row['text'])

    def test_an_unreadable_list_is_unknown_with_no_buttons_and_an_empty_list_says_so(self):
        rows = self.run_steps([{'name': 'bad', 'reply': self.model(), 'rows': {'alpha': {'__error': 'x', 'status': 503}}},
                               {'name': 'odd', 'reply': self.model(), 'rows': {'alpha': {'rows': 'no'}}},
                               {'name': 'empty', 'reply': self.model(), 'rows': {'alpha': self.reports(0)}}])
        for name in ('bad', 'odd'):
            self.assertIn('cannot be read. Unknown is not empty.', rows[name]['text'], name)
            self.assertEqual(rows[name]['buttons']['retry'], 0, name)
        self.assertIn('No report is waiting for a decision', rows['empty']['text'])

    def test_a_cut_list_says_how_many_older_reports_cannot_be_decided_from_office(self):
        row = self.run_steps([{'name': 'a', 'reply': self.model(), 'rows': {'alpha': self.reports(1, cut=True)}}])['a']
        self.assertIn('Only the newest 20 reports of each state are shown. Older ones cannot be decided from Office.', row['text'])
        self.assertIn('3 older reports are not shown.', row['text'])
        whole = self.run_steps([{'name': 'a', 'reply': self.model(), 'rows': {'alpha': self.reports()}}])['a']
        self.assertNotIn('Only the newest', whole['text'])

    def test_the_decision_labels_have_bulgarian_entries(self):
        # Bulgarian labels as escapes: the public audit keeps Cyrillic out of test files
        reply = {'id': A, 'action': 'retry', 'state': 'pending', 'journal_written': True}
        row = self.run_steps([{'name': 'a', 'language': 'bg', 'reply': self.model(), 'rows': {'alpha': self.reports()}, 'resolve': reply,
                               'clicks': [{'cls': 'delivery-retry', 'row': A}]}])['a']
        for expected in ('\u0427\u0430\u043a\u0430\u0442 \u0440\u0435\u0448\u0435\u043d\u0438\u0435', '\u041e\u043f\u0438\u0442\u0430\u0439 \u043f\u0430\u043a', '\u041f\u0440\u043e\u043f\u0443\u0441\u043d\u0438',
                         '\u0418\u0441\u043a\u0430 \u0432\u043d\u0438\u043c\u0430\u043d\u0438\u0435'):
            self.assertIn(expected, row['text'])
        self.assertIn('\u041f\u043e\u0442\u0432\u044a\u0440\u0434\u0438 \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u0438\u044f \u043e\u043f\u0438\u0442', row['clickTexts'][0])
        self.assertIn('\u0414\u043e\u043a\u043b\u0430\u0434\u044a\u0442 \u043c\u043e\u0436\u0435 \u0432\u0435\u0447\u0435 \u0434\u0430 \u0435 \u043f\u0440\u0438\u0441\u0442\u0438\u0433\u043d\u0430\u043b', row['clickTexts'][0])

    def test_a_failed_incident_bookkeeping_is_shown_and_a_clean_pass_shows_nothing(self):
        rows = self.run_steps([{'name': 'failed', 'reply': self.model(incidents_error='KeyError: opened')},
                               {'name': 'clean', 'reply': self.model()}])
        self.assertIn('Incident bookkeeping failed: KeyError: opened', rows['failed']['text'])
        self.assertNotIn('Incident bookkeeping', rows['clean']['text'])

    def test_unknown_and_the_incident_failure_have_bulgarian_labels(self):
        # Bulgarian labels as escapes: the public audit keeps Cyrillic out of test files
        unknown = '\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u043e'
        failed = '\u041e\u0442\u0447\u0438\u0442\u0430\u043d\u0435\u0442\u043e \u043d\u0430 \u0438\u043d\u0446\u0438\u0434\u0435\u043d\u0442\u0438\u0442\u0435 \u0435 \u043d\u0435\u0443\u0441\u043f\u0435\u0448\u043d\u043e'
        rows = self.run_steps([{'name': 'unknown', 'language': 'bg', 'reply': self.model(state='unknown', reason='old', age_seconds=900, relay=None,
                                                                                   projects=[], supervisor=None, last_pump_action=None)},
                               {'name': 'failed', 'language': 'bg', 'reply': self.model(incidents_error='boom')}])
        self.assertIn(unknown, rows['unknown']['text'])
        self.assertIn(failed + ': boom', rows['failed']['text'])

    def test_every_label_the_panel_uses_has_a_bulgarian_entry(self):
        import re
        script = (STATIC / 'delivery.js').read_text(encoding='utf-8')
        catalogue = (STATIC / 'i18n.js').read_text(encoding='utf-8')
        keys = set(re.findall(r"\bT\(\s*'((?:[^'\\\\]|\\\\.)*)'", script))
        keys |= set(re.findall(r":'([^']*)'", re.search(r"STATE=\{([^}]*)\}", script).group(1)))
        keys |= set(re.findall(r":'([^']*)'", re.search(r"NOTE=\{(.*?)\};", script, re.S).group(1)))
        self.assertGreater(len(keys), 20)
        self.assertEqual(sorted(k for k in keys if "'" + k + "':" not in catalogue and '"' + k + '":' not in catalogue), [])

    def test_bulgarian_labels_exist(self):
        rows = self.run_steps([{'name': 'bg', 'language': 'bg', 'reply': self.model()}])
        # Bulgarian labels as escapes: the public audit keeps Cyrillic out of test files
        for expected in ('\u0414\u043e\u0441\u0442\u0430\u0432\u043a\u0430', '\u0412 \u0440\u0435\u0434', '\u0420\u0435\u043b\u0435\u0442\u043e \u0440\u0430\u0431\u043e\u0442\u0438', '\u041f\u043e\u043c\u043f\u0430\u0442\u0430 \u0440\u0430\u0431\u043e\u0442\u0438', '\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0430 \u0434\u043e\u0441\u0442\u0430\u0432\u043a\u0430 \u043f\u0440\u0435\u0434\u0438 5 \u043c\u0438\u043d'):
            self.assertIn(expected, rows['bg']['text'])


if __name__ == '__main__':
    unittest.main()
