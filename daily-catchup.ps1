$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$env:PYTHONUTF8 = '1'
# Download and prepare the local queue; dispatch is handled separately.
& (Join-Path $taskRoot '.venv\Scripts\python.exe') (Join-Path $taskRoot 'daily_sync.py')
$resultCode = $LASTEXITCODE
exit $resultCode
