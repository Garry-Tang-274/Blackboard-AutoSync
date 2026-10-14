[CmdletBinding()]
param([ValidateSet('start','login','sync','discover','status','doctor')][string]$Mode = 'start')
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$pythonPath = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    & (Join-Path $taskRoot 'setup.ps1')
}
$env:PYTHONUTF8 = '1'
$scriptPath = Join-Path $taskRoot 'bb_sync.py'
if ($Mode -eq 'start') {
    $currentConfig = Get-Content -LiteralPath (Join-Path $taskRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($currentConfig.course_selection_mode -eq 'selected' -and $currentConfig.selected_course_ids.Count -eq 0) {
        & $pythonPath $scriptPath sync
        $resultCode = $LASTEXITCODE
    } elseif (Test-Path -LiteralPath (Join-Path $taskRoot '.state\login-verified.json')) {
        & $pythonPath $scriptPath sync
        $resultCode = $LASTEXITCODE
        if ($resultCode -eq 3) {
            & $pythonPath $scriptPath login --sync-after-login
            $resultCode = $LASTEXITCODE
        }
    } else {
        & $pythonPath $scriptPath login --sync-after-login
        $resultCode = $LASTEXITCODE
    }
    if ($resultCode -in 0,2) {
        $latestReport = Get-Content -LiteralPath (Join-Path $taskRoot 'reports\latest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($latestReport.status -in 'synced','partial') {
            & (Join-Path $taskRoot 'schedule.ps1') -Action apply
        }
    }
    $reportPath = Join-Path $taskRoot 'reports\latest.html'
    if (Test-Path -LiteralPath $reportPath) { Invoke-Item -LiteralPath $reportPath }
} elseif ($Mode -eq 'login') {
    & $pythonPath $scriptPath login --sync-after-login
    $resultCode = $LASTEXITCODE
} elseif ($Mode -eq 'sync') {
    & $pythonPath $scriptPath sync
    $resultCode = $LASTEXITCODE
    if ($resultCode -eq 3) {
        & $pythonPath $scriptPath login --sync-after-login
        $resultCode = $LASTEXITCODE
    }
    if ($resultCode -in 0,2) {
        & (Join-Path $taskRoot 'schedule.ps1') -Action apply
    }
    $reportPath = Join-Path $taskRoot 'reports\latest.html'
    if (Test-Path -LiteralPath $reportPath) { Invoke-Item -LiteralPath $reportPath }
} else {
    & $pythonPath $scriptPath $Mode
    $resultCode = $LASTEXITCODE
}
exit $resultCode
