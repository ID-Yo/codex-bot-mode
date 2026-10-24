"""The relay client connects to IPv4 loopback only, also for descriptors that still say localhost."""
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.team_room.desktop_relay import create_relay, RelayBridge
from scripts.team_room.work import MANAGER, Uncertain


class FakeBridge:
    origin = MANAGER

    def call(self, name, arguments):
        return {'threads': [], 'origin': self.origin}


class RelayLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = create_relay(FakeBridge(), self.root, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.path = self.root / 'relay.json'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5.0)
        self.temp.cleanup()

    def resolved_during(self, action):
        seen = []
        real = socket.getaddrinfo

        def spy(host, *args, **kwargs):
            seen.append(host)
            return real(host, *args, **kwargs)

        with patch('socket.getaddrinfo', spy):
            action()
        return seen

    def test_new_descriptor_names_ipv4_loopback(self):
        self.assertTrue(json.loads(self.path.read_text())['url'].startswith('http://127.0.0.1:'))

    def test_calls_never_resolve_localhost(self):
        seen = self.resolved_during(lambda: RelayBridge(self.root).call('list_threads', {}))
        self.assertEqual(set(seen), {'127.0.0.1'})

    def test_legacy_localhost_descriptor_still_works_without_localhost_lookup(self):
        data = json.loads(self.path.read_text())
        data['url'] = data['url'].replace('127.0.0.1', 'localhost')
        self.path.write_text(json.dumps(data))
        result = {}
        seen = self.resolved_during(lambda: result.update(RelayBridge(self.root).call('list_threads', {})))
        self.assertEqual(result['origin'], MANAGER)
        self.assertNotIn('localhost', seen)
        self.assertEqual(set(seen), {'127.0.0.1'})

    def test_relay_url_helper_normalizes_and_rejects(self):
        from scripts.team_room.relay_url import relay_base_url
        self.assertEqual(relay_base_url("http://LOCALHOST:24243"), "http://127.0.0.1:24243")
        self.assertEqual(relay_base_url("http://127.0.0.1:1"), "http://127.0.0.1:1")
        for url in ("http://localhost:24243@example.invalid", "http://localhost.evil.invalid:24243", "http://127.0.0.1.evil.invalid:24243", None, 5, "http://127.0.0.1:0"):
            with self.assertRaises(ValueError):
                relay_base_url(url)

    def test_other_hosts_stay_rejected(self):
        saved = json.loads(self.path.read_text())
        for url in ('http://127.0.0.2:24243', 'http://[::1]:24243', 'http://example.com:24243', 'http://127.0.0.1.evil.test:24243'):
            self.path.write_text(json.dumps({**saved, 'url': url}))
            with self.assertRaises(ValueError):
                RelayBridge(self.root)


if __name__ == '__main__':
    unittest.main()
