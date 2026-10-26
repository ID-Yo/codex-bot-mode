"""Explicit profile files, optimistic writes and no general filesystem access."""
import hashlib
import os
from pathlib import Path
import threading
import uuid

from . import project as served, registry, roster
from .store import BOTS

FILES = ('SOUL.md', 'PROFILE.md', 'memory/README.md')


def version(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


class BotSettings:
    lock = threading.Lock()

    def __init__(self, project, registry_file=None):
        self.project = Path(project).resolve(strict=True)
        self.registry_file = Path(registry_file).resolve() if registry_file else None

    def _projects_map(self):
        projects = {'current': self.project}
        reg_file = self.registry_file or registry.registry_path(self.project)
        if reg_file and Path(reg_file).exists():
            try:
                reg_data = registry.load_registry(reg_file)
                for p in reg_data.get('projects', []):
                    pid = p.get('id')
                    proot = p.get('root')
                    if pid and proot:
                        resolved = Path(proot).resolve()
                        if resolved.exists():
                            projects[pid] = resolved
                            if resolved == self.project:
                                projects['current'] = resolved
            except Exception:
                pass
        return projects

    def _resolve_project(self, selector, projects):
        if not selector:
            return None, None
        if isinstance(selector, Path):
            cand = selector.resolve()
            if cand == self.project:
                return 'current', self.project
            for pid, proot in projects.items():
                if proot == cand:
                    return pid, cand
            if cand.exists():
                return cand.name, cand
            raise ValueError(f"Project '{selector}' not found in registry")
        s = str(selector).strip()
        if not s:
            return None, None
        if s == 'current':
            return 'current', self.project
        if s in projects:
            return s, projects[s]
        try:
            cand = Path(s).resolve()
            if cand == self.project:
                return 'current', self.project
            for pid, proot in projects.items():
                if proot == cand:
                    return pid, cand
        except (OSError, ValueError):
            pass
        raise ValueError(f"Project '{selector}' not found in registry")

    def _resolve_agent(self, bot, project=None):
        if not isinstance(bot, str) or not bot.strip():
            raise ValueError('Select a supported bot profile file')

        projects = self._projects_map()

        prefix_pid = None
        prefix_root = None
        if ':' in bot:
            p_prefix, b_suffix = bot.split(':', 1)
            p_prefix = p_prefix.strip()
            actual_bot_id = b_suffix.strip()
            prefix_pid, prefix_root = self._resolve_project(p_prefix, projects)
        else:
            actual_bot_id = bot.strip()

        if not actual_bot_id:
            raise ValueError('Select a supported bot profile file')

        explicit_pid = None
        explicit_root = None
        if project:
            explicit_pid, explicit_root = self._resolve_project(project, projects)

        if prefix_root and explicit_root:
            if prefix_root != explicit_root:
                raise ValueError(f"Conflicting project selectors: bot prefix '{p_prefix}' does not match project '{project}'")
            target_pid = prefix_pid or explicit_pid
            target_root = prefix_root
        elif prefix_root:
            target_pid = prefix_pid
            target_root = prefix_root
        elif explicit_root:
            target_pid = explicit_pid
            target_root = explicit_root
        else:
            matching = []
            seen_roots = set()
            for pid, proot in projects.items():
                if proot in seen_roots:
                    continue
                seen_roots.add(proot)
                try:
                    rdata = roster.load(proot)
                    parts = [p['bot'] for p in rdata.get('participants', [])]
                    if actual_bot_id in parts or (proot == self.project and actual_bot_id in served.LEGACY):
                        matching.append((pid, proot))
                except Exception:
                    if proot == self.project and (actual_bot_id in BOTS or actual_bot_id in served.LEGACY):
                        matching.append((pid, proot))

            if len(matching) == 1:
                target_pid, target_root = matching[0]
            elif len(matching) > 1:
                curr_match = next((m for m in matching if m[1] == self.project), None)
                if curr_match:
                    target_pid, target_root = curr_match
                else:
                    raise ValueError(f"Ambiguous agent '{actual_bot_id}' found in multiple projects; specify project")
            else:
                if actual_bot_id in BOTS or actual_bot_id in served.LEGACY:
                    target_pid, target_root = 'current', self.project
                else:
                    raise ValueError(f"Unknown agent '{actual_bot_id}'")

        # 3. Locate workspace and profile in target_root
        workspace = None
        helper_bot_id = actual_bot_id
        role = actual_bot_id.replace('-', ' ').title()
        try:
            rdata = roster.load(target_root)
            part = next((p for p in roster.active(rdata) if p.get('bot') == actual_bot_id), None)
            if not part:
                part = next((p for p in rdata.get('participants', []) if p.get('bot') == actual_bot_id), None)
            if part:
                workspace = part.get('workspace')
                helper_bot_id = part.get('bot_id') or actual_bot_id
                role = part.get('role') or role
        except Exception:
            pass

        if not workspace and target_root == self.project and actual_bot_id in served.LEGACY:
            workspace, helper_bot_id = served.LEGACY[actual_bot_id]

        if not workspace:
            raise ValueError(f"Unknown agent '{actual_bot_id}' in project '{target_root.name}'")

        folder = target_root / workspace
        if folder.exists() and not registry._is_safe_contained(folder, target_root):
            raise ValueError('Profile is outside this project')

        profile_dir = None
        memory_project = target_root
        memory_location = 'not installed'
        if (folder / 'agent').is_dir() and ((folder / 'agent' / 'agent.yaml').is_file() or (folder / 'agent' / 'PROFILE.md').is_file()):
            profile_dir = folder / 'agent'
            memory_project = folder
            memory_location = f"{workspace}/agent/memory"
        elif folder.is_dir():
            profile_dir = folder
            memory_project = target_root
            memory_location = f".bot-mode/agents/{helper_bot_id}"

        if profile_dir and not registry._is_safe_contained(profile_dir, target_root):
            raise ValueError('Profile is outside this project')

        return target_root, target_pid, actual_bot_id, profile_dir, memory_project, helper_bot_id, memory_location, role, folder

    def path(self, bot, filename, project=None):
        if filename not in FILES and filename != 'memory':
            raise ValueError('Select a supported bot profile file')
        target_root, target_pid, actual_bot_id, profile_dir, memory_project, _, _, _, _ = self._resolve_agent(bot, project)
        if not profile_dir or not profile_dir.is_dir():
            raise FileNotFoundError(f"Agent profile folder not found for '{actual_bot_id}'")

        if filename == 'memory/README.md':
            target_path = profile_dir / 'memory' / 'README.md'
            if not target_path.exists() and (memory_project / 'agent' / 'memory' / 'README.md').exists():
                target_path = memory_project / 'agent' / 'memory' / 'README.md'
        else:
            target_path = profile_dir / filename
        if any(p.is_symlink() or p.is_junction() for p in [target_path, *target_path.parents] if p != target_root and p.is_relative_to(target_root)):
            raise ValueError('Linked profile paths cannot be edited')
        if target_path.exists() and not registry._is_safe_contained(target_path, target_root):
            raise ValueError('Profile is outside this project')
        elif not target_path.exists() and not registry._is_safe_contained(target_path.parent, target_root):
            raise ValueError('Profile is outside this project')
        return target_path

    def read(self, bot, filename, project=None):
        if filename not in FILES and filename != 'memory':
            raise ValueError('Select a supported bot profile file')
        target_root, target_pid, actual_bot_id, profile_dir, memory_project, helper_bot_id, memory_location, role, folder = self._resolve_agent(bot, project)

        if filename == 'memory':
            helper_path = (folder / 'scripts' / 'bot_mode.py') if (profile_dir and profile_dir == folder / 'agent') else (target_root / 'scripts' / 'bot_mode.py')
            if any(p.is_symlink() or p.is_junction() for p in [helper_path, *helper_path.parents] if p != target_root and p.is_relative_to(target_root)):
                raise ValueError('Linked helper paths cannot be executed')
            if helper_path.exists() and not registry._is_safe_contained(helper_path, target_root):
                raise ValueError('Helper path escapes project root')

            memory_dir = (memory_project / 'agent' / 'memory') if (profile_dir and profile_dir == folder / 'agent') else (target_root / '.bot-mode' / 'agents' / helper_bot_id)
            if memory_dir.exists():
                if not registry._is_safe_contained(memory_dir, target_root):
                    raise ValueError('Memory path escapes project root')
                for p in [memory_dir, *memory_dir.parents]:
                    if p != target_root and p.is_relative_to(target_root) and (p.is_symlink() or p.is_junction()):
                        raise ValueError('Linked memory paths cannot be read')

            from .profiles import _profile
            try:
                prof_res = _profile(actual_bot_id, target_root)
                lessons = prof_res[3]
                if not isinstance(lessons, list):
                    raise ValueError(f"Memory recall for agent '{actual_bot_id}' did not return lessons")
            except ValueError:
                raise
            except (FileNotFoundError, OSError, ImportError) as e:
                raise ValueError(f"Memory helper unavailable for agent '{actual_bot_id}': {e}")
            except Exception as e:
                raise ValueError(f"Memory recall failed for agent '{actual_bot_id}': {e}")

            meta = (profile_dir / 'agent.yaml') if profile_dir else None
            label_name = None
            if meta and meta.exists():
                try:
                    from . import manifest
                    label_name = (manifest.load(meta).get('parameters') or {}).get('display_name', {}).get('value')
                except Exception:
                    pass
            if not label_name:
                label_name = role

            lines = [f'# {label_name} learned memory', '', f'Current validated lessons: {len(lessons)}', '']
            if not lessons:
                lines.append('No current validated lessons. This bot has no active learned memory.')
            for lesson in lessons:
                lines.extend([f'## {lesson["key"]}', '', lesson['text'], '',
                              f'Source: {lesson["source"]}', f'Evidence: {lesson["quote"]}',
                              f'Episode: {lesson.get("episode", "")}', ''])
            content = '\n'.join(lines)
            return {'bot': bot, 'bot_id': actual_bot_id, 'project_id': target_pid,
                    'file': 'memory', 'path': memory_location, 'content': content,
                    'readonly': True, 'lessons': lessons, 'version': version(content)}

        if filename == 'memory/README.md':
            target_path = profile_dir / 'memory' / 'README.md'
            if not target_path.exists() and (memory_project / 'agent' / 'memory' / 'README.md').exists():
                target_path = memory_project / 'agent' / 'memory' / 'README.md'
            if not target_path.exists():
                raise FileNotFoundError(f"Memory contract file '{filename}' not found for agent '{actual_bot_id}'")
            if any(p.is_symlink() or p.is_junction() for p in [target_path, *target_path.parents] if p != target_root and p.is_relative_to(target_root)):
                raise ValueError('Linked profile paths cannot be read')
            if not registry._is_safe_contained(target_path, target_root):
                raise ValueError('Profile is outside this project')
            content = target_path.read_text(encoding='utf-8')
            return {'bot': bot, 'bot_id': actual_bot_id, 'project_id': target_pid,
                    'file': filename, 'path': target_path.relative_to(target_root).as_posix(),
                    'content': content, 'readonly': True, 'version': version(content)}

        path = self.path(bot, filename, project=project)
        if not path.exists():
            raise FileNotFoundError(f"Profile file '{filename}' not found for agent '{actual_bot_id}'")
        content = path.read_text(encoding='utf-8')
        return {'bot': bot, 'bot_id': actual_bot_id, 'project_id': target_pid,
                'file': filename, 'path': path.relative_to(target_root).as_posix(),
                'content': content, 'version': version(content)}

    def save(self, bot, filename, content, expected, project=None):
        if filename not in ('SOUL.md', 'PROFILE.md'):
            raise ValueError('Learned memory is read-only; select an editable profile file')
        if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 12000:
            raise ValueError('Profile content must be nonempty and at most 12000 bytes')
        target_root, target_pid, actual_bot_id, profile_dir, memory_project, helper_bot_id, _, _, _ = self._resolve_agent(bot, project)
        if not os.access(target_root, os.W_OK):
            raise ValueError('Project root is not writable')
        path = self.path(bot, filename, project=project)
        if path.exists() and not os.access(path, os.W_OK):
            raise ValueError('Profile file is not writable')
        if not path.exists() and not os.access(path.parent, os.W_OK):
            raise ValueError('Profile directory is not writable')
        if filename == 'PROFILE.md':
            bot_id_line = f'- Bot ID: `{helper_bot_id}`'
            if bot_id_line not in content.splitlines():
                raise ValueError('Keep the existing Bot ID in PROFILE.md')
        with self.lock:
            before = self.read(bot, filename, project=project)
            if before['version'] != expected:
                raise ValueError('File changed since opening. Reload before saving.')
            if before['content'] == content:
                return before
            temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                raw = path.read_bytes() if path.exists() else b''
                normalized = content.replace('\r\n', '\n')
                if b'\r\n' in raw:
                    normalized = normalized.replace('\n', '\r\n')
                temporary.write_bytes(normalized.encode('utf-8'))
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            return self.read(bot, filename, project=project)


class OfficeSettings:
    lock = threading.Lock()

    def __init__(self, root):
        self.path = Path(root) / 'office-settings.json'

    def read(self):
        import json
        name = json.loads(self.path.read_text(encoding='utf-8'))['name'] if self.path.exists() else 'Codex Office'
        return {'name': name, 'version': version(name)}

    def save(self, name, expected):
        import json
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError('Office name must contain 1 80 characters without control characters')
        with self.lock:
            if self.read()['version'] != expected:
                raise ValueError('Office changed since opening. Reload before saving.')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_text(json.dumps({'name': name.strip()}, ensure_ascii=False), encoding='utf-8')
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return self.read()
