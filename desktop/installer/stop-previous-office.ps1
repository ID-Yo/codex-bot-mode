<#
Stops a running Codex Office service gracefully before an upgrade or uninstall replaces its files (#244).

Uses the same authenticated /desktop/shutdown request as the controller's Stop button. It never terminates a process:
if Office is busy, unresponsive or still draining, it exits 2 so the installer can ask the owner to stop Office first.
If an installed service is running but its descriptor cannot be read, it fails closed (exit 2) instead of allowing
the installer to overwrite files while the service is alive (#219).

Exit codes: 0 = nothing to stop (no descriptor and no running installed service, clean stop, or the service is already gone)
                or the service stopped cleanly;
            2 = the service is alive but could not be stopped safely;
            1 = unexpected error. The installer aborts on every result except 0.
#>
param(
    [string]$SettingsPath = (Join-Path $env:LOCALAPPDATA 'org.codexbotmode.office\controller.json'),
    [string]$InstallDir = '',
    [int]$ShutdownTimeoutSec = 75,
    [int]$ExitWaitSec = 30
)
$ErrorActionPreference = 'Stop'
# Anything unexpected is a failure (exit 1): the installer treats every result other than 0 as "not stopped".
trap { Write-Output ('Unexpected error: ' + $_.Exception.Message); exit 1 }

function Done([int]$code, [string]$message) { Write-Output $message; exit $code }

function Normalize-Path([string]$p) {
    if ([string]::IsNullOrWhiteSpace($p)) { return $p }
    $clean = $p.Trim()
    if ($clean.StartsWith('\\?\UNC\', [System.StringComparison]::OrdinalIgnoreCase)) {
        return '\\' + $clean.Substring(8)
    }
    if ($clean.StartsWith('\\?\', [System.StringComparison]::OrdinalIgnoreCase)) {
        return $clean.Substring(4)
    }
    if ($clean.StartsWith('\\.\', [System.StringComparison]::OrdinalIgnoreCase)) {
        return $clean.Substring(4)
    }
    return $clean
}

function Get-InstalledServiceProcesses([string]$dir) {
    $processes = @()
    $running = Get-Process -Name 'office-service*' -ErrorAction SilentlyContinue
    if (-not $running) { return $processes }

    $cleanDir = if ($dir) { (Normalize-Path $dir).TrimEnd('\', '/') } else {
        $regLocation = $null
        try {
            $reg = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Codex Office' -ErrorAction SilentlyContinue
            if ($reg -and $reg.InstallLocation) {
                $regLocation = (Normalize-Path $reg.InstallLocation).Trim('"', "'").TrimEnd('\', '/')
            }
        } catch {}
        if ($regLocation) { $regLocation } else { (Normalize-Path (Join-Path $env:LOCALAPPDATA 'Codex Office')).TrimEnd('\', '/') }
    }
    foreach ($p in $running) {
        $exe = $null
        try {
            $exe = $p.Path
        } catch {}
        if (-not $exe) {
            try {
                $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)" -ErrorAction SilentlyContinue
                $exe = $cim.ExecutablePath
            } catch {}
        }
        if ($exe) {
            $normExe = Normalize-Path $exe
            if ($cleanDir) {
                if ($normExe.StartsWith($cleanDir + '\', [System.StringComparison]::OrdinalIgnoreCase) -or
                    [string]::Equals($normExe, $cleanDir, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $processes += $p
                }
            } else {
                $processes += $p
            }
        } else {
            $cmd = $null
            try {
                $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)" -ErrorAction SilentlyContinue
                $cmd = $cim.CommandLine
            } catch {}
            if ($cmd -and $cleanDir) {
                $normCmd = Normalize-Path $cmd
                if ($normCmd.IndexOf($cleanDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
                    $processes += $p
                }
            } else {
                $processes += $p
            }
        }
    }
    return $processes
}

function Test-PortOpen([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect('127.0.0.1', $port, $null, $null)
        # Windows needs about 2 seconds to report a refused loopback connection; a timeout does not prove the port is free.
        if (-not $pending.AsyncWaitHandle.WaitOne(4000)) { return $true }
        try { $client.EndConnect($pending); return $true } catch { return $false }
    } finally { $client.Close() }
}

function Wait-ProcessGone([int]$processId, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return -not (Get-Process -Id $processId -ErrorAction SilentlyContinue)
}

function Invoke-Controller([string]$endpoint, [string]$method, $descriptor, [int]$timeoutSec) {
    $request = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:' + $descriptor.port + $endpoint)
    $request.Method = $method
    $request.Timeout = $timeoutSec * 1000
    $request.ReadWriteTimeout = $timeoutSec * 1000
    $request.Headers.Add('X-Office-Controller-Token', [string]$descriptor.token)
    $request.Headers.Add('Origin', 'http://tauri.localhost')
    if ($method -eq 'POST') { $request.ContentLength = 0 }
    try { $response = $request.GetResponse() } catch [System.Net.WebException] {
        if ($null -eq $_.Exception.Response) { throw }
        $response = $_.Exception.Response
    }
    try {
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        $body = $reader.ReadToEnd()
        return @{ Status = [int]$response.StatusCode; Body = $body }
    } finally { $response.Close() }
}

$normSettingsPath = Normalize-Path $SettingsPath
$descriptor = $null
$servicePid = 0
$port = 0

if (-not (Test-Path -LiteralPath $normSettingsPath)) {
    $installed = Get-InstalledServiceProcesses $InstallDir
    if ($installed.Count -gt 0) {
        Done 2 'Codex Office is running, but controller settings could not be found. Close Codex Office before continuing.'
    }
    Done 0 'No controller settings; nothing to stop.'
}

try {
    $settings = Get-Content -LiteralPath $normSettingsPath -Raw | ConvertFrom-Json
    $normProject = Normalize-Path $settings.projectPath
    if ([string]::IsNullOrWhiteSpace($normProject)) { throw 'missing projectPath' }
    $descriptorPath = Join-Path $normProject '.bot-mode\team-room-v1\desktop-controller\desktop.json'
    $normDescriptorPath = Normalize-Path $descriptorPath
    if (-not (Test-Path -LiteralPath $normDescriptorPath)) { throw 'missing descriptor' }
    $descriptor = Get-Content -LiteralPath $normDescriptorPath -Raw | ConvertFrom-Json
    $servicePid = [int]$descriptor.pid
    $port = [int]$descriptor.port
    if ($servicePid -le 0 -or $port -le 0 -or $port -gt 65535 -or [string]::IsNullOrEmpty([string]$descriptor.token)) { throw 'incomplete descriptor' }
} catch {
    $installed = Get-InstalledServiceProcesses $InstallDir
    if ($installed.Count -gt 0) {
        Done 2 'Codex Office is running from the installation folder, but its service descriptor could not be read. Stop Codex Office before continuing.'
    }
    Done 0 'No readable service descriptor; nothing to stop.'
}

if ($descriptor.state -eq 'stopped') {
    $proc = Get-Process -Id $servicePid -ErrorAction SilentlyContinue
    $isSvc = $null -ne $proc -and $proc.ProcessName -like 'office-service*'
    if (-not $isSvc -and -not (Test-PortOpen $port)) {
        Done 0 'Office recorded a clean stop.'
    }
}

$process = Get-Process -Id $servicePid -ErrorAction SilentlyContinue
$isService = $null -ne $process -and $process.ProcessName -like 'office-service*'
if (-not $isService -and -not (Test-PortOpen $port)) {
    $installed = Get-InstalledServiceProcesses $InstallDir
    if ($installed.Count -gt 0) {
        Done 2 'Codex Office is running from the installation folder, but its active process does not match the descriptor. Close Codex Office before continuing.'
    }
    Done 0 'The recorded service is gone and its port is free; the controller will archive the stale descriptor.'
}

try {
    $result = Invoke-Controller '/desktop/shutdown' 'POST' $descriptor $ShutdownTimeoutSec
} catch {
    Done 2 'Office is running but did not answer the shutdown request. Stop it from Codex Office, then retry.'
}
if ($result.Status -eq 200) {
    if (Wait-ProcessGone $servicePid $ExitWaitSec) { Done 0 'Office stopped safely.' }
    Done 2 'Office confirmed shutdown but its process is still running. Close Codex Office, then retry.'
}
if ($result.Status -eq 202) {
    if (Wait-ProcessGone $servicePid $ShutdownTimeoutSec) { Done 0 'Office finished its work and stopped.' }
    Done 2 'Office is still finishing work. Wait for it or stop it from Codex Office, then retry.'
}
$reason = ''
try { $reason = [string](($result.Body | ConvertFrom-Json).reason) } catch {}
Done 2 ('Office is busy and was not stopped. ' + $reason).Trim()

