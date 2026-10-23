"""Run the real Rust lifecycle against a frozen service and synthetic Codex only."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

from smoke_packaged_profiles import make_project


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rust-tests', type=Path, required=True)
    parser.add_argument('--sidecar', type=Path, required=True)
    parser.add_argument('--codex-fixture', type=Path, required=True)
    args = parser.parse_args()
    if args.codex_fixture.name.lower() != 'fixture_codex.exe':
        parser.error('Only the compiled desktop/tests/fixture_codex.c is permitted')
    environment = dict(os.environ, OFFICE_TEST_SIDECAR=str(args.sidecar.resolve(strict=True)),
                       OFFICE_TEST_CODEX=str(args.codex_fixture.resolve(strict=True)))
    for layout in ('legacy', 'packaged'):
        with tempfile.TemporaryDirectory(prefix='office-native-synthetic-') as folder:
            project = Path(folder)
            make_project(project, layout)
            (project / '.synthetic-office-test').touch()
            environment['OFFICE_TEST_PROJECT'] = str(project)
            subprocess.run([str(args.rust_tests.resolve(strict=True)),
                'tests::packaged_service_passes_production_rust_startup_and_shutdown',
                '--exact', '--ignored', '--nocapture'], env=environment, check=True, timeout=180)
            print(f'{layout}: Rust running View, service/launcher identity, stop, restart and recheck PASS', flush=True)


if __name__ == '__main__':
    main()
