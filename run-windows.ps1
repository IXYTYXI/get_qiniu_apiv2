param(
    [Parameter(Mandatory = $true)][string]$StartDate,
    [string]$EndDate = $StartDate,
    [ValidateSet('audio', 'danmaku', 'both')][string]$Only = 'audio',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$start = [datetime]::ParseExact($StartDate, 'yyyy-MM-dd', [cultureinfo]::InvariantCulture)
$end = [datetime]::ParseExact($EndDate, 'yyyy-MM-dd', [cultureinfo]::InvariantCulture)
if ($end -lt $start) { throw 'EndDate must not precede StartDate.' }
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw 'Create the environment first: py -3.11 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e .'
}
$previousUtf8 = $env:PYTHONUTF8
$env:PYTHONUTF8 = '1'
Push-Location $PSScriptRoot
$failed = @()
try {
    for ($day = $start; $day -le $end; $day = $day.AddDays(1)) {
        $dateText = $day.ToString('yyyy-MM-dd')
        Write-Host "Processing $dateText ($Only)"
        $arguments = @('-m', 'qiniu_get', 'run', '--date', $dateText, '--only', $Only)
        if ($DryRun) { $arguments += '--dry-run' }
        & $python @arguments
        if ($LASTEXITCODE -ne 0) { $failed += $dateText }
    }
} finally {
    Pop-Location
    $env:PYTHONUTF8 = $previousUtf8
}
if ($failed.Count -gt 0) {
    Write-Host "Failed dates (rerun after checking errors): $($failed -join ', ')"
    exit 1
}
exit 0
