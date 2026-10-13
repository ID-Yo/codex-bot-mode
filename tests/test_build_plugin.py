import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_plugin  # noqa: E402


class PluginBuildTests(unittest.TestCase):
    def test_built_plugin_launches_a_team_and_rebuild_keeps_project_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = build_plugin.build(Path(tmp) / 'codex-bot-mode')
            meta = json.loads((out / '.codex-plugin/plugin.json').read_text(encoding='utf-8'))
            self.assertEqual((meta['name'], meta['skills']), ('codex-bot-mode', './skills/'))
            self.assertTrue((out / 'skills/codex-team-launch/SKILL.md').exists())
            project = Path(tmp) / 'demo'
            project.mkdir()
            run = subprocess.run([sys.executable, '-B', str(out / 'scripts/team.py'), 'install-team', 'sales',
                                  '--project', str(project), '--approval', 'once', '--confirm'],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            roster = (project / '.bot-mode/roster.json').read_bytes()
            build_plugin.build(out)
            self.assertEqual((project / '.bot-mode/roster.json').read_bytes(), roster)
            links = subprocess.run([sys.executable, '-B', str(project / '.bot-mode/roster.py'), '--project',
                                    str(project), 'links'], capture_output=True, text=True)
            self.assertEqual(len(json.loads(links.stdout)), 4)
            other = Path(tmp) / 'other'
            other.mkdir()
            with self.assertRaises(SystemExit):
                build_plugin.build(other)


if __name__ == '__main__':
    unittest.main()
