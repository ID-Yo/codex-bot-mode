import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from scripts.team_room.settings import BotSettings


class BotSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / 'agents/project-manager'
        self.profile.mkdir(parents=True)
        (self.profile / 'SOUL.md').write_text('Original', encoding='utf-8')
        self.settings = BotSettings(self.root)
    def test_versioned_save_and_conflict(self):
        before = self.settings.read('manager', 'SOUL.md')
        self.settings.save('manager', 'SOUL.md', 'Changed', before['version'])
        self.assertEqual(self.settings.read('manager', 'SOUL.md')['content'], 'Changed')
        with self.assertRaises(ValueError): self.settings.save('manager', 'SOUL.md', 'Lost change', before['version'])
    def test_arbitrary_paths_and_symlinks_refused(self):
        for filename in ('../SOUL.md', 'auth.json', 'memory/episodes/x.json'):
            with self.assertRaises(ValueError): self.settings.read('manager', filename)
        with self.assertRaises(ValueError): self.settings.read('unknown', 'SOUL.md')
        target = self.root / 'outside.md'; target.write_text('Secret')
        (self.profile / 'SOUL.md').unlink()
        try: (self.profile / 'SOUL.md').symlink_to(target)
        except OSError: return
        with self.assertRaises(ValueError): self.settings.read('manager', 'SOUL.md')

    def test_profile_identity_cannot_be_removed(self):
        path = self.profile / 'PROFILE.md'
        path.write_text('- Bot ID: `project-manager`\n',encoding='utf-8')
        before = self.settings.read('manager','PROFILE.md')
        with self.assertRaises(ValueError): self.settings.save('manager','PROFILE.md','Different identity',before['version'])
        self.assertEqual(path.read_text(encoding='utf-8'),before['content'])

    def test_memory_returns_validated_lessons_not_contract(self):
        lesson={'key':'lesson-one','text':'Remember this outcome.','source':'docs/proof.md','quote':'Verified outcome','episode':'episode-one'}
        with patch('scripts.team_room.profiles._profile', return_value=('Soul','Profile','Contract',[lesson])) as recall:
            row=self.settings.read('manager','memory')
        recall.assert_called_once_with('manager',self.root)
        self.assertTrue(row['readonly'])
        self.assertIn(lesson['text'],row['content'])
        self.assertIn(lesson['source'],row['content'])
        self.assertNotIn('Contract',row['content'])
        with self.assertRaises(ValueError): self.settings.save('manager','memory','Overwrite',row['version'])

    def test_empty_memory_is_explicit(self):
        with patch('scripts.team_room.profiles._profile', return_value=('Soul','Profile','Contract',[])):
            self.assertIn('No current validated lessons', self.settings.read('developer','memory')['content'])

    def test_unchanged_save_preserves_source_hash_bytes(self):
        path=self.profile/'SOUL.md';path.write_bytes(b'Original\nSecond line\n')
        before=self.settings.read('manager','SOUL.md')
        self.settings.save('manager','SOUL.md',before['content'],before['version'])
        self.assertEqual(path.read_bytes(),b'Original\nSecond line\n')


class OfficeSettingsTests(unittest.TestCase):
    def test_persist_conflict_and_validation(self):
        from scripts.team_room.settings import OfficeSettings
        with tempfile.TemporaryDirectory() as root:
            settings=OfficeSettings(root)
            original=settings.read()
            saved=settings.save('Engineering Office',original['version'])
            self.assertEqual(OfficeSettings(root).read()['name'],'Engineering Office')
            with self.assertRaises(ValueError): settings.save('Stale',original['version'])
            for invalid in ['', ' '*2, 'x'*81, 'Bad\nname', None]:
                with self.assertRaises(ValueError): settings.save(invalid,saved['version'])
