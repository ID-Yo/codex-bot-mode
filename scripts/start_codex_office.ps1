param([switch]$NoBrowser, [string]$Project, [string]$Model, [switch]$RelayOnly)
$ErrorActionPreference = 'Stop'
$officeRoot = Split-Path $PSScriptRoot -Parent
$officeUrl = if ($env:CBM_OFFICE_URL) { $env:CBM_OFFICE_URL } else { 'http://localhost:24242' }
# Model: -Model, else .bot-mode/office-model.txt (local, not committed), else the Office default.
if (-not $Model) {
    $modelFile = Join-Path $officeRoot '.bot-mode/office-model.txt'
    $Model = if (Test-Path $modelFile) { (Get-Content $modelFile -TotalCount 1).Trim() } else { 'gpt-6.1-sol' }
}
if ($Project) {
    $Project = (Resolve-Path -LiteralPath $Project).Path
    $officeRuntime = Join-Path $Project '.bot-mode/team-room-v1/office'
} else {
    $officeRuntime = Join-Path $officeRoot '.bot-mode/team-room-v1/demo-20261005-developer'
}

function Test-Office {
    try {
        $state = Invoke-RestMethod "$officeUrl/api/state" -TimeoutSec 2
        return ($null -ne $state.participants -and $null -ne $state.journal -and $null -ne $state.limits)
    } catch { return $false }
}

$officeRelayRoot = Join-Path $officeRoot '.bot-mode/desktop-relay'

function Resolve-CodexPython {
    if (-not $script:officePython) { $script:officePython = (Get-Command python -ErrorAction Stop).Source }
}

function Resolve-Codex {
    Resolve-CodexPython
    if ($script:officeBinary) { return }
    if ($env:CBM_CODEX_BINARY) { $script:officeBinary = $env:CBM_CODEX_BINARY; return }
    $officePackages = Get-ChildItem 'C:/Program Files/WindowsApps' -Directory -Filter 'OpenAI.Codex_*' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending
    foreach ($package in $officePackages) {
        $candidate = Join-Path $package.FullName 'app/resources/codex.exe'
        if ((Test-Path $candidate) -and (Get-Item $candidate).Length -gt 0) {
            $script:officeBinary = $candidate
            break
        }
    }
    if (-not $script:officeBinary) { throw 'Install Codex before starting Codex Office. No usable Codex runtime was found.' }
}

# Registers the Desktop relay, or replaces one that no longer answers. Independent of whether Office already runs.
# Only the lead Manager's own chat (from the roster) can register it; returns $false elsewhere.
function Ensure-Relay {
    Resolve-CodexPython
    $officeManager = & $script:officePython -B -c "from scripts.team_room import project, work; project.configure(); print(work.MANAGER or '')"
    if (-not ($env:CODEX_THREAD_ID -and $env:CODEX_THREAD_ID -eq $officeManager -and $env:CODEX_APP_TOOLS_PIPE_PATH)) { return $false }
    # Windows PowerShell turns native stderr into a terminating error under 'Stop'.
    $ErrorActionPreference = 'Continue'
    & $script:officePython -B -m scripts.team_room.desktop_relay --root $officeRelayRoot --probe *> $null
    $probeExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($probeExit -eq 0) { return $true }
    Resolve-Codex
    New-Item -ItemType Directory -Path $officeRelayRoot -Force | Out-Null
    # A registered relay that is alive but silent is replaced by the relay program itself after a sustained failure.
    $relayArgs = '-B -m scripts.team_room.desktop_relay --codex-path "{0}" --root "{1}"' -f $script:officeBinary,$officeRelayRoot
    $relayProcess = Start-Process -FilePath $script:officePython -ArgumentList $relayArgs -WorkingDirectory $officeRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $officeRelayRoot 'stdout.log') `
        -RedirectStandardError (Join-Path $officeRelayRoot 'stderr.log')
    $null = $relayProcess.Handle  # keeps the handle so ExitCode is readable after a fast exit
    # Ready only when the registrar's relay answers a health probe: a descriptor file can be stale. The registrar
    # may spend about 10 s on rechecks before it replaces a silent relay, and then registers a new one.
    $relayDeadline = (Get-Date).AddSeconds(60)
    while ($true) {
        $ErrorActionPreference = 'Continue'
        & $script:officePython -B -m scripts.team_room.desktop_relay --root $officeRelayRoot --probe *> $null
        $probeExit = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($probeExit -eq 0) { break }
        $relayProcess.Refresh()
        if ($relayProcess.HasExited -and $relayProcess.ExitCode -ne 0) {
            throw ('Desktop relay registration failed (registrar exit code {0}). See .bot-mode/desktop-relay/stderr.log' -f $relayProcess.ExitCode)
        }
        if ($relayProcess.HasExited) { throw 'Desktop relay registrar finished but the relay does not answer. See .bot-mode/desktop-relay/stdout.log' }
        if ((Get-Date) -gt $relayDeadline) { throw 'Desktop relay did not answer within 60 seconds after registration. See .bot-mode/desktop-relay/stderr.log' }
        Start-Sleep -Milliseconds 500
    }
    return $true
}

try {
    $relayReady = Ensure-Relay
    if ($RelayOnly) {
        if (-not $relayReady) { throw 'The Desktop relay can only be registered from the appointed Manager chat (its own executor). Run this command there.' }
        Write-Output "Desktop relay is ready: $officeRelayRoot"
        exit 0
    }
    if (-not (Test-Office)) {
        Resolve-Codex
        New-Item -ItemType Directory -Path $officeRuntime -Force | Out-Null
        $officeArgs = '-B -m scripts.team_room --codex-path "{0}" --model {2} --runtime-root "{1}"' -f $script:officeBinary,$officeRuntime,$Model
        if ($Project) {
            $officeArgs += ' --project "{0}"' -f $Project
        } elseif (Test-Path (Join-Path $officeRelayRoot 'relay.json')) {
            $officeArgs += ' --desktop-relay'
        }
        $officeProcess = Start-Process -FilePath $script:officePython -ArgumentList $officeArgs -WorkingDirectory $officeRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $officeRuntime 'office.stdout.log') `
            -RedirectStandardError (Join-Path $officeRuntime 'office.stderr.log')
        $officeDeadline = (Get-Date).AddSeconds(45)
        while (-not (Test-Office)) {
            $officeProcess.Refresh()
            if ($officeProcess.HasExited) { throw "Office stopped during startup. See $officeRuntime/office.stderr.log" }
            if ((Get-Date) -gt $officeDeadline) { throw "Office did not become ready. See $officeRuntime/office.stderr.log" }
            Start-Sleep -Milliseconds 500
        }
    }
    Write-Output "Codex Office is ready: $officeUrl"
    if (-not $NoBrowser) { Start-Process $officeUrl }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
