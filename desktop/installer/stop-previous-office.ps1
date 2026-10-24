<#
Stops a running Codex Office service gracefully before an upgrade or uninstall replaces its files (#244).

Uses the same authenticated /desktop/shutdown request as the controller's Stop button. It never terminates a process:
if Office is busy, unresponsive or still draining, it exits 2 so the installer can ask the owner to stop Office first.

Exit codes: 0 = nothing to stop (no descriptor, clean stop, or the service is already gone) or the service stopped;
            2 = the service is alive but could not be stopped safely;
            1 = unexpected error. The installer aborts on every result except 0.
#>
param(
    [string]$SettingsPath = (Join-Path $env:LOCALAPPDATA 'org.codexbotmode.office\controller.json'),
    [int]$ShutdownTimeoutSec = 75,
    [int]$ExitWaitSec = 30
)
$ErrorActionPreference = 'Stop'
# Anything unexpected is a failure (exit 1): the installer treats every result other than 0 as "not stopped".
trap { Write-Output ('Unexpected error: ' + $_.Exception.Message); exit 1 }

function Done([int]$code, [string]$message) { Write-Output $message; exit $code }

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

if (-not (Test-Path -LiteralPath $SettingsPath)) { Done 0 'No controller settings; nothing to stop.' }
try {
    $settings = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
    $descriptorPath = Join-Path $settings.projectPath '.bot-mode\team-room-v1\desktop-controller\desktop.json'
    $descriptor = Get-Content -LiteralPath $descriptorPath -Raw | ConvertFrom-Json
    $servicePid = [int]$descriptor.pid
    $port = [int]$descriptor.port
    if ($servicePid -le 0 -or $port -le 0 -or $port -gt 65535 -or [string]::IsNullOrEmpty([string]$descriptor.token)) { throw 'incomplete' }
} catch { Done 0 'No readable service descriptor; nothing to stop.' }

if ($descriptor.state -eq 'stopped') { Done 0 'Office recorded a clean stop.' }
$process = Get-Process -Id $servicePid -ErrorAction SilentlyContinue
$isService = $null -ne $process -and $process.ProcessName -like 'office-service*'
if (-not $isService -and -not (Test-PortOpen $port)) { Done 0 'The recorded service is gone and its port is free; the controller will archive the stale descriptor.' }

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
