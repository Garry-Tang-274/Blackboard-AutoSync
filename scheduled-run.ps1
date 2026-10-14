$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$env:PYTHONUTF8 = '1'
$logDir = Join-Path $taskRoot '.state\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logPath = Join-Path $logDir ('sync-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
try {
    & (Join-Path $taskRoot '.venv\Scripts\python.exe') (Join-Path $taskRoot 'bb_sync.py') sync *> $logPath
    $resultCode = $LASTEXITCODE
    if ($resultCode -eq 3) {
        Write-Output ('Login expired. Open START.cmd to refresh the session.') | Add-Content -LiteralPath $logPath
    }
    exit $resultCode
} catch {
    Write-Output ('Scheduled runner failed: ' + $_.Exception.GetType().Name) | Add-Content -LiteralPath $logPath
    exit 1
}
