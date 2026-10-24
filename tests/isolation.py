"""Process-wide test isolation from the owner's real user locations (#310).

Tests must never write to the real ~/.codex-bot-mode config (which names the Chief of Staff folder whose
knowledge/projects.md every team install appends to), ~/.agents, ~/plugins or the Codex config. isolate() points the
config variable and the home directories at a throw-away folder once per process; it is imported by tests/conftest.py
(pytest), tests/test_000_isolation.py (unittest discover, imported first) and by test modules that install teams.
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

REAL_HOME = Path.home()
_root = None


def isolate():
    global _root
    if _root is not None:
        return _root
    _root = Path(tempfile.mkdtemp(prefix='cbm-test-isolation-'))
    atexit.register(shutil.rmtree, str(_root), True)
    home = _root / 'home'
    home.mkdir()
    gitconfig = REAL_HOME / '.gitconfig'
    if gitconfig.is_file():
        os.environ.setdefault('GIT_CONFIG_GLOBAL', str(gitconfig))  # read only, keeps git identity for fixtures
    os.environ['CODEX_BOT_MODE_CONFIG'] = str(_root / 'no-config.json')
    os.environ['CODEX_HOME'] = str(home / '.codex')
    os.environ['HOME'] = os.environ['USERPROFILE'] = str(home)
    return _root
