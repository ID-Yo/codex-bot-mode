# Commits and pushes changes in this private project. Run on a schedule.
# The last failure is written to .backup/last-error.txt; success removes it.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$statusDir = Join-Path $root '.backup'
$errorFile = Join-Path $statusDir 'last-error.txt'
New-Item -ItemType Directory -Force -Path $statusDir | Out-Null
Set-Location -LiteralPath $root
try {
    if (Test-Path -LiteralPath (Join-Path $root 'agent/memory/writer.lock')) { exit 0 }
    git add -A
    if ($LASTEXITCODE -ne 0) { throw 'git add failed' }
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git commit -q -m ('Backup ' + (Get-Date -Format 'yyyy-MM-dd HH:mm'))
        if ($LASTEXITCODE -ne 0) { throw 'git commit failed' }
    }
    git push -q
    if ($LASTEXITCODE -ne 0) { throw 'git push failed' }
    Remove-Item -LiteralPath $errorFile -ErrorAction SilentlyContinue
} catch {
    Set-Content -LiteralPath $errorFile -Encoding utf8 -Value ((Get-Date -Format s) + ' ' + $_)
    exit 1
}
