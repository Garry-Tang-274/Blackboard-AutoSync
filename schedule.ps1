[CmdletBinding()]
param(
    [ValidateSet('install','apply','status','disable','enable','remove')][string]$Action = 'status',
    [ValidateRange(1,24)][int]$EveryHours = 6
)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$sha = [System.Security.Cryptography.SHA256]::Create()
try { $rootHash = [System.BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($taskRoot))).Replace('-','').Substring(0,8) }
finally { $sha.Dispose() }
$taskName = 'Blackboard-AutoSync-' + $rootHash
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($Action -eq 'apply') {
    $config = Get-Content -LiteralPath (Join-Path $taskRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $scheduleEnabled = $true
    if ($null -ne $config.schedule_enabled) { $scheduleEnabled = [bool]$config.schedule_enabled }
    if (-not $scheduleEnabled) {
        if ($existing) { Disable-ScheduledTask -TaskName $taskName | Out-Null }
        Write-Host 'Automatic synchronization disabled.'
        exit 0
    }
    if ($null -ne $config.schedule_every_hours) { $EveryHours = [int]$config.schedule_every_hours }
    if ($EveryHours -lt 1 -or $EveryHours -gt 24) { throw 'Schedule interval must be 1-24 hours.' }
    if ($existing) {
        $periodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours $EveryHours)
        $onLogin = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
        Set-ScheduledTask -TaskName $taskName -Trigger @($periodic,$onLogin) | Out-Null
        Enable-ScheduledTask -TaskName $taskName | Out-Null
        Write-Host ('Schedule updated: every ' + $EveryHours + ' hours.')
        exit 0
    }
    $firstReportPath = Join-Path $taskRoot 'reports\latest.json'
    if (-not (Test-Path -LiteralPath $firstReportPath)) { Write-Host 'Schedule saved; it will install after the first connected synchronization.'; exit 0 }
    $firstReport = Get-Content -LiteralPath $firstReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($firstReport.status -notin 'synced','partial' -or $firstReport.courses.Count -lt 1) { Write-Host 'Schedule saved; complete the first connected synchronization to install.'; exit 0 }
    $Action = 'install'
}
switch ($Action) {
    'install' {
        $reportPath = Join-Path $taskRoot 'reports\latest.json'
        if (-not (Test-Path -LiteralPath $reportPath)) { throw 'Run START.cmd and complete the first synchronization before scheduling.' }
        $report = Get-Content -Raw -LiteralPath $reportPath -Encoding UTF8 | ConvertFrom-Json
        if ($report.status -notin 'synced','partial' -or $report.courses.Count -lt 1) {
            throw 'A connected course synchronization is required before scheduling.'
        }
        if ($existing) { Write-Host ('Scheduled task already exists: ' + $taskName); break }
        $hostPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $runnerPath = Join-Path $taskRoot 'scheduled-run.ps1'
        $taskAction = New-ScheduledTaskAction -Execute $hostPath -Argument ('-NoProfile -WindowStyle Hidden -File "' + $runnerPath + '"') -WorkingDirectory $taskRoot
        $periodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours $EveryHours)
        $onLogin = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
        $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger @($periodic,$onLogin) -Principal $principal -Settings $settings -Description 'Read-only Blackboard course material synchronization; every 6 hours by default.' | Out-Null
        Write-Host ('Scheduled task installed: ' + $taskName + ' / every ' + $EveryHours + ' hours and at user logon.')
    }
    'status' {
        if ($existing) { $existing | Select-Object TaskName,State; Get-ScheduledTaskInfo -TaskName $taskName | Select-Object LastRunTime,LastTaskResult,NextRunTime }
        else { Write-Host 'Not scheduled yet; complete the first course synchronization in START.cmd.' }
    }
    'disable' { if ($existing) { Disable-ScheduledTask -TaskName $taskName | Out-Null } }
    'enable' { if ($existing) { Enable-ScheduledTask -TaskName $taskName | Out-Null } }
    'remove' { if ($existing) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false } }
}
