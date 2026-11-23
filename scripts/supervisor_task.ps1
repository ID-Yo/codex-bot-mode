<#
Registers, shows or removes the Codex Bot Mode supervisor: a hidden Windows scheduled task that checks the Desktop relay and
the outbox pumps every 2 minutes and restarts what is down, without the Manager chat (#421, epic #420).

  powershell -File scripts/supervisor_task.ps1            # install or update (idempotent)
  powershell -File scripts/supervisor_task.ps1 -Status    # task state, last run, last result, health file
  powershell -File scripts/supervisor_task.ps1 -Uninstall

-RestartOffice also lets the task start Codex Office when it does not answer (off by default: not every install uses Office).
-Interval sets the repeat in minutes (default 2). Runs as the current user, no elevation, no window, also on battery.
#>
param([switch]$Status, [switch]$Uninstall, [switch]$RestartOffice, [int]$Interval = 2, [switch]$NoNotify)
$ErrorActionPreference = 'Stop'
$officeRoot = Split-Path $PSScriptRoot -Parent
$taskName = 'Codex Bot Mode Supervisor'
$legacyName = 'Codex Relay Watchdog'
$script = Join-Path $officeRoot 'scripts/relay_watchdog.py'

function Get-PythonW {
    $python = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if (-not $python) {
        $console = (Get-Command python -ErrorAction Stop).Source
        $candidate = Join-Path (Split-Path $console -Parent) 'pythonw.exe'
        if (Test-Path $candidate) { $python = $candidate } else { $python = $console }
    }
    return $python
}

if ($Status) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) { Write-Output "${taskName} is not installed. Run: powershell -File scripts/supervisor_task.ps1"; exit 1 }
    $info = $task | Get-ScheduledTaskInfo
    $settings = $task.Settings
    [pscustomobject]@{
        State = [string]$task.State; LastRunTime = $info.LastRunTime; LastTaskResult = $info.LastTaskResult; NextRunTime = $info.NextRunTime
        RunsOnBattery = (-not $settings.DisallowStartIfOnBatteries); MultipleInstances = [string]$settings.MultipleInstances
        Hidden = $settings.Hidden; Action = ($task.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments })
    } | Format-List
    $health = Join-Path $officeRoot '.bot-mode/relay-health.json'
    if (Test-Path $health) { Write-Output "Health file: $health"; Get-Content $health -Raw }
    $statusFile = Join-Path $officeRoot '.bot-mode/supervisor-status.json'
    if (Test-Path $statusFile) { Write-Output "Status file: $statusFile"; Get-Content $statusFile -Raw }
    exit 0
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Output "Removed ${taskName}."
    } else { Write-Output "${taskName} was not installed." }
    exit 0
}

if ($Interval -lt 1) { throw 'Interval must be at least 1 minute.' }
if (-not (Test-Path $script)) { throw "Missing $script" }
$python = Get-PythonW
$arguments = '-B "{0}" --office-root "{1}" --all-projects --restart --interval-seconds {2}' -f $script, $officeRoot, ($Interval * 60)
if ($RestartOffice) { $arguments += ' --restart-office' }
if (-not $NoNotify) { $arguments += ' --notify' }
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $officeRoot
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Interval) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($atLogon, $repeat) -Settings $settings -Principal $principal `
    -Description 'Codex Bot Mode: keeps the Desktop relay and the outbox pumps running (restarts them without the Manager chat).' -Force | Out-Null
Write-Output "Installed ${taskName}: every $Interval min and at logon, hidden, also on battery. Command: $python $arguments"

# The hand-made predecessor is removed only when it provably runs this repository's watchdog: a python interpreter as the
# program and exactly this script as the script operand (the first argument that is not an option; -m and -c mean there is no
# script). A path that only appears later, as data, does not count. Anything else is left alone.
function Test-RunsThisWatchdog($task) {
    $scriptFull = [IO.Path]::GetFullPath($script)
    foreach ($taskAction in $task.Actions) {
        if ((Split-Path ([string]$taskAction.Execute).Trim('"') -Leaf) -notmatch '^pythonw?\.exe$') { continue }
        $sawB = $false
        foreach ($token in [regex]::Matches([string]$taskAction.Arguments, '"[^"]*"|\S+')) {
            $value = $token.Value.Trim('"')
            if ($value -ceq '-B' -and -not $sawB) { $sawB = $true; continue }  # the only interpreter option the installed forms use
            if ($value.StartsWith('-')) { break }  # any other option (-m, -mmodule, -c, ...): the script operand is not provable
            if ([string]::Equals([IO.Path]::GetFullPath($value), $scriptFull, [StringComparison]::OrdinalIgnoreCase)) { return $true }
            break  # the first operand is the script; whatever follows is its arguments
        }
    }
    return $false
}
$legacy = Get-ScheduledTask -TaskName $legacyName -TaskPath '\' -ErrorAction SilentlyContinue
if ($legacy -and (Test-RunsThisWatchdog $legacy)) {
    Unregister-ScheduledTask -TaskName $legacyName -Confirm:$false
    Write-Output "Removed the older task ${legacyName} (replaced by ${taskName})."
}
