"""Find the Codex Desktop tool pipe and the packaged Codex runtime without any inherited Manager context (#421).

The pipe name is not a secret: the operating system lists it, and the packaged app-tools server connects to it with the
caller's own Windows account. A standalone relay can therefore start without the Manager chat and find the pipe again
after Codex Desktop restarts."""
import os
from pathlib import Path

PIPE_DIRECTORY = '\\\\.\\pipe\\'
PIPE_PREFIX = 'codex-app-tools-'
PACKAGES = 'C:/Program Files/WindowsApps'
PACKAGE_PREFIX = 'OpenAI.Codex_'


def tool_pipes(listing=None):
    """Full paths of the Desktop tool pipes that exist now, newest name order is irrelevant: callers try each."""
    if listing is None:
        if os.name != 'nt': return []
        try: listing = os.listdir(PIPE_DIRECTORY)
        except OSError: return []
    return [PIPE_DIRECTORY + name for name in sorted(listing) if name.startswith(PIPE_PREFIX)]


def _version(name):
    try: return tuple(int(part) for part in name.split('_')[1].split('.'))
    except (IndexError, ValueError): return ()


def find_codex_binary(environ=None, packages=PACKAGES):
    """Path of codex.exe: CBM_CODEX_BINARY when it names a file, else the newest installed Codex package.
    Raises ValueError with the remedy when none is found."""
    environ = os.environ if environ is None else environ
    override = environ.get('CBM_CODEX_BINARY')
    if override and Path(override).is_file(): return Path(override)
    try: names = [entry for entry in os.listdir(packages) if entry.startswith(PACKAGE_PREFIX)]
    except OSError: names = []
    for name in sorted(names, key=_version, reverse=True):
        candidate = Path(packages) / name / 'app' / 'resources' / 'codex.exe'
        if candidate.is_file() and candidate.stat().st_size > 0: return candidate
    raise ValueError('No Codex runtime found. Install Codex Desktop, or set CBM_CODEX_BINARY to its codex.exe.')
