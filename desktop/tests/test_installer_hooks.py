"""NSIS hook checks (#244): the hook file compiles from a separate generated-installer directory and the installer
aborts on every result of the shutdown helper except success. Needs makensis (the copy Tauri downloads is used)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

DESKTOP = Path(__file__).resolve().parents[1]
HOOKS = DESKTOP / 'src-tauri' / 'windows' / 'hooks.nsh'
CANDIDATES = [os.environ.get('OFFICE_MAKENSIS', ''), str(Path(os.environ.get('LOCALAPPDATA', '')) / 'tauri' / 'NSIS' / 'makensis.exe'),
              shutil.which('makensis') or '']
MAKENSIS = next((c for c in CANDIDATES if c and Path(c).is_file()), None)


def nsis_path(path):
    return str(path).replace('/', '\\')


@unittest.skipUnless(MAKENSIS, 'makensis is not available')
class InstallerHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def compile(self, name, body):
        script = self.dir / (name + '.nsi')
        script.write_text(body, encoding='utf-8')
        result = subprocess.run([MAKENSIS, '/V2', str(script)], cwd=self.dir, capture_output=True, text=True, timeout=120)
        return result

    def test_full_hook_compiles_from_a_separate_installer_directory(self):
        generated = self.dir / 'generated' / 'x64'
        generated.mkdir(parents=True)
        body = f'''Unicode true
OutFile "{nsis_path(generated / 'hooks-full.exe')}"
InstallDir "$TEMP\\office-hook-compile"
RequestExecutionLevel user
!include "{nsis_path(HOOKS)}"
Section Install
  !insertmacro NSIS_HOOK_PREINSTALL
  !insertmacro NSIS_HOOK_POSTINSTALL
  WriteUninstaller "$INSTDIR\\uninstall.exe"
SectionEnd
Section Uninstall
  !insertmacro NSIS_HOOK_PREUNINSTALL
  !insertmacro NSIS_HOOK_POSTUNINSTALL
SectionEnd
'''
        result = self.compile('full', body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((generated / 'hooks-full.exe').is_file())

    def run_helper(self, ps_body):
        script = self.dir / 'helper.ps1'
        script.write_text(ps_body, encoding='utf-8')
        marker = self.dir / 'after-hook.txt'
        marker.unlink(missing_ok=True)
        body = f'''Unicode true
OutFile "{nsis_path(self.dir / 'harness.exe')}"
RequestExecutionLevel user
SilentInstall silent
!include "{nsis_path(HOOKS)}"
Section Install
  !insertmacro CodexOfficeStopService "{nsis_path(script)}" harness
  FileOpen $2 "{nsis_path(marker)}" w
  FileWrite $2 "continued"
  FileClose $2
SectionEnd
'''
        built = self.compile('harness', body)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        run = subprocess.run([str(self.dir / 'harness.exe'), '/S'], capture_output=True, timeout=120)
        return run.returncode, marker.exists()

    def test_installer_continues_only_when_the_helper_reports_success(self):
        self.assertEqual(self.run_helper('exit 0'), (0, True))
        for code in (1, 2, 3, 255):
            returncode, continued = self.run_helper(f'exit {code}')
            self.assertFalse(continued, f'exit {code} must stop the installer')
            self.assertNotEqual(returncode, 0, f'exit {code}')

    def test_helper_that_cannot_run_stops_the_installer(self):
        returncode, continued = self.run_helper('throw "boom"')
        self.assertFalse(continued)
        self.assertNotEqual(returncode, 0)


if __name__ == '__main__':
    unittest.main()