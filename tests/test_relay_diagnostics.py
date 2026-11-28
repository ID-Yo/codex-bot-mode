import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from scripts.team_room import desktop_relay


class Bridge:
    pid = 0


class DiagnosticsTests(unittest.TestCase):
    def test_proof_of_life_file_and_state_change_line(self):
        said = []
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        stop = desktop_relay.start_diagnostics(root, Bridge(), say=lambda text, flush=True: said.append(text), interval=0.05)
        self.addCleanup(stop)  # runs before the folder is removed: nothing writes into it any more
        deadline = time.time() + 5
        while time.time() < deadline and not (root / 'relay-alive.json').exists():
            time.sleep(0.05)
        stop()  # the file is not replaced while it is read
        data = json.loads((root / 'relay-alive.json').read_text())
        self.assertIn('parent_alive', data)
        self.assertIn('uptime_seconds', data)
        self.assertTrue(any('Desktop relay alive' in line for line in said))

    def test_stopping_ends_the_writer_thread(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        stop = desktop_relay.start_diagnostics(Path(folder.name), Bridge(), say=lambda text, flush=True: None, interval=0.05)
        self.addCleanup(stop)
        self.assertTrue(any(thread.name == 'relay-diagnostics' for thread in threading.enumerate()))
        stop()
        self.assertFalse(any(thread.name == 'relay-diagnostics' for thread in threading.enumerate()))


if __name__ == '__main__':
    unittest.main()
