"""Exercise a frozen service with real profile helpers and a synthetic Codex fixture.

Never pass a real Codex executable: this check creates isolated discussion threads.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]


def request(descriptor, method, path):
    port = descriptor['port']
    origin = 'http://tauri.localhost' if path.startswith('/desktop/') else f'http://localhost:{port}'
    headers = {'Host': f'localhost:{port}', 'Origin': origin,
               'X-Office-Controller-Token': descriptor['token']}
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, (path, response.status)
        return body
    finally:
        connection.close()


def check(sidecar, fixture, layout):
    with tempfile.TemporaryDirectory(prefix='office-profile-smoke-') as folder:
        project = Path(folder)
        metadata = project / '.bot-mode'
        metadata.mkdir()
        if layout == 'legacy':
            workspace, bot_id = 'agents/project-manager', 'project-manager'
            profile = project / workspace
            helper = project / 'scripts/bot_mode.py'
            helper_source = ROOT / 'scripts/bot_mode.py'
        else:
            workspace, bot_id = 'team/Development/developer', 'developer'
            profile = project / workspace / 'agent'
            helper = project / workspace / 'scripts/bot_mode.py'
            helper_source = ROOT / 'packages/project-manager-v1/scripts/bot_mode.py'
        (profile / 'memory').mkdir(parents=True)
        for name, content in {'BASE.md': '# Base', 'SOUL.md': '# Soul',
                              'PROFILE.md': f'# Profile\n- Bot ID: `{bot_id}`',
                              'memory/README.md': '# Memory contract'}.items():
            (profile / name).write_text(content, encoding='utf-8')
        helper.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(helper_source, helper)
        roster = {'schema': 1, 'project': {'name': 'profile-smoke'},
                  'teams': [{'name': 'Development'}],
                  'participants': [{'bot': 'developer', 'bot_id': bot_id,
                                    'role': 'Developer', 'team': 'Development',
                                    'workspace': workspace, 'status': 'active'}]}
        (metadata / 'roster.json').write_text(json.dumps(roster), encoding='utf-8')
        runtime = metadata / 'team-room-v1/smoke'
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        # stdout contains a local descriptor; keep it out of test output.
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([str(sidecar), '--codex-path', str(fixture),
                '--project', str(project), '--runtime-root', str(runtime),
                '--port', '0', '--desktop-managed'], stdout=subprocess.DEVNULL,
                stderr=errors, creationflags=flags)
            descriptor = None
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        errors.seek(0)
                        from scripts.team_room.transport import safe_error
                        raise AssertionError('Service exited: ' + safe_error(errors.read().decode(errors='replace')[-400:]))
                    try:
                        candidate = json.loads((runtime / 'desktop.json').read_text(encoding='utf-8'))
                        if candidate.get('state') == 'running':
                            descriptor = candidate
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(.1)
                assert descriptor is not None, 'No running descriptor within 30 seconds'
                health = json.loads(request(descriptor, 'GET', '/desktop/health'))
                assert health['state'] == 'running' and 'token' not in health
                assets = request(descriptor, 'GET', '/projects.js').decode()
                assert 'office_visible_projects' in assets
                status = json.loads(request(descriptor, 'GET', '/desktop/status'))
                assert status['safety'] == 'safe'
                stopped = json.loads(request(descriptor, 'POST', '/desktop/shutdown'))
                assert stopped['state'] == 'stopped'
                process.wait(timeout=20)
                assert process.returncode == 0
                saved = json.loads((runtime / 'desktop.json').read_text(encoding='utf-8'))
                assert saved['state'] == 'stopped'
                print(f'{layout}: real helper, health, Projects asset and clean stop PASS')
            finally:
                if process.poll() is None and descriptor:
                    try:
                        request(descriptor, 'POST', '/desktop/shutdown')
                        process.wait(timeout=20)
                    except (OSError, AssertionError, subprocess.TimeoutExpired):
                        pass
                if process.poll() is None:
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                       capture_output=True, timeout=10, check=True)
                    else:
                        process.terminate()
                    process.wait(timeout=10)


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sidecar', type=Path, required=True)
    parser.add_argument('--codex-fixture', type=Path, required=True)
    args = parser.parse_args()
    if args.codex_fixture.name.lower() != 'fixture_codex.exe':
        parser.error('Only the synthetic fixture_codex.exe is accepted')
    for layout in ('legacy', 'packaged'):
        check(args.sidecar.resolve(strict=True), args.codex_fixture.resolve(strict=True), layout)
