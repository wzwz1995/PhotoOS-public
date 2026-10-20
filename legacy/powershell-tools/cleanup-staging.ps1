# cleanup-staging.ps1 - remove camera staging copies that are already in the library
#
# Why: a dry run copies the files off the camera into C:\Staging\<timestamp>\ but
# does not move them anywhere, so that copy stays behind (tens of GB). After a
# successful import the same content is in the library, making the copy redundant.
#
# Safety rule: a staging folder is only deleted when EVERY media file inside it can
# be matched to a file in the library by name + exact byte size (and, with
# -DeepVerify, by identical SHA256 as well). Anything that cannot be matched keeps
# its whole folder and is reported instead of deleted.
#
# The newest N folders are always kept (a run may still be in progress).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File cleanup-staging.ps1              # scan only
#   powershell -ExecutionPolicy Bypass -File cleanup-staging.ps1 -Execute     # delete
#   powershell -ExecutionPolicy Bypass -File cleanup-staging.ps1 -Execute -DeepVerify
#
# Paths come from photo-tool.config.json next to this script; parameters override.
#
# NOTE: keep this file UTF-8 with BOM. Run check-tools.ps1 after editing.

[CmdletBinding()]
param(
    [string]$StagingRoot = '',
    [string]$WorkRoot = '',
    [string]$PhotoRoot = '',
    [string]$VideoRoot = '',
    [string]$ConfigPath = '',
    [string]$LibraryIndex = '',
    [int]$KeepLatest = 1,
    [switch]$DeepVerify,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
if (-not $ConfigPath) { $ConfigPath = Join-Path $PSScriptRoot 'photo-tool.config.json' }

# ---- config (same file the GUI uses) ---------------------------------------
$cfg = [ordered]@{
    PhotoRoot         = 'C:\Photos'
    VideoRoot         = 'C:\Videos'
    WorkRoot          = 'C:\Photos\_归类清单'
    CameraStagingRoot = 'C:\Staging'
}
if (Test-Path -LiteralPath $ConfigPath) {
    try {
        $j = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($k in @($cfg.Keys)) { if ($j.$k) { $cfg[$k] = [string]$j.$k } }
    }
    catch { }
}
if (-not $StagingRoot) { $StagingRoot = $cfg.CameraStagingRoot }
if (-not $PhotoRoot) { $PhotoRoot = $cfg.PhotoRoot }
if (-not $VideoRoot) { $VideoRoot = $cfg.VideoRoot }
if (-not $WorkRoot) { $WorkRoot = $cfg.WorkRoot }
if (-not $LibraryIndex) { $LibraryIndex = Join-Path $WorkRoot 'library-index.csv' }

$mediaExt = @('.jpg', '.jpeg', '.nef', '.png', '.mov', '.mp4', '.heic', '.tif', '.tiff', '.arw', '.cr2', '.dng')

Write-Host ("暂存根目录：{0}" -f $StagingRoot)
if (-not (Test-Path -LiteralPath $StagingRoot)) {
    Write-Host '暂存目录不存在，没有需要清理的东西。'
    exit 0
}

# ---- library lookup: "<name>|<size>" -> full path ---------------------------
$byNameSize = @{}
$byName = @{}
if (Test-Path -LiteralPath $LibraryIndex) {
    foreach ($r in (Import-Csv -LiteralPath $LibraryIndex)) {
        if (-not $r.Path -or -not $r.Name) { continue }
        $byName[$r.Name] = $r.Path
        $byNameSize["$($r.Name)|$($r.Size)"] = $r.Path
    }
    Write-Host ("库索引：{0} 条（{1}）" -f $byName.Count, $LibraryIndex)
}
else {
    Write-Host '警告：没有库索引，无法核对内容，将不做任何删除。'
    Write-Host ("（期望位置：{0}）" -f $LibraryIndex)
    exit 1
}

$folders = @(Get-ChildItem -LiteralPath $StagingRoot -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)
if ($folders.Count -eq 0) { Write-Host '暂存目录里没有子目录。'; exit 0 }
$keep = @($folders | Select-Object -First ([Math]::Max(0, $KeepLatest)))
$candidates = @($folders | Select-Object -Skip ([Math]::Max(0, $KeepLatest)))
Write-Host ("暂存子目录：{0} 个（保留最新 {1} 个，检查其余 {2} 个）" -f $folders.Count, $keep.Count, $candidates.Count)

$deleteList = New-Object System.Collections.ArrayList
$keepList = New-Object System.Collections.ArrayList

foreach ($f in $candidates) {
    $files = @(Get-ChildItem -LiteralPath $f.FullName -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $mediaExt -contains $_.Extension.ToLower() })
    $bytes = 0; foreach ($x in $files) { $bytes += $x.Length }

    if ($files.Count -eq 0) {
        [void]$deleteList.Add([pscustomobject]@{
            Folder = $f.FullName; Files = 0; GB = [math]::Round($bytes / 1GB, 3)
            Status = 'empty'; Note = '只有清单/空目录'
        })
        continue
    }

    $unmatched = New-Object System.Collections.ArrayList
    $sizeMismatch = New-Object System.Collections.ArrayList
    foreach ($x in $files) {
        $key = "$($x.Name)|$($x.Length)"
        $libPath = $byNameSize[$key]
        if (-not $libPath) {
            if ($byName.ContainsKey($x.Name)) { [void]$sizeMismatch.Add($x.Name) } else { [void]$unmatched.Add($x.Name) }
            continue
        }
        if ($DeepVerify) {
            $h1 = (Get-FileHash -LiteralPath $x.FullName -Algorithm SHA256).Hash
            $h2 = (Get-FileHash -LiteralPath $libPath -Algorithm SHA256).Hash
            if ($h1 -ne $h2) { [void]$unmatched.Add($x.Name) }
        }
    }

    if ($unmatched.Count -eq 0 -and $sizeMismatch.Count -eq 0) {
        [void]$deleteList.Add([pscustomobject]@{
            Folder = $f.FullName; Files = $files.Count; GB = [math]::Round($bytes / 1GB, 3)
            Status = 'redundant'; Note = $(if ($DeepVerify) { '内容哈希一致' } else { '库里同名同大小' })
        })
    }
    else {
        [void]$keepList.Add([pscustomobject]@{
            Folder = $f.FullName; Files = $files.Count; GB = [math]::Round($bytes / 1GB, 3)
            Status = 'kept'; Note = ("库里找不到相同副本：{0} 个缺名、{1} 个同名但大小不同" -f $unmatched.Count, $sizeMismatch.Count)
        })
    }
}

foreach ($k in $keep) {
    [void]$keepList.Add([pscustomobject]@{
        Folder = $k.FullName
        Files = @(Get-ChildItem -LiteralPath $k.FullName -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $mediaExt -contains $_.Extension.ToLower() }).Count
        GB = [math]::Round(((@(Get-ChildItem -LiteralPath $k.FullName -Recurse -File -ErrorAction SilentlyContinue) | Measure-Object Length -Sum).Sum) / 1GB, 3)
        Status = 'kept'; Note = '最新的一份，保留'
    })
}

Write-Host ''
Write-Host '=== 可安全删除 ==='
if ($deleteList.Count -eq 0) { Write-Host '  （无）' }
else {
    $deleteList | ForEach-Object { Write-Host ("  {0}  {1} 个文件  {2} GB  [{3}] {4}" -f $_.Folder, $_.Files, $_.GB, $_.Status, $_.Note) }
    $totalGb = 0; foreach ($d in $deleteList) { $totalGb += $d.GB }
    Write-Host ("  合计：{0} GB" -f [math]::Round($totalGb, 2))
}
Write-Host ''
Write-Host '=== 保留 ==='
if ($keepList.Count -eq 0) { Write-Host '  （无）' }
else { $keepList | ForEach-Object { Write-Host ("  {0}  {1} 个文件  {2} GB  [{3}] {4}" -f $_.Folder, $_.Files, $_.GB, $_.Status, $_.Note) } }

if (-not $Execute) {
    Write-Host ''
    Write-Host '扫描完成，没有删除任何东西。加 -Execute 才会真正删除上面「可安全删除」的目录。'
    exit 0
}

# ------------------------------------------------------------------ delete
if ($deleteList.Count -eq 0) { Write-Host ''; Write-Host '没有可删除的目录。'; exit 0 }

$journalDir = Join-Path $WorkRoot 'runs'
if (-not (Test-Path -LiteralPath $journalDir)) { New-Item -ItemType Directory -Force -Path $journalDir | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$journal = Join-Path $journalDir "cleanup-staging-$stamp.csv"
$rows = New-Object System.Collections.ArrayList
$deleted = 0; $freed = 0.0; $failed = 0

foreach ($d in $deleteList) {
    $res = 'ok'
    try {
        if (Test-Path -LiteralPath $d.Folder) { Remove-Item -LiteralPath $d.Folder -Recurse -Force -ErrorAction Stop }
        if (Test-Path -LiteralPath $d.Folder) { $res = 'error: 目录仍存在' }
    }
    catch { $res = 'error: ' + $_.Exception.Message }
    if ($res -eq 'ok') { $deleted++; $freed += $d.GB } else { $failed++ }
    [void]$rows.Add([pscustomobject]@{
        Folder = $d.Folder; Files = $d.Files; GB = $d.GB; Verify = $(if ($DeepVerify) { 'sha256' } else { 'name+size' }); Result = $res
    })
}
$rows | Export-Csv -Path $journal -NoTypeInformation -Encoding UTF8

Write-Host ''
Write-Host '=== 删除结果 ==='
Write-Host ("已删除目录：{0}  失败：{1}  释放空间：约 {2} GB" -f $deleted, $failed, [math]::Round($freed, 2))
Write-Host ("清理日志：{0}" -f $journal)
Write-Host '照片库本身未被改动（这些只是暂存副本）。'
