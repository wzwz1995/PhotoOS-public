# run-pipeline.ps1 - headless wrapper used by the GUI
#
# Runs photo-pipeline.ps1 and mirrors every line (including Write-Host output)
# into a UTF-8 log file that the GUI tails live. Exit code 0 = finished,
# 1 = the pipeline threw.
#
# NOTE: keep this file UTF-8 with BOM (PowerShell 5.1 reads BOM-less files as GBK).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [string]$LogPath = '',
    [string]$PhotoRoot = 'C:\Photos',
    [string]$VideoRoot = 'C:\Videos',
    [string]$WorkRoot = 'C:\Photos\_归类清单',
    [double]$GapHours = 12,
    [int]$MaxFiles = 0,
    [string]$CameraStagingRoot = 'C:\CameraImport',
    [int]$CameraMaxFiles = 0,
    [switch]$Execute,
    [switch]$SkipDedupe,
    [switch]$AllowCrossFolderDedupe
)

$tools = $PSScriptRoot
if (-not $LogPath) {
    $LogPath = Join-Path $WorkRoot ("gui-run-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
$logDir = Split-Path $LogPath -Parent
if ($logDir -and -not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

function Write-Line($text) {
    try { Add-Content -LiteralPath $LogPath -Value $text -Encoding UTF8 } catch { }
}

Write-Line ("=== 开始 {0} ===" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Write-Line ("来源: {0}" -f $Source)
Write-Line ("模式: {0}" -f $(if ($Execute) { 'EXECUTE（真实移动）' } else { 'DRY RUN（只看计划）' }))
Write-Line ''

$argList = @{
    Source    = $Source
    PhotoRoot = $PhotoRoot
    VideoRoot = $VideoRoot
    WorkRoot  = $WorkRoot
    GapHours  = $GapHours
    CameraStagingRoot = $CameraStagingRoot
}
if ($CameraMaxFiles -gt 0) { $argList['CameraMaxFiles'] = $CameraMaxFiles }
if ($Execute) { $argList['Execute'] = $true }
if ($SkipDedupe) { $argList['SkipDedupe'] = $true }
if ($AllowCrossFolderDedupe) { $argList['AllowCrossFolderDedupe'] = $true }
if ($MaxFiles -gt 0) { $argList['MaxFiles'] = $MaxFiles }

$code = 0
try {
    & (Join-Path $tools 'photo-pipeline.ps1') @argList *>&1 | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { 'ERROR: ' + $_.ToString() } else { [string]$_ }
        Write-Line $line
    }
}
catch {
    Write-Line ('ERROR: ' + $_.Exception.Message)
    $code = 1
}

Write-Line ''
Write-Line ("=== 结束 {0} ===" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
if ($code -eq 0) { Write-Line 'PIPELINE-STATUS: OK' } else { Write-Line 'PIPELINE-STATUS: FAILED' }
exit $code
