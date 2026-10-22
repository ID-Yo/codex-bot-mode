param(
    [string]$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$TargetTriple = 'x86_64-pc-windows-msvc'
)

$ErrorActionPreference = 'Stop'
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$Entry = Join-Path $PSScriptRoot 'office_service.py'
$Output = Join-Path $PSScriptRoot 'src-tauri/binaries'
$ScriptsPath = Join-Path $RepositoryRoot 'scripts'
$TempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { 'D:\Temp' }
$env:PYINSTALLER_CONFIG_DIR = Join-Path $TempRoot 'codex-office-pyinstaller'
New-Item -ItemType Directory -Path $Output -Force | Out-Null

Push-Location $RepositoryRoot
try {
    python -m pip install --disable-pip-version-check --no-warn-script-location `
        -r (Join-Path $RepositoryRoot 'requirements-updates.txt') 'pyinstaller==6.22.3'
    if ($LASTEXITCODE -ne 0) { throw 'Installing the pinned sidecar build dependencies failed.' }

    $Built = Join-Path $Output 'office-service.exe'
    Remove-Item -LiteralPath $Built -Force -ErrorAction SilentlyContinue
    $HiddenImports = @(
        'tomllib',
        'scripts.bot_mode',
        'component_release',
        'profile_update',
        'component_update_service',
        'component_update_status',
        'component_activation',
        'component_activation_host',
        'runtime_update',
        'office_runtime_adapter',
        'scripts.component_activation_host',
        'scripts.runtime_update',
        'scripts.office_runtime_adapter'
    )
    $HostModule = Join-Path $ScriptsPath 'component_update_host.py'
    if (Test-Path $HostModule) {
        $HiddenImports += 'component_update_host'
    } elseif (Test-Path (Join-Path $ScriptsPath 'team_room/component_update_host.py')) {
        $HiddenImports += 'team_room.component_update_host'
    } else {
        Write-Output 'component_update_host is not in this source revision; rebuild after its host integration merges.'
    }
    $ImportArgs = @()
    foreach ($Module in $HiddenImports) { $ImportArgs += @('--hidden-import', $Module) }

    python -m PyInstaller --noconfirm --clean --onefile --name office-service `
        --paths $RepositoryRoot --paths $ScriptsPath `
        --collect-submodules scripts.team_room `
        --collect-submodules team_room `
        --collect-all cryptography `
        @ImportArgs `
        --add-data "$(Join-Path $RepositoryRoot 'scripts/team_room/static');scripts/team_room/static" `
        --distpath $Output --workpath (Join-Path $TempRoot 'codex-office-pyinstaller-work') `
        --specpath (Join-Path $TempRoot 'codex-office-pyinstaller-spec') $Entry
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller did not produce the Office sidecar.' }

    $Sidecar = Join-Path $Output "office-service-$TargetTriple.exe"
    Move-Item -LiteralPath $Built -Destination $Sidecar -Force
    python (Join-Path $PSScriptRoot 'generate_icon.py')
    if ($LASTEXITCODE -ne 0) { throw 'Tauri icon generation failed.' }

    & $Sidecar --help
    if ($LASTEXITCODE -ne 0) { throw 'The packaged Office sidecar failed its --help smoke test.' }
    Write-Output "Built unsigned Office sidecar; --help smoke check passed: $Sidecar"
} finally {
    Pop-Location
}
