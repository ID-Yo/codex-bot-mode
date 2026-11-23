"""Process-wide test isolation from the owner's real user locations (#310).

Tests must never write to the real ~/.codex-bot-mode config (which names the Chief of Staff folder whose
knowledge/projects.md every team install appends to), ~/.agents, ~/plugins or the Codex config. isolate() points the
config variable and the home directories at a throw-away folder once per process; it is imported by tests/conftest.py
(pytest), tests/test_000_isolation.py (unittest discover, imported first) and by test modules that install teams.

On Windows the temp directory can be reported in its 8.3 short form (GitHub runners: C:\\Users\\RUNNER~1\\AppData\\Local\\Temp
for the user runneradmin) while Path.resolve() returns the long form, so equal paths compare unequal. isolate() therefore
switches tempfile and TEMP/TMP to the long form before any test creates a directory (#433).
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

REAL_HOME = Path.home()
_root = None


def _use_long_tempdir():
    """Make tempfile (and child processes via TEMP/TMP) use the canonical long form of the temp directory."""
    if os.name != 'nt':
        return
    long_form = os.path.realpath(tempfile.gettempdir())
    tempfile.tempdir = long_form
    os.environ['TEMP'] = os.environ['TMP'] = long_form


def isolate():
    global _root
    if _root is not None:
        return _root
    _use_long_tempdir()
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
