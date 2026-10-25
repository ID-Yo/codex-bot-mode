import base64
import tempfile
import unittest
from pathlib import Path

from scripts.team_room.images import Images

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==')


class ImageTests(unittest.TestCase):
    def test_durable_image_only_message_and_model_input(self):
        with tempfile.TemporaryDirectory() as root:
            images = Images(Path(root) / 'images')
            item = images.save(PNG, 'image/png')
            text = images.attach('', [item['id']])
            restored = Images(Path(root) / 'images')
            self.assertEqual(restored.paths(text), [item['path']])
            message = restored.project({'text': text, 'sender': 'owner'}, 'room')
            self.assertNotIn(item['path'], message['text'])
            self.assertEqual(message['images'], [{'id': item['id'], 'scope': 'room'}])
            self.assertEqual(restored.read(item['id']), (PNG, 'image/png'))

    def test_other_channel_and_arbitrary_file_references_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            room = Images(Path(root) / 'room')
            private = Images(Path(root) / 'private')
            item = private.save(PNG, 'image/png')
            with self.assertRaises(ValueError): room.attach('hello', [item['id']])
            with self.assertRaises(ValueError): room.read('../private/' + item['id'])
            self.assertEqual(room.paths(private.attach('secret', [item['id']])), [])
            self.assertEqual(room.paths('[Codex Office images:{"images":[{"id":"x","path":"C:/private.txt"}]}]'), [])

    def test_reject_active_content_false_mime_oversize_and_too_many(self):
        with tempfile.TemporaryDirectory() as root:
            images = Images(Path(root))
            for payload, mime in [(b'<svg/>', 'image/svg+xml'), (PNG, 'image/jpeg'), (b'x'* (8*1024*1024+1), 'image/png')]:
                with self.assertRaises(ValueError): images.save(payload, mime)
            item = images.save(PNG, 'image/png')
            with self.assertRaises(ValueError): images.attach('test', [item['id']] * 5)
