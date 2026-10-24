"""Regression for #310: installing a team in a test must not touch the owner's real Chief of Staff list."""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / 'scripts'))
import isolation  # noqa: E402

isolation.isolate()
from team_room import teams  # noqa: E402

INSTALL = ('import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from team_room import teams;'
           ' teams.install(teams.plan_team(Path(sys.argv[2]), "development", team_answers={"tracking_mode": "local"}))')


def snapshot(file):
    stat = file.stat()
    return stat.st_mtime_ns, hashlib.sha256(file.read_bytes()).hexdigest()


class RealConfigIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        # A stand-in for the owner's real home: ~/.codex-bot-mode/config.json naming a Chief of Staff folder.
        self.home = base / 'home'
        cos = base / 'cos'
        (cos / 'knowledge').mkdir(parents=True)
        self.projects = cos / 'knowledge' / 'projects.md'
        self.projects.write_text('| Project | Path | Notes |\n', encoding='utf-8')
        (self.home / '.codex-bot-mode').mkdir(parents=True)
        (self.home / '.codex-bot-mode' / 'config.json').write_text('{"chief_of_staff": %s}' % __import__('json').dumps(str(cos)))
        self.project = base / 'project'
        self.project.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, env):
        return subprocess.run([sys.executable, '-c', INSTALL, str(ROOT / 'scripts'), str(self.project)], env=env,
                              capture_output=True, text=True, timeout=120)

    def test_suite_isolation_points_config_away_from_every_real_home(self):
        self.assertNotEqual(Path(os.environ['USERPROFILE']), isolation.REAL_HOME)
        self.assertTrue(str(teams.config_path()).startswith(str(isolation._root)))
        self.assertFalse(teams.config_path().exists())

    def test_install_under_the_suite_environment_leaves_the_real_target_untouched(self):
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home))  # suite variables stay set
        before = snapshot(self.projects)
        result = self.run_install(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(self.projects), before)

    def test_without_isolation_the_same_install_would_write_the_row(self):
        env = {k: v for k, v in os.environ.items() if k != 'CODEX_BOT_MODE_CONFIG'}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home))
        result = self.run_install(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.project.name, self.projects.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
