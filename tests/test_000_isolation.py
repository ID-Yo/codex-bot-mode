"""Imported first by 'unittest discover' (alphabetical order): isolates the whole run (see isolation.py)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolation  # noqa: E402

isolation.isolate()


class IsolationActive(unittest.TestCase):
    def test_config_and_home_are_throw_away(self):
        import os
        for name in ('CODEX_BOT_MODE_CONFIG', 'CODEX_HOME', 'HOME', 'USERPROFILE'):
            self.assertTrue(os.environ[name].startswith(str(isolation._root)), name)


if __name__ == '__main__':
    unittest.main()
