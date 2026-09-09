param(
    [string]$TargetDate = '',
    [ValidateSet('audio', 'danmaku', 'both')][string]$Only = 'audio',
    [switch]$DryRun,
    [switch]$PrintPlan,
    [string]$PythonExe = ''
)
$ErrorActionPreference = 'Stop'
if (-not $TargetDate) {
    $zone = [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
    $chinaNow = [TimeZoneInfo]::ConvertTimeFromUtc([datetime]::UtcNow, $zone)
    $TargetDate = $chinaNow.AddDays(-1).ToString('yyyy-MM-dd')
}
$day = [datetime]::ParseExact($TargetDate, 'yyyy-MM-dd', [cultureinfo]::InvariantCulture)
$TargetDate = $day.ToString('yyyy-MM-dd')
if (-not $PythonExe) { $PythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
$logs = Join-Path $PSScriptRoot 'logs'
$stamp = (Get-Date).ToString('yyyyMMdd-HHmmss-fff') + "-$PID"
$prefix = Join-Path $logs "daily-$TargetDate-$stamp"
$arguments = @('-m', 'qiniu_get', 'run', '--date', $TargetDate, '--only', $Only)
if ($DryRun) { $arguments += '--dry-run' }
$plan = [ordered]@{
    target_date = $TargetDate
    working_directory = $PSScriptRoot
    python = $PythonExe
    arguments = $arguments
    stdout_log = "$prefix.stdout.log"
    stderr_log = "$prefix.stderr.log"
}
if ($PrintPlan) {
    $plan | ConvertTo-Json -Depth 4
    exit 0
}
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$exitCode = 1
$started = [datetime]::UtcNow.ToString('o')
$previousUtf8 = $env:PYTHONUTF8
$env:PYTHONUTF8 = '1'
try {
    if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Python not found: $PythonExe" }
    $process = Start-Process -FilePath $PythonExe -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -Wait -PassThru -NoNewWindow -RedirectStandardOutput $plan.stdout_log -RedirectStandardError $plan.stderr_log
    $exitCode = $process.ExitCode
} catch {
    $_.Exception.Message | Out-File -FilePath $plan.stderr_log -Encoding utf8 -Append
} finally {
    $env:PYTHONUTF8 = $previousUtf8
    $result = [ordered]@{
        target_date = $TargetDate
        started_at_utc = $started
        finished_at_utc = [datetime]::UtcNow.ToString('o')
        exit_code = $exitCode
        stdout_log = $plan.stdout_log
        stderr_log = $plan.stderr_log
    }
    $result | ConvertTo-Json | Out-File -FilePath "$prefix.result.json" -Encoding utf8
}
Write-Host "Target date: $TargetDate; exit code: $exitCode; logs: $logs"
exit $exitCode
