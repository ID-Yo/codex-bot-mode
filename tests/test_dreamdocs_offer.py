import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from scripts.team_room import dreamdocs_offer as offer, project_onboarding as onboarding  # noqa: E402
import team  # noqa: E402


def make_project(base, name='proj', manager=True):
    root = Path(base) / name
    (root / '.bot-mode/local').mkdir(parents=True)
    if manager:
        (root / '.bot-mode/roster.json').write_text(json.dumps({'schema': 1, 'project': {'name': name},
            'teams': [{'name': 'Development'}], 'participants': [{'bot': 'manager', 'role': 'Manager', 'team': 'Development',
            'status': 'active', 'thread_id': '00000000-0000-0000-0000-000000000001'}]}), encoding='utf-8')
    return root


class OfferRecordTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'p'
        self.root.mkdir()

    def test_unanswered_project_is_offered_in_both_places(self):
        self.assertIsNone(offer.load(self.root))
        self.assertTrue(offer.should_offer(self.root, 'office'))
        self.assertTrue(offer.should_offer(self.root, 'team-launch'))

    def test_skip_is_asked_once_per_place_and_never_blocks(self):
        offer.record(self.root, 'skip', 'office')
        self.assertFalse(offer.should_offer(self.root, 'office'))
        self.assertTrue(offer.should_offer(self.root, 'team-launch'))
        offer.record(self.root, 'skip', 'team-launch')
        self.assertEqual(offer.load(self.root)['asked_in'], ['office', 'team-launch'])
        self.assertFalse(offer.should_offer(self.root, 'team-launch'))
        offer.record(self.root, 'yes', 'team-launch')  # a later explicit answer replaces Skip
        self.assertEqual(offer.load(self.root)['choice'], 'yes')

    def test_yes_and_no_are_final_until_the_owner_asks(self):
        for choice, other in (('no', 'yes'), ('yes', 'skip'), ('no', 'skip')):
            with self.subTest(choice=choice, other=other):
                offer.path(self.root).unlink(missing_ok=True)
                offer.record(self.root, choice, 'office')
                for place in offer.PLACES:
                    self.assertFalse(offer.should_offer(self.root, place))
                with self.assertRaises(ValueError):
                    offer.record(self.root, other, 'team-launch')
                self.assertEqual(offer.load(self.root)['choice'], choice)
                offer.record(self.root, choice, 'team-launch')  # repeating the same answer is harmless
                self.assertEqual(offer.load(self.root)['decided_in'], 'office')
                offer.record(self.root, other, 'team-launch', owner_change=True)
                self.assertEqual(offer.load(self.root)['choice'], other)

    def test_a_project_that_already_has_dreamdocs_is_not_offered_it(self):
        (self.root / 'docs').mkdir()
        (self.root / 'docs/00-index.md').write_text('# index')
        self.assertFalse(offer.already_present(self.root))
        (self.root / 'memory').mkdir()
        self.assertTrue(offer.already_present(self.root))
        self.assertFalse(offer.should_offer(self.root, 'office'))

    def test_adoption_needs_yes_and_a_stable_release_identity(self):
        commit = '26c7aff57be6ffb4ccd6287c979e3782a0a19b2a'
        with self.assertRaises(ValueError):
            offer.mark_adopted(self.root, 'v1.4.0', commit)
        offer.record(self.root, 'yes', 'team-launch')
        for tag, sha in (('main', commit), ('v1.5.0-rc.2', commit), ('v1.4.0', 'abc')):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                offer.mark_adopted(self.root, tag, sha)
        offer.mark_adopted(self.root, 'v1.4.0', commit)
        offer.record(self.root, 'yes', 'office')
        self.assertEqual(offer.load(self.root)['adopted']['tag'], 'v1.4.0')

    def test_damaged_or_oversized_record_is_an_error_not_a_new_offer(self):
        file = offer.path(self.root)
        file.parent.mkdir(parents=True)
        for body in ('{', json.dumps({'schema': 1, 'choice': 'maybe', 'decided_at': 'x', 'decided_in': 'office', 'asked_in': []}),
                     ' ' * (offer.MAX_BYTES + 1)):
            with self.subTest(body=body[:20]):
                file.write_text(body)
                with self.assertRaises(ValueError):
                    offer.load(self.root)
                with self.assertRaises(ValueError):
                    offer.record(self.root, 'yes', 'office')

    def test_saving_keeps_local_state_out_of_git(self):
        offer.record(self.root, 'skip', 'office')
        self.assertTrue((self.root / '.bot-mode/.gitignore').exists())
        self.assertIn('local', (self.root / '.bot-mode/.gitignore').read_text())


class OnboardingOfferTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.host = self.base / 'host'
        self.host.mkdir()
        self.project = make_project(self.base)
        self.payload = dict(name='Existing', folder=str(self.project), kind='existing', show_dashboard=True)

    def connect(self, **extra):
        payload = {**self.payload, **extra}
        plan = onboarding.prepare(self.host, payload)
        return plan, onboarding.confirm(self.host, payload, plan['version'], confirmed=True)

    def test_no_answer_is_skip_and_planning_writes_nothing(self):
        plan = onboarding.prepare(self.host, self.payload)
        self.assertEqual((plan['dreamdocs']['choice'], plan['dreamdocs']['handoff']), ('skip', None))
        self.assertEqual(plan['configuration']['dreamdocs'], 'skip')
        self.assertFalse(offer.path(self.project).exists())
        self.assertFalse(any(step['id'] == 'adopt_dreamdocs' for step in plan['setup_steps']))

    def test_yes_adds_a_codex_handoff_and_no_dreamdocs_files_are_created(self):
        plan, result = self.connect(dreamdocs='yes')
        step = next(step for step in plan['setup_steps'] if step['id'] == 'adopt_dreamdocs')
        self.assertEqual((step['supported'], step['done']), (False, False))
        self.assertIn('copy nothing until I approve it', step['handoff'])
        self.assertEqual(result['dreamdocs'], {'choice': 'yes', 'recorded': True, 'handoff': step['handoff']})
        self.assertEqual(offer.load(self.project)['choice'], 'yes')
        self.assertFalse((self.project / 'docs').exists() or (self.project / 'memory').exists())

    def test_choice_changes_the_plan_version(self):
        versions = {onboarding.prepare(self.host, {**self.payload, 'dreamdocs': c})['version'] for c in offer.CHOICES}
        self.assertEqual(len(versions), 3)

    def test_invalid_answer_is_rejected(self):
        for bad in ('maybe', True, None, ['yes']):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                onboarding.prepare(self.host, {**self.payload, 'dreamdocs': bad})

    def test_an_earlier_no_wins_over_a_later_yes(self):
        offer.record(self.project, 'no', 'team-launch')
        plan, result = self.connect(dreamdocs='yes')
        self.assertEqual((plan['dreamdocs']['choice'], plan['dreamdocs']['decided']), ('no', True))
        self.assertIsNone(plan['dreamdocs']['handoff'])
        self.assertEqual(offer.load(self.project)['choice'], 'no')
        self.assertEqual(result['dreamdocs']['choice'], 'no')

    def test_existing_dreamdocs_means_nothing_to_install(self):
        (self.project / 'docs').mkdir()
        (self.project / 'docs/00-index.md').write_text('# index')
        (self.project / 'memory').mkdir()
        plan, result = self.connect(dreamdocs='yes')
        self.assertTrue(plan['dreamdocs']['already_present'])
        self.assertIsNone(plan['dreamdocs']['handoff'])
        self.assertIsNone(result['dreamdocs']['handoff'])

    def test_a_failed_record_never_undoes_the_connection(self):
        with mock.patch.object(offer, 'record', side_effect=OSError('disk full')):
            _, result = self.connect(dreamdocs='yes')
        self.assertEqual(result['state'], 'connected')
        self.assertFalse(result['dreamdocs']['recorded'])
        registry = json.loads((self.host / '.bot-mode/local/office-project-roots.json').read_text())
        self.assertTrue(any(p['root'] == str(self.project.resolve()) for p in registry['projects']))

    def test_new_project_records_the_answer_and_lists_the_step_as_remaining(self):
        folder = self.base / 'brand-new'
        payload = {'name': 'Fresh', 'folder': str(folder), 'kind': 'new', 'dreamdocs': 'yes'}
        plan = onboarding.prepare(self.host, payload)
        self.assertEqual(plan['state'], 'ready_to_create')
        self.assertFalse(folder.exists())
        result = onboarding.confirm(self.host, payload, plan['version'], confirmed=True)
        self.assertEqual(result['state'], 'created_pending_team')
        self.assertEqual([s['id'] for s in result['remaining']], ['appoint_team', 'adopt_dreamdocs'])
        self.assertEqual(offer.load(folder)['choice'], 'yes')


class TeamCliTests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys.stdout, 'reconfigure', create=True):
            code = team.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_show_set_refuse_and_owner_change(self):
        with tempfile.TemporaryDirectory() as temp:
            code, out, _ = self.run_cli('dreamdocs-offer', '--project', temp)
            self.assertEqual((code, json.loads(out)['ask_now'], json.loads(out)['record']), (0, True, None))
            code, out, _ = self.run_cli('dreamdocs-offer', '--project', temp, '--set', 'no')
            self.assertEqual((code, json.loads(out)['record']['choice'], json.loads(out)['ask_now']), (0, 'no', False))
            code, _, err = self.run_cli('dreamdocs-offer', '--project', temp, '--set', 'yes')
            self.assertEqual(code, 2)
            self.assertIn('owner already answered no', err)
            code, out, _ = self.run_cli('dreamdocs-offer', '--project', temp, '--set', 'yes', '--owner-change')
            self.assertEqual((code, json.loads(out)['record']['choice']), (0, 'yes'))
            code, _, err = self.run_cli('dreamdocs-offer', '--project', temp, '--adopted-tag', 'v1.4.0')
            self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()

