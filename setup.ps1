[CmdletBinding()]
param([switch]$SkipBrowserInstall)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'config.json'))) {
    Copy-Item -LiteralPath (Join-Path $taskRoot 'config.example.json') -Destination (Join-Path $taskRoot 'config.json')
}
$env:PYTHONUTF8 = '1'
$env:PLAYWRIGHT_SKIP_BROWSER_GC = '1'
$pythonPath = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $systemPython = (Get-Command python -ErrorAction Stop).Source
    & $systemPython -m venv (Join-Path $taskRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment.' }
}
& $pythonPath -m pip install --disable-pip-version-check -r (Join-Path $taskRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if (-not $SkipBrowserInstall) {
    & $pythonPath -m playwright install chromium --no-shell
    if ($LASTEXITCODE -ne 0) { throw 'Browser installation failed.' }
}
& $pythonPath (Join-Path $taskRoot 'bb_sync.py') doctor
if ($LASTEXITCODE -ne 0) { throw 'Runtime verification failed.' }
Write-Host 'Setup completed. Open START.cmd to log in and synchronize.'
