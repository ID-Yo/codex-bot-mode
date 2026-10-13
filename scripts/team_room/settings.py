"""Explicit profile files, optimistic writes and no general filesystem access."""
import hashlib
from pathlib import Path
import threading
import uuid

from .store import BOTS

FILES = ('SOUL.md', 'PROFILE.md', 'memory/README.md')


def version(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


class BotSettings:
    lock = threading.Lock()
    def __init__(self, project):
        self.project = Path(project).resolve(strict=True)
    def path(self, bot, filename):
        if bot not in BOTS or filename not in FILES: raise ValueError('Select a supported bot profile file')
        from . import project as served
        path = served.profile_location(bot, self.project)[0] / filename
        if any(p.is_symlink() or p.is_junction() for p in [path, *path.parents] if p != self.project and p.is_relative_to(self.project)):
            raise ValueError('Linked profile paths cannot be edited')
        if not path.resolve(strict=True).is_relative_to(self.project): raise ValueError('Profile is outside this project')
        return path
    def read(self, bot, filename):
        if filename == 'memory':
            if bot not in BOTS: raise ValueError('Unknown bot')
            from .profiles import _profile
            lessons = _profile(bot, self.project)[3]
            from . import project as served
            try:
                location = served.profile_location(bot, self.project)[4]
            except ValueError:
                location = 'not installed'
            lines = [f'# {served.label(bot)} learned memory', '', f'Current validated lessons: {len(lessons)}', '']
            if not lessons: lines.append('No current validated lessons. This bot has no active learned memory.')
            for lesson in lessons:
                lines.extend([f'## {lesson["key"]}', '', lesson['text'], '',
                              f'Source: {lesson["source"]}', f'Evidence: {lesson["quote"]}',
                              f'Episode: {lesson.get("episode", "")}', ''])
            content = '\n'.join(lines)
            return {'bot':bot, 'file':'memory', 'path':location, 'content':content,
                    'readonly':True, 'lessons':lessons, 'version':version(content)}
        path = self.path(bot, filename)
        content = path.read_text(encoding='utf-8')
        return {'bot':bot, 'file':filename, 'path':path.relative_to(self.project).as_posix(), 'content':content, 'version':version(content)}
    def save(self, bot, filename, content, expected):
        if bot not in BOTS or filename not in FILES: raise ValueError('Learned memory is read-only; select an editable profile file')
        if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 12000:
            raise ValueError('Profile content must be nonempty and at most 12000 bytes')
        if filename == 'PROFILE.md':
            from . import project as served
            identity = served.profile_location(bot, self.project)[3]
            if f'- Bot ID: `{identity}`' not in content.splitlines():
                raise ValueError('Keep the existing Bot ID in PROFILE.md')
        with self.lock:
            before = self.read(bot, filename)
            if before['version'] != expected: raise ValueError('File changed since opening. Reload before saving.')
            if before['content'] == content: return before
            path = self.path(bot, filename)
            temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                raw = path.read_bytes()
                normalized = content.replace('\r\n', '\n')
                if b'\r\n' in raw: normalized = normalized.replace('\n', '\r\n')
                temporary.write_bytes(normalized.encode('utf-8'))
                temporary.replace(path)
            finally: temporary.unlink(missing_ok=True)
            return self.read(bot, filename)


class OfficeSettings:
    lock = threading.Lock()

    def __init__(self, root):
        self.path = Path(root) / 'office-settings.json'

    def read(self):
        import json
        name = json.loads(self.path.read_text(encoding='utf-8'))['name'] if self.path.exists() else 'Codex Office'
        return {'name':name, 'version':version(name)}

    def save(self, name, expected):
        import json
        if not isinstance(name,str) or not name.strip() or len(name.strip()) > 80 or any(ord(c)<32 for c in name):
            raise ValueError('Office name must contain 1–80 characters without control characters')
        with self.lock:
            if self.read()['version'] != expected: raise ValueError('Office changed since opening. Reload before saving.')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
            try:
                temporary.write_text(json.dumps({'name':name.strip()},ensure_ascii=False),encoding='utf-8')
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return self.read()
