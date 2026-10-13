"""Durable raster attachments, scoped to one conversation channel."""
import json
from pathlib import Path
import re
import uuid

MAX_BYTES = 8 * 1024 * 1024
MARKER = re.compile(r'\n\n\[Codex Office images:(\{[^\n]+\})\]\nInspect the attached images[^\n]*$')


class Images:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def save(self, data, mime):
        formats = [('image/png', 'png', data.startswith(b'\x89PNG\r\n\x1a\n')),
                   ('image/jpeg', 'jpg', data.startswith(b'\xff\xd8\xff')),
                   ('image/gif', 'gif', data.startswith((b'GIF87a', b'GIF89a'))),
                   ('image/webp', 'webp', data[:4] == b'RIFF' and data[8:12] == b'WEBP')]
        match = next((f for f in formats if f[0] == mime and f[2]), None)
        if not data or len(data) > MAX_BYTES or not match:
            raise ValueError('Use PNG, JPEG, GIF or WebP images up to 8 MB')
        self.root.mkdir(parents=True, exist_ok=True)
        key = uuid.uuid4().hex
        path = self.root / (key + '.' + match[1])
        path.write_bytes(data)
        (self.root / (key + '.json')).write_text(json.dumps({'mime': mime, 'extension': match[1]}), encoding='utf-8')
        return {'id': key, 'path': str(path)}

    def path(self, key):
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{32}', key):
            raise ValueError('Invalid image ID')
        try:
            meta = json.loads((self.root / (key + '.json')).read_text(encoding='utf-8'))
            if meta['extension'] not in ('png', 'jpg', 'gif', 'webp'): raise ValueError()
            path = (self.root / (key + '.' + meta['extension'])).resolve(strict=True)
            if path.parent != self.root: raise ValueError()
            return path, meta['mime']
        except (OSError, KeyError, ValueError):
            raise ValueError('Image is unavailable in this conversation') from None

    def read(self, key):
        path, mime = self.path(key)
        return path.read_bytes(), mime

    def attach(self, text, keys):
        if not isinstance(text, str) or not isinstance(keys, list) or len(keys) > 4:
            raise ValueError('Enter text and attach at most 4 images')
        if not keys: return text
        if MARKER.search(text): raise ValueError('Attachment metadata is reserved')
        items = [{'id': key, 'path': str(self.path(key)[0])} for key in keys]
        return ((text.strip() or 'Please describe the attached images.') + '\n\n[Codex Office images:'
                + json.dumps({'images': items}) + ']\nInspect the attached images before answering; '
                'use your image-viewing tool on these local paths when available.')

    def items(self, text):
        match = MARKER.search(text)
        if not match: return []
        try:
            items = json.loads(match[1])['images']
            if not isinstance(items, list) or not 1 <= len(items) <= 4: return []
            if any(str(self.path(item['id'])[0]) != item['path'] for item in items): return []
            return items
        except (ValueError, KeyError, TypeError):
            return []

    def paths(self, text):
        return [item['path'] for item in self.items(text)]

    def project(self, message, scope):
        items = self.items(message['text'])
        if not items: return message
        return {**message, 'text': MARKER.sub('', message['text']),
                'images': [{'id': item['id'], 'scope': scope} for item in items]}
