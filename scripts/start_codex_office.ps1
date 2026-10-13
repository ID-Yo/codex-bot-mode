param([switch]$NoBrowser, [string]$Project, [string]$Model)
$ErrorActionPreference = 'Stop'
$officeRoot = Split-Path $PSScriptRoot -Parent
$officeUrl = 'http://localhost:24242'
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

try {
    if (-not (Test-Office)) {
        $officePython = (Get-Command python -ErrorAction Stop).Source
        $officeBinary = $null
        $officePackages = Get-ChildItem 'C:/Program Files/WindowsApps' -Directory -Filter 'OpenAI.Codex_*' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending
        foreach ($package in $officePackages) {
            $candidate = Join-Path $package.FullName 'app/resources/codex.exe'
            if ((Test-Path $candidate) -and (Get-Item $candidate).Length -gt 0) {
                $officeBinary = $candidate
                break
            }
        }
        if (-not $officeBinary) { throw 'Install Codex before starting Codex Office. No usable Codex runtime was found.' }
        New-Item -ItemType Directory -Path $officeRuntime -Force | Out-Null
        $officeRelayRoot = Join-Path $officeRoot '.bot-mode/desktop-relay'
        # Only the lead Manager's own chat (from the roster) can register the Desktop relay.
        $officeManager = & $officePython -B -c "from scripts.team_room import project, work; project.configure(); print(work.MANAGER or '')"
        if ($env:CODEX_THREAD_ID -and $env:CODEX_THREAD_ID -eq $officeManager -and $env:CODEX_APP_TOOLS_PIPE_PATH) {
            # Windows PowerShell turns native stderr into a terminating error under 'Stop'.
            $ErrorActionPreference = 'Continue'
            & $officePython -B -m scripts.team_room.desktop_relay --root $officeRelayRoot --probe *> $null
            $probeExit = $LASTEXITCODE
            $ErrorActionPreference = 'Stop'
            if ($probeExit -ne 0) {
                New-Item -ItemType Directory -Path $officeRelayRoot -Force | Out-Null
                $relayArgs = '-B -m scripts.team_room.desktop_relay --codex-path "{0}" --root "{1}"' -f $officeBinary,$officeRelayRoot
                $relayProcess = Start-Process -FilePath $officePython -ArgumentList $relayArgs -WorkingDirectory $officeRoot -WindowStyle Hidden -PassThru `
                    -RedirectStandardOutput (Join-Path $officeRelayRoot 'stdout.log') `
                    -RedirectStandardError (Join-Path $officeRelayRoot 'stderr.log')
                $relayDeadline = (Get-Date).AddSeconds(20)
                while (-not (Test-Path (Join-Path $officeRelayRoot 'relay.json'))) {
                    $relayProcess.Refresh()
                    if ($relayProcess.HasExited -or (Get-Date) -gt $relayDeadline) { throw 'Desktop relay registration failed. See .bot-mode/desktop-relay/stderr.log' }
                    Start-Sleep -Milliseconds 300
                }
            }
        }
        $officeArgs = '-B -m scripts.team_room --codex-path "{0}" --model {2} --runtime-root "{1}"' -f $officeBinary,$officeRuntime,$Model
        if ($Project) {
            $officeArgs += ' --project "{0}"' -f $Project
        } elseif (Test-Path (Join-Path $officeRelayRoot 'relay.json')) {
            $officeArgs += ' --desktop-relay'
        }
        $officeProcess = Start-Process -FilePath $officePython -ArgumentList $officeArgs -WorkingDirectory $officeRoot -WindowStyle Hidden -PassThru `
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
    Write-Host "Codex Office is ready: $officeUrl"
    if (-not $NoBrowser) { Start-Process $officeUrl }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
