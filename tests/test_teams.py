import os
import json
import tempfile
import unittest
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from team_room import manifest, roster, teams  # noqa: E402
import team as cli  # noqa: E402

CATALOG = ROOT / 'catalog'


class TeamTests(unittest.TestCase):
    def test_tracking_is_required_before_install_and_both_needs_authority(self):
        plan = teams.plan_team(self.project, 'development')
        self.assertIsNone(plan['work_tracking']['mode'])
        with self.assertRaises(teams.TeamError):
            teams.install(plan)
        self.assertEqual(list(self.project.iterdir()), [])
        for values in ({'tracking_mode': 'invalid'},
                       {'tracking_mode': 'local', 'tracking_authority': 'github'}):
            with self.assertRaises(teams.TeamError):
                teams.plan_team(self.project, 'development', team_answers=values)
        plan = teams.plan_team(self.project, 'development', team_answers={'tracking_mode': 'both'})
        with self.assertRaises(teams.TeamError):
            teams.install(plan)
        plan = teams.plan_team(self.project, 'development', team_answers={
            'tracking_mode': 'both', 'tracking_authority': 'github'})
        teams.install(plan)
        before = roster.load(self.project)
        self.assertEqual(before['teams'][0]['work_tracking'], {'mode': 'both', 'authority': 'github'})
        teams.install(teams.plan_agent(self.project, 'Development', 'strategist'))
        self.assertEqual(roster.load(self.project)['teams'], before['teams'])
        self.assertIn('Implementation must have a recorded task',
                      (self.project / 'team/Development/strategist/agent/BASE.md').read_text())

    def test_expanded_inventory_installs_every_bundle_and_new_individual(self):
        inventory = teams.index(CATALOG)
        self.assertEqual(len(inventory['agents']), 24)
        self.assertEqual(len(inventory['teams']), 13)
        new_roles = ['business-analyst', 'research-analyst', 'product-manager',
                     'data-analyst', 'operations-specialist', 'knowledge-writer',
                     'support-specialist', 'learning-designer', 'career-advisor',
                     'financial-analyst']
        self.assertNotIn('personal-assistant', [a['name'] for a in inventory['agents']])
        for item in inventory['teams']:
            root = self.project / item['name']
            root.mkdir()
            values = {'tracking_mode': 'local'}
            if item['name'] == 'starter':
                values['specialist'] = 'research-analyst'
            plan = teams.plan_team(root, item['name'], team_answers=values)
            self.assertEqual(list(root.iterdir()), [])
            if item['name'] == 'starter':
                self.assertEqual(len(plan['agents']), 2)
            else:
                self.assertTrue(3 <= len(plan['agents']) <= 5)
            teams.install(plan)
            self.assertTrue(all(p['thread_id'] is None for p in roster.load(root)['participants']))
            with self.assertRaises(teams.TeamError):
                teams.plan_team(root, item['name'], team_answers=values)
        teams.install(teams.plan_team(self.project, 'development', team_answers={'tracking_mode': 'local'}))
        for name in new_roles:
            plan = teams.plan_agent(self.project, 'Development', name)
            teams.install(plan)
            root = self.project / plan['agents'][0]['workspace']
            profile = (root / 'agent/PROFILE.md').read_text(encoding='utf-8')
            playbook = (root / 'agent/PLAYBOOK.md').read_text(encoding='utf-8')
            self.assertIn('Required:', profile)
            self.assertIn('Optional:', profile)
            self.assertIn('Synthetic exercise', playbook)
            self.assertIn('Boundary exercise', playbook)
            result = subprocess.run([sys.executable, '-B', str(root / 'scripts/bot_mode.py'),
                                     '--project', str(root), '--bot', plan['agents'][0]['bot'], 'memory'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [])
        for item in inventory['agents']:
            self.assertEqual(teams.agent_template(CATALOG, item['name'])[1]['version'], item['version'])
        for item in inventory['teams']:
            self.assertEqual(teams.team_template(CATALOG, item['name'])[1]['version'], item['version'])

    def test_starter_requires_real_specialist_and_uses_its_identity(self):
        for value in (None, 'blank', 'manager', 'director', 'made-up'):
            with self.assertRaises(teams.TeamError):
                teams.plan_team(self.project, 'starter', team_answers={
                    'tracking_mode': 'local', 'specialist': value})
            self.assertEqual(list(self.project.iterdir()), [])
        plan = teams.plan_team(self.project, 'starter', team_answers={
            'tracking_mode': 'local', 'specialist': 'product-manager'})
        self.assertEqual([a['bot'] for a in plan['agents']], ['manager', 'product-manager'])
        self.assertEqual([a['role'] for a in plan['agents']], ['Manager', 'Product Manager'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'demo'
        self.project.mkdir()
        self.orig_config = os.environ.get('CODEX_BOT_MODE_CONFIG')
        os.environ['CODEX_BOT_MODE_CONFIG'] = str(Path(self.tmp.name) / 'no-config.json')

    def tearDown(self):
        self.tmp.cleanup()
        if hasattr(self, 'orig_config') and self.orig_config is not None:
            os.environ['CODEX_BOT_MODE_CONFIG'] = self.orig_config
        else:
            os.environ.pop('CODEX_BOT_MODE_CONFIG', None)

    def run_cli(self, *args):
        return cli.main([*args])

    def test_agent_monitor_catalog_template_and_privacy_audit(self):
        folder, meta = teams.agent_template(CATALOG, 'agent-monitor')
        self.assertEqual(meta['role'], 'Agent Monitor')
        self.assertEqual(meta['version'], 1)
        self.assertEqual(meta['reports_to'], 'chief-of-staff')
        perms = meta.get('permissions', {})
        self.assertEqual(perms.get('assign_work'), 'none')
        self.assertEqual(perms.get('product_writes'), 'none')
        self.assertEqual(perms.get('external_messages'), 'none')
        self.assertEqual(perms.get('publishing'), 'none')
        params = meta.get('parameters', {})
        self.assertEqual(params['display_name']['default'], 'Agent Monitor')
        self.assertEqual(params['model']['default'], 'gpt-6-luna')
        self.assertEqual(params['reasoning']['default'], 'low')
        self.assertEqual(params['reasoning']['choices'], ['low', 'medium', 'high'])
        self.assertEqual(params['interval_minutes']['default'], 20)
        self.assertEqual(params['recipient']['default'], '')
        # Generic privacy assertions: dynamically construct checks without literal forbidden substrings
        forbidden_fragments = ['01a' + '1', '0aa' + '0', 'Users' + '\\User', 'ID' + '-Yo', 'ivan' + 'yosifov']
        for fname in ('agent.yaml', 'PROFILE.md', 'SOUL.md', 'PLAYBOOK.md'):
            content = (folder / fname).read_text(encoding='utf-8')
            for fragment in forbidden_fragments:
                self.assertNotIn(fragment.lower(), content.lower())
        doc_path = ROOT / 'docs/operations/agent-monitor.md'
        self.assertTrue(doc_path.exists())
        doc_content = doc_path.read_text(encoding='utf-8')
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment.lower(), doc_content.lower())
        self.assertIn('gpt-6-luna', doc_content)
        self.assertIn('Gemini Flash high', doc_content)

        # Repository hygiene documentation audit
        hygiene_path = ROOT / 'docs/operations/repository-hygiene.md'
        self.assertTrue(hygiene_path.exists())
        hygiene_content = hygiene_path.read_text(encoding='utf-8')
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment.lower(), hygiene_content.lower())
        self.assertIn('Repository Hygiene', hygiene_content)
        self.assertIn('Process Cleanup Sanity Check', hygiene_content)

    def test_catalog_templates_are_valid(self):
        data = teams.index(CATALOG)
        for item in data['agents']:
            teams.agent_template(CATALOG, item['name'])
        for item in data['teams']:
            teams.team_template(CATALOG, item['name'])

    def test_plan_does_not_write(self):
        plan = teams.plan_team(self.project, 'development', team_answers={'tracking_mode': 'local'})
        self.assertEqual([a['bot'] for a in plan['agents']], ['manager', 'developer', 'code-reviewer', 'designer', 'verifier'])
        self.assertEqual(plan['section'], 'demo · Development')
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertIsNone(plan['director_proposal'])

    def test_install_team_then_second_team_and_director(self):
        self.assertEqual(self.run_cli('install-team', 'development', '--tracking-mode', 'local', '--project', str(self.project),
                                      '--approval', 'once'), 2)  # no --confirm
        self.assertEqual(self.run_cli('install-team', 'development', '--tracking-mode', 'local', '--project', str(self.project),
                                      '--approval', 'once', '--confirm'), 0)
        dev = self.project / 'team/Development/developer'
        self.assertTrue((dev / 'agent/BASE.md').exists())
        self.assertIn('../../../.bot-mode/roster.py', (dev / 'agent/BASE.md').read_text(encoding='utf-8'))
        links = subprocess.run([sys.executable, '-B', str(self.project / '.bot-mode/roster.py'), '--project',
                                str(self.project), 'links'], capture_output=True, text=True)
        self.assertEqual(links.returncode, 0, links.stderr)
        self.assertEqual(len(json.loads(links.stdout)), 5)
        self.assertEqual(roster.load(self.project)['project']['tooling'], str(ROOT))
        self.assertTrue((self.project / 'team/PROJECT.md').exists())
        meta = manifest.load(dev / 'agent/agent.yaml')
        self.assertEqual(meta['parameters']['display_name']['source'], 'default')
        out = subprocess.run([sys.executable, '-B', str(dev / 'scripts/bot_mode.py'), '--project', str(dev),
                              '--bot', 'developer', 'memory'], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [])
        data = roster.load(self.project)
        self.assertEqual(data['teams'][0]['section'], 'demo · Development')
        self.assertTrue(all(p['thread_id'] is None for p in data['participants']))
        with self.assertRaises(teams.TeamError):
            teams.plan_team(self.project, 'development', team_answers={'tracking_mode': 'local'})
        plan = teams.plan_team(self.project, 'development', team_answers={'team_name': 'Platform', 'tracking_mode': 'local'})
        self.assertEqual([a['bot'] for a in plan['agents']][:2], ['platform-manager', 'platform-developer'])
        self.assertIsNotNone(plan['director_proposal'])
        teams.install(plan)
        self.assertEqual(self.run_cli('add-agent', '--project', str(self.project), '--template', 'director',
                                      '--actor', 'owner', '--approval', 'once', '--confirm'), 0)
        director = self.project / 'team/project-director'
        self.assertIn('- Bot ID: \x60project-director\x60', (director / 'agent/PROFILE.md').read_text(encoding='utf-8'))
        self.assertIn('../../.bot-mode/roster.py', (director / 'agent/BASE.md').read_text(encoding='utf-8'))
        with self.assertRaises(teams.TeamError):
            teams.plan_agent(self.project, None, 'director')

    def test_owner_answers_and_blank_agent(self):
        teams.install(teams.plan_team(self.project, 'development',
                                      answers={'developer': {'display_name': 'Dev Ana'}}, team_answers={'tracking_mode': 'local'}))
        meta = manifest.load(self.project / 'team/Development/developer/agent/agent.yaml')
        self.assertEqual(meta['parameters']['display_name'], {**meta['parameters']['display_name'], 'value': 'Dev Ana', 'source': 'owner'})
        with self.assertRaises(teams.TeamError):
            teams.plan_agent(self.project, 'Development', 'blank', {'role': 'Copywriter'})
        spec = {'role': 'Copywriter', 'purpose': 'Write product copy.', 'responsibilities': 'Draft copy.',
                'done_means': 'Copy approved.', 'verification': 'Reviewer checks tone.'}
        path = Path(self.tmp.name) / 'a.json'
        path.write_text(json.dumps(spec), encoding='utf-8')
        args = ['add-agent', '--project', str(self.project), '--team', 'Development', '--template', 'blank',
                '--answers', str(path), '--actor', 'manager']
        self.assertEqual(self.run_cli(*args, '--approval', 'none', '--confirm'), 2)
        self.assertFalse((self.project / 'team/Development/copywriter').exists())
        self.assertEqual(self.run_cli(*args, '--approval', 'once', '--confirm'), 0)
        meta = manifest.load(self.project / 'team/Development/copywriter/agent/agent.yaml')
        self.assertTrue(meta['custom'])
        self.assertEqual(meta['proposed_by'], 'manager')
        self.assertIn('Write product copy.', (self.project / 'team/Development/copywriter/agent/SOUL.md').read_text(encoding='utf-8'))

    def test_standing_approvals(self):
        teams.install(teams.plan_team(self.project, 'development', team_answers={'tracking_mode': 'local'}))
        file = teams.approvals_path(self.project)
        manifest.dump(file, {'approvals': [
            {'id': 'catalog', 'granted_by': 'owner', 'used_by': ['manager'], 'covers': ['catalog-agent'],
             'teams': ['Development'], 'templates': ['verifier'], 'max_per_month': 1},
            {'id': 'old', 'granted_by': 'owner', 'used_by': ['manager'], 'covers': ['catalog-agent'], 'expires': '2000-01-01'},
            {'id': 'fake', 'granted_by': 'manager', 'used_by': ['manager'], 'covers': ['catalog-agent']},
            {'id': 'off', 'granted_by': 'owner', 'used_by': ['manager'], 'covers': ['catalog-agent'], 'revoked': True}]})
        check = lambda key, **kw: teams.check_approval(self.project, f'standing:{key}', kw.get('actor', 'manager'),
                                                         kw.get('kind', 'catalog-agent'), 'Development', kw.get('template', 'verifier'))
        self.assertEqual(check('catalog'), 'standing:catalog')
        for key, kw in (('old', {}), ('fake', {}), ('off', {}), ('missing', {}), ('catalog', {'actor': 'developer'}),
                        ('catalog', {'kind': 'custom-agent'}), ('catalog', {'template': 'developer'})):
            with self.assertRaises(teams.TeamError, msg=key):
                check(key, **kw)
        teams.record_use(self.project, 'standing:catalog', 'manager', 'test')
        with self.assertRaises(teams.TeamError):
            check('catalog')

    def test_marketing_and_sales_join_an_existing_project(self):
        teams.install(teams.plan_team(self.project, 'development', team_answers={'tracking_mode': 'local'}))
        marketing = teams.plan_team(self.project, 'marketing', team_answers={'tracking_mode': 'local'})
        self.assertEqual([a['bot'] for a in marketing['agents']],
                         ['marketing-manager', 'strategist', 'content-writer', 'content-reviewer'])
        self.assertIsNotNone(marketing['director_proposal'])
        teams.install(marketing)
        sales = teams.plan_team(self.project, 'sales', team_answers={'tracking_mode': 'local'})
        self.assertEqual([a['bot'] for a in sales['agents']],
                         ['sales-manager', 'lead-researcher', 'outreach-writer', 'pipeline-analyst'])
        teams.install(sales)
        outreach = manifest.load(self.project / 'team/Sales/outreach-writer/agent/agent.yaml')
        self.assertEqual(outreach['permissions']['external_messages'], 'draft-only')
        self.assertEqual(sorted(t['name'] for t in roster.load(self.project)['teams']), ['Development', 'Marketing', 'Sales'])

    def test_team_launch_registers_project_with_chief_of_staff_once(self):
        import os
        cos = Path(self.tmp.name) / 'cos'
        (cos / 'knowledge').mkdir(parents=True)
        (cos / 'knowledge/projects.md').write_text('# Projects\n\n| Project | Path | Purpose |\n|---|---|---|\n', encoding='utf-8')
        config = Path(self.tmp.name) / 'config.json'
        config.write_text(json.dumps({'chief_of_staff': str(cos)}), encoding='utf-8')
        os.environ['CODEX_BOT_MODE_CONFIG'] = str(config)
        try:
            teams.install(teams.plan_team(self.project, 'development', team_answers={'tracking_mode': 'local'}))
            teams.install(teams.plan_team(self.project, 'sales', team_answers={'tracking_mode': 'local'}))
        finally:
            os.environ['CODEX_BOT_MODE_CONFIG'] = str(Path(self.tmp.name) / 'no-config.json')
        text = (cos / 'knowledge/projects.md').read_text(encoding='utf-8')
        self.assertEqual(text.count(str(self.project.resolve())), 1)
        self.assertIn('| demo |', text)

    def test_existing_team_adopts_upgrade_roles(self):
        data = roster.load(self.project)
        roster.add_team(data, 'Development')
        roster.add_participant(data, 'developer', 'Developer', 'Development', 'team/Development/developer')
        roster.save(self.project, data, roster.version(roster.load(self.project)))
        memory = self.project / 'team/Development/developer/agent/memory'
        memory.mkdir(parents=True)
        (memory / 'summary.md').write_text('kept', encoding='utf-8')
        for template in ('code-reviewer', 'designer'):
            teams.install(teams.plan_agent(self.project, 'Development', template))
        self.assertEqual((memory / 'summary.md').read_text(encoding='utf-8'), 'kept')
        self.assertTrue((self.project / 'team/Development/code-reviewer/agent/PLAYBOOK.md').exists())

    def test_invalid_template_refused(self):
        bad = Path(self.tmp.name) / 'catalog'
        shutil.copytree(CATALOG, bad)
        (bad / 'agents/developer/agent.yaml').write_text('role: Developer\nversion: 2\nparameters:\n  model:\n    default: x\n', encoding='utf-8')
        with self.assertRaises(teams.TeamError):
            teams.agent_template(bad, 'developer')
        with self.assertRaises(teams.TeamError):
            teams.plan_team(self.project, 'development', source=str(bad))


if __name__ == '__main__':
    unittest.main()
