[CmdletBinding()]
param([switch]$VerifyOnly)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$name = 'Blackboard-Daily-2000-CatchUp'
if (-not $VerifyOnly) {
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -Argument ('-NoProfile -WindowStyle Hidden -File "' + (Join-Path $taskRoot 'daily-catchup.ps1') + '"') -WorkingDirectory $taskRoot
    $firstRun = (Get-Date).Date.AddHours(20)
    if ($firstRun -le (Get-Date)) { $firstRun = $firstRun.AddDays(1) }
    $daily = New-ScheduledTaskTrigger -Daily -At $firstRun
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $name -Action $action -Trigger @($daily,$logon) -Principal $principal -Settings $settings -Description 'Daily 20:00 Blackboard sync; catch up at Windows logon if missed; deduplicate by local day.' -Force | Out-Null
    $legacy = Get-ScheduledTask -TaskName 'Blackboard-Daily-0800-CatchUp' -ErrorAction SilentlyContinue
    if ($legacy) { Disable-ScheduledTask -TaskName $legacy.TaskName | Out-Null }
}
Get-ScheduledTask -TaskName $name | Select-Object TaskName,State,@{Name='StartWhenAvailable';Expression={$_.Settings.StartWhenAvailable}},@{Name='Triggers';Expression={$_.Triggers.CimClass.CimClassName -join ','}}
Get-ScheduledTaskInfo -TaskName $name | Select-Object LastRunTime,LastTaskResult,NextRunTime
