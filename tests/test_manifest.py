import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from team_room import manifest  # noqa: E402

SAMPLE = """# comment
bot_id: chief-of-staff
version: 1
skills:
  - codex-chief-of-staff
routes_to: [project-director, project-manager]
roles:
  - template: manager
    bot: manager
  - template: developer
permissions:
  owner_tools: ask-before-irreversible
parameters:
  display_name:
    default: Chief of Staff
    value: Fos  # chosen by the owner
    question: What should I be called? The role stays Chief of Staff.
  model:
    default: inherit
    value: null
title: "a: b # not a comment"
"""


class ManifestTests(unittest.TestCase):
    def test_parse(self):
        data = manifest.loads(SAMPLE)
        self.assertEqual(data['version'], 1)
        self.assertEqual(data['skills'], ['codex-chief-of-staff'])
        self.assertEqual(data['routes_to'], ['project-director', 'project-manager'])
        self.assertEqual(data['roles'], [{'template': 'manager', 'bot': 'manager'}, {'template': 'developer'}])
        self.assertEqual(data['parameters']['display_name']['value'], 'Fos')
        self.assertIsNone(data['parameters']['model']['value'])
        self.assertEqual(data['title'], 'a: b # not a comment')

    def test_round_trip(self):
        data = manifest.loads(SAMPLE)
        self.assertEqual(manifest.loads(manifest.dumps(data)), data)

    def test_rejects(self):
        for bad in ('a: 1\na: 2', 'a:\n\tb: 1', 'a: {b: 1}', 'just text', 'a: 1\n  b: 2'):
            with self.assertRaises(manifest.ManifestError, msg=bad):
                manifest.loads(bad)

    def test_live_chief_of_staff_manifest(self):
        path = Path(__file__).resolve().parents[1] / 'packages/chief-of-staff-v1/agent/agent.yaml'
        data = manifest.load(path)
        self.assertEqual(data['bot_id'], 'chief-of-staff')
        self.assertIn('display_name', data['parameters'])


if __name__ == '__main__':
    unittest.main()
