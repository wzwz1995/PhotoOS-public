# photo-pipeline.ps1 - one command to import new photos/videos into the organised library
#
#   scan metadata -> resolve cities -> classify by city/session -> remove duplicates
#
# Designed for repeat use: point it at a new folder (camera card, phone export,
# download folder) and it does the whole job. Everything is incremental - the
# geocode cache and the duplicate hash cache persist between runs, so only new
# files cost real work.
#
# Safety contract
#   * dry run by default: nothing moves until you pass -Execute
#   * same-volume moves only; nothing is ever deleted or overwritten
#   * every moved file is verified (exists + identical byte length) right after the move
#   * every run writes an inventory, a plan, a move journal, a dedupe journal,
#     a rollback script and a run report into <WorkRoot>\runs\<timestamp>\
#   * duplicates are moved to a quarantine folder, never deleted
#
# Usage
#   # 1) see what would happen
#   powershell -ExecutionPolicy Bypass -File photo-pipeline.ps1 -Source "C:\Inbox"
#   # 2) do it
#   powershell -ExecutionPolicy Bypass -File photo-pipeline.ps1 -Source "C:\Inbox" -Execute
#   # shortcut: .\import-photos.cmd "C:\Inbox"        (add -Execute to actually move)
#
# NOTE: keep this file UTF-8 with BOM (PowerShell 5.1 reads BOM-less files as GBK).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)][string[]]$Source,
    [string]$PhotoRoot = 'C:\Photos',
    [string]$VideoRoot = 'C:\Videos',
    [string]$WorkRoot = 'C:\Photos\_归类清单',
    [double]$GapHours = 12,
    [int]$MaxFiles = 0,
    [string]$CameraStagingRoot = 'C:\CameraImport',
    [int]$CameraMaxFiles = 0,
    [switch]$Execute,
    [switch]$SkipDedupe,
    [switch]$SkipGeocode,
    [switch]$AllowCrossFolderDedupe
)

$ErrorActionPreference = 'Stop'
$tools = $PSScriptRoot
$script:step = 0

function Say($msg) { Write-Host $msg }
function Step($msg) { $script:step++; Write-Host ""; Write-Host ("=== [{0}] {1} ===" -f $script:step, $msg) }
function Newest($dir, $pattern) {
    $f = Get-ChildItem -LiteralPath $dir -Filter $pattern -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1
    if ($f) { return $f.FullName }
    return $null
}

$mode = if ($Execute) { 'EXECUTE' } else { 'DRY RUN' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$runDir = Join-Path (Join-Path $WorkRoot 'runs') $stamp
$geoCache = Join-Path $WorkRoot 'geo-cache.csv'
$dupCache = Join-Path $WorkRoot 'dup-cache.csv'
$quarantine = Join-Path $WorkRoot '_重复文件（确认后可删除）'

Say "photo-pipeline  mode=$mode"
Say ("  source      : {0}" -f ($Source -join ' | '))
Say ("  photo root  : {0}" -f $PhotoRoot)
Say ("  video root  : {0}" -f $VideoRoot)
Say ("  run folder  : {0}" -f $runDir)

# --------------------------------------------------------------- preflight
Step 'preflight'
# A camera / phone in MTP mode has no drive letter, so its path
# ("此电脑\Z 5\可移动存储 10001\DCIM\101NCZ_5") is not a filesystem path.
# Copy those files to a staging folder first, then run the normal pipeline on it.
$resolvedSources = New-Object System.Collections.ArrayList
$stagedSources = New-Object System.Collections.ArrayList
foreach ($s in $Source) {
    if (Test-Path -LiteralPath $s) { [void]$resolvedSources.Add((Resolve-Path -LiteralPath $s).Path); continue }
    Say ("  '{0}' 不是文件系统路径，按相机/便携设备处理 ..." -f $s)
    $camArgs = @{ Path = $s; StagingRoot = $CameraStagingRoot }
    if ($CameraMaxFiles -gt 0) { $camArgs['MaxFiles'] = $CameraMaxFiles }
    $camOut = & (Join-Path $tools 'import-from-camera.ps1') @camArgs *>&1
    foreach ($line in $camOut) { Say ("    " + [string]$line) }
    $stageLine = @($camOut | Where-Object { $_ -like 'STAGING-PATH:*' } | Select-Object -Last 1)
    if (-not $stageLine) { throw ("无法从设备读取：{0}（相机是否开机 / USB 模式是否正确 / 是否改用读卡器）" -f $s) }
    $stage = ($stageLine[0] -replace '^STAGING-PATH:\s*', '').Trim()
    Say ("  -> 已复制到暂存目录：{0}" -f $stage)
    [void]$stagedSources.Add($stage)
    [void]$resolvedSources.Add($stage)
}
$Source = $resolvedSources.ToArray()

foreach ($s in $Source) { if (-not (Test-Path -LiteralPath $s)) { throw "source not found: $s" } }
foreach ($r in @($PhotoRoot, $VideoRoot)) {
    if (-not (Test-Path -LiteralPath $r)) { New-Item -ItemType Directory -Force -Path $r | Out-Null; Say "  created $r" }
}
foreach ($s in $Source) {
    $full = (Resolve-Path -LiteralPath $s).Path
    if ($full -like "$PhotoRoot*" -or $full -like "$VideoRoot*") { throw "source is already inside the library: $full" }
}
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$drive = (Get-Item -LiteralPath $PhotoRoot).PSDrive
if ($drive) { Say ("  free space on {0}: {1} GB" -f $drive.Name, [math]::Round($drive.Free / 1GB, 1)) }
foreach ($t in @('read-exif.ps1', 'resolve-cities.ps1', 'classify-photos.ps1', 'find-duplicates.ps1', 'import-from-camera.ps1')) {
    if (-not (Test-Path (Join-Path $tools $t))) { throw "missing tool: $t (expected in $tools)" }
}
Say '  tools ok'

# ---------------------------------------------------- library index (merge map)
# Without it a new import would create a parallel session folder for a session
# the library already has (and re-imported duplicates would land elsewhere).
$indexPath = Join-Path $WorkRoot 'library-index.csv'
Step 'library index'
if (-not (Test-Path -LiteralPath $indexPath)) {
    $libNow = @(Get-ChildItem -LiteralPath $PhotoRoot, $VideoRoot -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notlike "*$([IO.Path]::DirectorySeparatorChar)_归类清单$([IO.Path]::DirectorySeparatorChar)*" })
    if ($libNow.Count -eq 0) { Say '  library is empty - index will be created by this run' }
    else {
        Say ("  no index yet, building one from {0} existing library files ..." -f $libNow.Count)
        $tmp = Join-Path $runDir 'library-scan.csv'
        & (Join-Path $tools 'read-exif.ps1') -Path @($PhotoRoot, $VideoRoot) -Recurse -AsObject |
            Export-Csv -Path $tmp -NoTypeInformation -Encoding UTF8
        $rows = New-Object System.Collections.ArrayList
        foreach ($r in (Import-Csv $tmp)) {
            $p = Join-Path $r.Dir $r.Name
            if ($p -like "*$([IO.Path]::DirectorySeparatorChar)_归类清单$([IO.Path]::DirectorySeparatorChar)*") { continue }
            $folder = Split-Path $r.Dir -Leaf
            $city = Split-Path (Split-Path $r.Dir -Parent) -Leaf
            $ct = $r.FileWriteTime
            if ($r.DateTimeOriginal -match '^\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}$') {
                $ct = ([datetime]::ParseExact($r.DateTimeOriginal, 'yyyy:MM:dd HH:mm:ss', $null)).ToString('yyyy-MM-dd HH:mm:ss')
            }
            [void]$rows.Add([pscustomobject]@{
                Path = $p; Name = $r.Name; Kind = $(if ($r.Kind -like '*video*' -or $r.Kind -like 'mov*' -or $r.Kind -like 'mp4*') { 'video' } else { 'photo' })
                Size = (Get-Item -LiteralPath $p).Length; MTime = $r.FileWriteTime
                CaptureTime = $ct; City = $city; SessionFolder = $folder
            })
        }
        $rows | Export-Csv -Path $indexPath -NoTypeInformation -Encoding UTF8
        Say ("  index built: {0} entries -> {1}" -f $rows.Count, $indexPath)
    }
}
else {
    $idxCount = @(Import-Csv -LiteralPath $indexPath).Count
    Say ("  index loaded: {0} entries ({1})" -f $idxCount, $indexPath)
}

# ------------------------------------------------------------ 1. inventory
Step 'scan metadata (read-exif.ps1)'
& (Join-Path $tools 'read-exif.ps1') -Path $Source -Recurse -AsObject |
    Export-Csv -Path (Join-Path $runDir 'inventory.csv') -NoTypeInformation -Encoding UTF8
$invPath = Join-Path $runDir 'inventory.csv'
$inv = @(Import-Csv $invPath)
$invWithTime = @($inv | Where-Object { $_.DateTimeOriginal -ne '' })
$invWithGps = @($inv | Where-Object { $_.GpsLat -ne '' -and $_.GpsLat })
Say ("  files scanned : {0}" -f $inv.Count)
Say ("  capture time  : {0}" -f $invWithTime.Count)
Say ("  GPS present   : {0}" -f $invWithGps.Count)

# ------------------------------------------------------------- 2. geocoding
if (-not $SkipGeocode -and $invWithGps.Count -gt 0) {
    Step 'resolve GPS clusters to cities (resolve-cities.ps1)'
    & (Join-Path $tools 'resolve-cities.ps1') -Inventory $invPath -CachePath $geoCache -CellDeg 0.05 -MinSepDeg 0.0 -MaxCalls 400 -DelayMs 250 |
        Select-Object -Last 12 | Out-String -Width 120 | Write-Host
}
else { Say ''; Say '(geocoding skipped)' }

# ------------------------------------------------------------ 3. classify
Step ("classify into {0} / {1}" -f $PhotoRoot, $VideoRoot)
$classifyArgs = @{
    Inventory    = $invPath
    GeoCache     = $geoCache
    PhotoRoot    = $PhotoRoot
    VideoRoot    = $VideoRoot
    JournalDir   = $runDir
    GapHours     = $GapHours
    LibraryIndex = $indexPath
    IndexOut     = $indexPath
}
if ($MaxFiles -gt 0) { $classifyArgs['MaxFiles'] = $MaxFiles }
if ($Execute) { $classifyArgs['Execute'] = $true }
& (Join-Path $tools 'classify-photos.ps1') @classifyArgs
$classifyPlan = Newest $runDir 'plan-*.csv'
$moveJournal = Newest $runDir 'move-journal-*.csv'
# NOTE: never write this as `$plan = if (...) { @(...) }` - PowerShell unwraps a
# one-element array into the bare object, and a PSCustomObject has no .Count.
$plan = @()
if ($classifyPlan) { $plan = @(Import-Csv $classifyPlan) }
Say ("  planned files : {0}" -f $plan.Count)
if ($moveJournal) {
    $mj = @(Import-Csv $moveJournal)
    Say ("  moved (ok)    : {0} / failed: {1}" -f @($mj | Where-Object { $_.Result -eq 'ok' }).Count, @($mj | Where-Object { $_.Result -ne 'ok' }).Count)
}

# -------------------------------------------------------------- 4. dedupe
$dedupePlan = $null; $dedupeJournal = $null
if (-not $SkipDedupe) {
    Step 'find duplicate content (find-duplicates.ps1)'
    $dupArgs = @{
        Roots          = @($PhotoRoot, $VideoRoot)
        OutDir         = $runDir
        CachePath      = $dupCache
        QuarantineRoot = $quarantine
        PhotoRoot      = $PhotoRoot
        VideoRoot      = $VideoRoot
    }
    if ($Execute) { $dupArgs['Quarantine'] = $true }
    if ($AllowCrossFolderDedupe) { $dupArgs['AllowCrossFolder'] = $true }
    & (Join-Path $tools 'find-duplicates.ps1') @dupArgs
    $dedupePlan = Newest $runDir 'duplicate-plan-*.csv'
    $dedupeJournal = Newest $runDir 'dedupe-journal-*.csv'

    # quarantined files must leave the index too, or the next import would try
    # to merge into a folder listing files that are no longer there
    if ($dedupeJournal -and (Test-Path -LiteralPath $indexPath)) {
        $q = @(Import-Csv -LiteralPath $dedupeJournal | Where-Object { $_.Result -eq 'ok' })
        if ($q.Count) {
            $gone = @{}
            foreach ($r in $q) { $gone[$r.Source] = $true }
            $kept = @(Import-Csv -LiteralPath $indexPath | Where-Object { -not $gone.ContainsKey($_.Path) })
            $kept | Export-Csv -Path $indexPath -NoTypeInformation -Encoding UTF8
            Say ("  library index pruned: {0} quarantined entries removed ({1} left)" -f $q.Count, $kept.Count)
        }
    }
}
else { Say ''; Say '(dedupe skipped)' }

# ------------------------------------------------------------- 5. report
Step 'write run report'
$libFiles = @(Get-ChildItem -LiteralPath $PhotoRoot, $VideoRoot -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notlike "*$([IO.Path]::DirectorySeparatorChar)_归类清单$([IO.Path]::DirectorySeparatorChar)*" })
$libBytes = ($libFiles | Measure-Object Length -Sum).Sum
$qFiles = @()
if (Test-Path $quarantine) { $qFiles = @(Get-ChildItem -LiteralPath $quarantine -Recurse -File -Force) }

$rep = New-Object System.Collections.ArrayList
[void]$rep.Add('# 照片流水线运行报告')
[void]$rep.Add('')
[void]$rep.Add(('- 运行时间：{0}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')))
[void]$rep.Add(('- 模式：**{0}**' -f $mode))
[void]$rep.Add(('- 来源：{0}' -f ($Source -join ' ; ')))
[void]$rep.Add('')
[void]$rep.Add('## 步骤结果')
[void]$rep.Add('')
[void]$rep.Add('| 步骤 | 结果 |')
[void]$rep.Add('| --- | --- |')
[void]$rep.Add(('| 扫描元数据 | {0} 个文件（有拍摄时间 {1}，有 GPS {2}） |' -f $inv.Count, $invWithTime.Count, $invWithGps.Count))
[void]$rep.Add(('| 归类计划 | {0} 个文件 |' -f $plan.Count))
if ($moveJournal) {
    $mj = @(Import-Csv $moveJournal)
    [void]$rep.Add(('| 实际移动 | {0} 成功 / {1} 失败 |' -f @($mj | Where-Object { $_.Result -eq 'ok' }).Count, @($mj | Where-Object { $_.Result -ne 'ok' }).Count))
}
else { [void]$rep.Add('| 实际移动 | 未执行（dry run） |') }
if ($dedupePlan) {
    $dp = @(Import-Csv $dedupePlan)
    $dRemove = @($dp | Where-Object { $_.Role -eq 'duplicate' })
    $dCross = @($dp | Where-Object { $_.Role -eq 'duplicate-other-folder' })
    [void]$rep.Add(('| 查重 | 重复组 {0}，可移除副本 {1}，跨文件夹未动 {2} |' -f @($dp | Group-Object GroupId).Count, $dRemove.Count, $dCross.Count))
    if ($dedupeJournal) { [void]$rep.Add(('| 隔离副本 | {0} 个 |' -f @(Import-Csv $dedupeJournal).Count)) }
}
[void]$rep.Add('')
[void]$rep.Add('## 运行后库内规模')
[void]$rep.Add('')
[void]$rep.Add(('- 照片 + 视频：**{0} 个文件 / {1} GB**' -f $libFiles.Count, [math]::Round($libBytes / 1GB, 2)))
[void]$rep.Add(('- 待确认删除的隔离副本：{0} 个' -f $qFiles.Count))
[void]$rep.Add('')
[void]$rep.Add('## 产物')
[void]$rep.Add('')
foreach ($f in (Get-ChildItem -LiteralPath $runDir -File | Sort-Object Name)) {
    [void]$rep.Add(('- `{0}`' -f $f.Name))
}
[void]$rep.Add('')
[void]$rep.Add('回滚：')
[void]$rep.Add('')
[void]$rep.Add('```powershell')
$restore = Newest $runDir 'restore-*.ps1'
$undo = Newest $runDir 'undo-dedupe-*.ps1'
if ($restore) { [void]$rep.Add(('powershell -ExecutionPolicy Bypass -File "{0}"' -f $restore)) }
if ($undo) { [void]$rep.Add(('powershell -ExecutionPolicy Bypass -File "{0}"' -f $undo)) }
[void]$rep.Add('```')
$p = Join-Path $runDir 'run-report.md'
[System.IO.File]::WriteAllText($p, ($rep -join "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
Say "  $p"

Say ''
Say '=== 完成 ==='
Say ("模式        : {0}" -f $mode)
Say ("库内规模    : {0} 个文件 / {1} GB" -f $libFiles.Count, [math]::Round($libBytes / 1GB, 2))
if ($stagedSources.Count -gt 0) {
    Say ''
    Say '相机/设备来源先复制到了暂存目录（真实执行后照片已从暂存移入库里，只剩导入清单 CSV）：'
    foreach ($sp in $stagedSources) { Say ("   {0}" -f $sp) }
}
if (-not $Execute) { Say '这是 dry run，没有移动任何文件。确认计划无误后加 -Execute 再跑一次。' }
else { Say '文件已就位；重复副本在隔离区，确认后可删除该目录释放空间。' }
