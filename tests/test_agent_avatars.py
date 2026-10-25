"""Agent avatars: a self-contained SVG set and a stable, seeded assignment (avatars.js, run in Node like the other
frontend contract tests). The assignment is pseudo-random but fixed per project and bot id, so reloads and pages agree."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'scripts/team_room/static'

NODE = r'''
const A = require(process.argv[1]);
const out = {};
out.count = A.count;
out.pinned = {
  routeport_manager: A.index('routeport', 'manager'), routeport_developer: A.index('routeport', 'developer'),
  hark_manager: A.index('hark', 'manager'), cos: A.index('standalone', 'chief-of-staff'), alpha_agent0: A.index('alpha', 'agent0'),
};
out.repeat = Array.from({length: 50}, () => A.index('routeport', 'manager')).every(n => n === out.pinned.routeport_manager);
// 400 plausible agents: the seed spreads over the whole set.
const buckets = new Array(A.count).fill(0);
for (let p = 0; p < 20; p++) for (let b = 0; b < 20; b++) buckets[A.index('project' + p, 'agent' + b)]++;
out.buckets = buckets;
const svgs = Array.from({length: A.count}, (_, i) => A.svg(i));
out.unique = new Set(svgs).size;
out.external = svgs.filter(s => /https?:\/\/(?!www\.w3\.org\/2000\/svg)|href=|url\(|<image|<script|<style|currentColor/i.test(s)).length;
out.wrap = A.svg(A.count + 3) === A.svg(3);
const node = () => ({attrs: {}, classes: new Set(), dataset: {}, textContent: '', innerHTML: '',
  setAttribute(k, v) { this.attrs[k] = v; }, classList: {add: c => n.classes.add(c)}});
let n = node();
A.fill(n, {key: 'routeport:manager', name: 'RoutePort Manager'});
out.byKey = n.dataset.avatar === String(A.index('routeport', 'manager')) && n.innerHTML.startsWith('<svg');
n = node();
A.fill(n, {projectId: 'routeport', botId: 'manager', name: 'x'});
out.byIds = n.dataset.avatar === String(A.index('routeport', 'manager'));
n = node();
A.fill(n, {name: 'RoutePort Manager'});
out.fallback = n.textContent === 'RM' && n.innerHTML === '' && !n.classes.has('avatar-art');
out.styles = new Set(Array.from({length: A.count}, (_, i) => A.traits(i).style)).size;
// The Manager shown next to a project is the one the registry resolved, whatever the order or the ids look like.
const bot = (project, id, role) => ({bot_key: project + ':' + id, bot_id: id, project_id: project, role, name: id});
const teamFirst = {bots: [bot('p', 'marketing-manager', 'Manager'), bot('p', 'manager', 'Manager')], managers: {p: 'p:manager'}};
out.teamFirst = A.managerOf(teamFirst, 'p').bot_key;
out.ambiguous = A.managerOf({bots: [bot('p', 'manager', 'Manager'), bot('p', 'manager2', 'Manager')], managers: {}}, 'p');
out.noMap = A.managerOf({bots: [bot('p', 'manager', 'Manager')]}, 'p');
out.substring = A.managerOf({bots: [bot('p', 'manager-helper', 'Developer'), bot('p', 'pm', 'Product Manager')], managers: {}}, 'p');
out.staleKey = A.managerOf({bots: [bot('p', 'manager', 'Manager')], managers: {p: 'p:gone'}}, 'p');
out.otherProject = A.managerOf({bots: [bot('q', 'manager', 'Manager')], managers: {p: 'q:manager'}}, 'p');
out.nothing = [A.managerOf(null, 'p'), A.managerOf({}, 'p'), A.managerOf({managers: {p: 5}, bots: []}, 'p')];
console.log(JSON.stringify(out));
'''


def run_node():
    done = subprocess.run(['node', '-e', NODE, str(STATIC / 'avatars.js')], capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


class AvatarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_node()

    def test_assignment_is_fixed_per_project_and_bot(self):
        # Pinned: changing the seed or the hash would silently change every agent's face.
        self.assertEqual(self.result['pinned'], {'routeport_manager': 14, 'routeport_developer': 5, 'hark_manager': 19,
                                                 'cos': 18, 'alpha_agent0': 30})
        self.assertTrue(self.result['repeat'])
        self.assertEqual(self.result, run_node(), 'a second process gives the same assignment')

    def test_same_bot_id_in_two_projects_is_seeded_apart(self):
        pinned = self.result['pinned']
        self.assertNotEqual(pinned['routeport_manager'], pinned['hark_manager'])

    def test_assignment_spreads_over_the_whole_library(self):
        buckets = self.result['buckets']
        self.assertGreaterEqual(self.result['count'], 24)
        self.assertEqual(len(buckets), self.result['count'])
        self.assertTrue(all(buckets), 'every avatar is used')
        self.assertLess(max(buckets), 4 * 400 / self.result['count'])

    def test_library_is_distinct_and_self_contained(self):
        self.assertEqual(self.result['unique'], self.result['count'])
        self.assertEqual(self.result['external'], 0)
        self.assertTrue(self.result['wrap'])

    def test_fill_uses_the_bot_key_or_the_ids_and_falls_back_to_initials(self):
        self.assertTrue(self.result['byKey'])
        self.assertTrue(self.result['byIds'])
        self.assertTrue(self.result['fallback'])

    def test_every_hair_style_is_used(self):
        self.assertEqual(self.result['styles'], 9)

    def test_project_manager_comes_from_the_registry_and_is_never_guessed(self):
        r = self.result
        self.assertEqual(r['teamFirst'], 'p:manager', 'a team Manager listed first is not the project Manager')
        self.assertIsNone(r['ambiguous'], 'two Managers: no face is implied')
        self.assertIsNone(r['noMap'], 'an overview without the resolved Manager shows none')
        self.assertIsNone(r['substring'], 'ids or roles that merely contain "manager" never qualify')
        self.assertIsNone(r['staleKey'])
        self.assertIsNone(r['otherProject'])
        self.assertEqual(r['nothing'], [None, None, None])

    def test_projects_page_asks_the_registry_for_the_manager(self):
        source = (STATIC / 'projects.js').read_text(encoding='utf-8')
        self.assertIn('officeAvatars.managerOf(registryData,pid)', source)
        self.assertNotIn('/manager/i.test', source)

    def test_office_serves_and_loads_the_library(self):
        web = (ROOT / 'scripts/team_room/web.py').read_text(encoding='utf-8')
        index = (STATIC / 'index.html').read_text(encoding='utf-8')
        for name in ('avatars.js', 'avatars.css'):
            self.assertIn(f"'/{name}': ('{name}'", web)
            self.assertIn(f'/{name}"', index)
        self.assertLess(index.index('/avatars.js'), index.index('/projects.js'))


if __name__ == '__main__':
    unittest.main()

