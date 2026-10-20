# import-from-camera.ps1 - copy photos/videos off a camera or phone (MTP/PTP device)
#
# A device connected in MTP mode has no drive letter: "此电脑\Z 5\可移动存储 10001\DCIM\101NCZ_5"
# is a shell namespace path, so Test-Path / Get-ChildItem cannot touch it. This
# script walks the shell namespace with Shell.Application and copies the files to
# a staging folder on disk, which the normal pipeline can then process.
#
# The card/device is only ever READ from - nothing is deleted or modified on it.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File import-from-camera.ps1 -Path "此电脑\Z 5\可移动存储 10001\DCIM\101NCZ_5"
#   powershell -ExecutionPolicy Bypass -File import-from-camera.ps1 -DeviceName "Z 5" -SubPath "DCIM\101NCZ_5" -List
#
# NOTE: keep this file UTF-8 with BOM (PowerShell 5.1 reads BOM-less files as GBK).

[CmdletBinding()]
param(
    [string]$Path = '',                       # shell path, e.g. 此电脑\Z 5\可移动存储 10001\DCIM\101NCZ_5
    [string]$DeviceName = '',                 # or just the device name, e.g. "Z 5"
    [string]$SubPath = 'DCIM',                # used with -DeviceName
    [string]$StagingRoot = 'C:\Staging',
    [string[]]$Include = @('.jpg', '.jpeg', '.nef', '.mov', '.mp4', '.png', '.heic', '.tif', '.tiff'),
    [int]$MinBytes = 0,                       # ignore files smaller than this
    [int]$MaxFiles = 0,                       # 0 = all (handy for a test run)
    [int]$TimeoutMinutes = 240,
    [switch]$BulkFolderCopy,                  # let the shell copy whole folders (faster, but opaque)
    [switch]$List                             # only list what is on the device, copy nothing
)

$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject Shell.Application
$thisPC = $shell.NameSpace(17)

function Get-SizeColumn {
    param($Folder)
    # the device view uses different column indices than Explorer's normal view,
    # so find the column whose value looks like a byte size
    for ($i = 0; $i -lt 16; $i++) {
        $hdr = $Folder.GetDetailsOf($null, $i)
        if (-not $hdr) { continue }
        $sample = $null
        foreach ($it in @($Folder.Items())) { $sample = $Folder.GetDetailsOf($it, $i); if ($sample) { break } }
        if ($sample -match '^\s*[\d.,]+\s*(B|KB|MB|GB|TB|字节)\s*$') { return $i }
    }
    return -1
}

function ConvertTo-Bytes {
    param([string]$Text)
    if (-not $Text) { return 0 }
    $t = $Text.Trim()
    if ($t -match '^([\d.,]+)\s*(B|字节)$') { return [int64]([double]($Matches[1] -replace ',', '')) }
    if ($t -match '^([\d.,]+)\s*KB$') { return [int64]([double]($Matches[1] -replace ',', '') * 1KB) }
    if ($t -match '^([\d.,]+)\s*MB$') { return [int64]([double]($Matches[1] -replace ',', '') * 1MB) }
    if ($t -match '^([\d.,]+)\s*GB$') { return [int64]([double]($Matches[1] -replace ',', '') * 1GB) }
    return 0
}

function Resolve-DeviceItem {
    param([string[]]$Parts)
    $cur = $thisPC
    $item = $null
    for ($i = 0; $i -lt $Parts.Count; $i++) {
        $name = $Parts[$i]
        if (-not $name) { continue }
        $item = @($cur.Items()) | Where-Object { $_.Name -eq $name } | Select-Object -First 1
        if (-not $item) { return $null }
        if ($i -lt ($Parts.Count - 1)) {
            $cur = $item.GetFolder
            if ($null -eq $cur) { return $null }
        }
    }
    return $item
}

# ---------------------------------------------------------------- locate it
$parts = @()
if ($Path) {
    $parts = @($Path -split '[\\/]' | Where-Object { $_ -and $_ -ne '此电脑' -and $_ -ne 'This PC' })
}
elseif ($DeviceName) {
    $parts = @($DeviceName) + @($SubPath -split '[\\/]' | Where-Object { $_ })
}
else { throw 'need -Path (shell path) or -DeviceName + -SubPath' }

Write-Host ("定位：{0}" -f ($parts -join ' \ '))
$target = Resolve-DeviceItem $parts
if ($null -eq $target) {
    Write-Host '找不到该设备/目录。可用的便携设备：'
    @($thisPC.Items()) | ForEach-Object { Write-Host ("  - " + $_.Name) }
    throw '设备未连接，或名称/路径不对（相机要开机、USB 模式选 MTP/PTP，或改用读卡器）'
}

# --------------------------------------------------------------- enumerate
$targetFolder = if ($target.IsFolder) { $target.GetFolder } else { $target.Parent }
$sizeCol = Get-SizeColumn -Folder $targetFolder
Write-Host ("命中：{0}（大小列索引 {1}）" -f $target.Name, $sizeCol)

$wanted = New-Object System.Collections.ArrayList
$subFolders = New-Object System.Collections.ArrayList     # folders to copy as a whole
foreach ($it in @($targetFolder.Items())) {
    if ($it.IsFolder) { [void]$subFolders.Add($it); continue }
    $ext = ('.' + ($it.Name -split '\.')[-1]).ToLower()
    if ($Include -notcontains $ext) { continue }
    $sizeText = if ($sizeCol -ge 0) { $targetFolder.GetDetailsOf($it, $sizeCol) } else { '' }
    $est = ConvertTo-Bytes $sizeText
    if ($est -lt $MinBytes) { continue }
    [void]$wanted.Add([pscustomobject]@{ Item = $it; Name = $it.Name; Rel = $it.Name; SizeText = $sizeText; EstBytes = $est })
}
# one level down (e.g. DCIM\101NCZ_5 when the path points at DCIM)
foreach ($sf in $subFolders) {
    $sfFolder = $sf.GetFolder
    $sc = Get-SizeColumn -Folder $sfFolder
    foreach ($it in @($sfFolder.Items())) {
        if ($it.IsFolder) { continue }
        $ext = ('.' + ($it.Name -split '\.')[-1]).ToLower()
        if ($Include -notcontains $ext) { continue }
        $sizeText = if ($sc -ge 0) { $sfFolder.GetDetailsOf($it, $sc) } else { '' }
        $est = ConvertTo-Bytes $sizeText
        if ($est -lt $MinBytes) { continue }
        [void]$wanted.Add([pscustomobject]@{ Item = $it; Name = $it.Name; Rel = "$($sf.Name)\$($it.Name)"; SizeText = $sizeText; EstBytes = $est })
    }
}

$totalEst = 0; foreach ($w in $wanted) { $totalEst += $w.EstBytes }
if ($MaxFiles -gt 0 -and $wanted.Count -gt $MaxFiles) {
    Write-Host ("-MaxFiles {0}：只取前 {0} 个（共 {1} 个）" -f $MaxFiles, $wanted.Count)
    $wanted = @($wanted | Select-Object -First $MaxFiles)
    $subFolders.Clear()
    $totalEst = 0; foreach ($w in $wanted) { $totalEst += $w.EstBytes }
}
Write-Host ("待复制：{0} 个文件，约 {1} GB" -f $wanted.Count, [math]::Round($totalEst / 1GB, 2))
$byExt = $wanted | Group-Object { ($_.Name -split '\.')[-1].ToUpper() } | Sort-Object Count -Descending
$byExt | ForEach-Object { Write-Host ("   {0}: {1}" -f $_.Name, $_.Count) }

if ($List) {
    $wanted | Select-Object -First 40 | ForEach-Object { Write-Host ("   {0,-26} {1}" -f $_.Rel, $_.SizeText) }
    Write-Host '（-List 模式，未复制任何文件）'
    return
}
if ($wanted.Count -eq 0) { throw '这个目录里没有符合扩展名的文件' }

# ------------------------------------------------------------------ stage
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stage = Join-Path $StagingRoot $stamp
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Write-Host ("暂存目录：{0}" -f $stage)

# Copy strategy: per file by default - progress is visible, each file is verified
# individually and a failure only costs that one file. -BulkFolderCopy lets the
# shell copy whole folders instead (faster on some devices, but opaque).
$destFolder = $shell.NameSpace($stage)
if ($BulkFolderCopy) {
    foreach ($sf in $subFolders) {
        Write-Host ("批量复制子目录 {0} ..." -f $sf.Name)
        try { $destFolder.CopyHere($sf, 1556) } catch { Write-Warning ("复制 {0} 失败：{1}" -f $sf.Name, $_.Exception.Message) }
    }
    # wait for the bulk copy to settle: count stops growing and total stops changing
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $lastCount = -1; $lastBytes = -1; $stable = 0
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        $files = @(Get-ChildItem -LiteralPath $stage -Recurse -File -ErrorAction SilentlyContinue)
        $bytes = 0; foreach ($f in $files) { $bytes += $f.Length }
        if ($files.Count -eq $lastCount -and $bytes -eq $lastBytes) { $stable++ } else { $stable = 0 }
        $lastCount = $files.Count; $lastBytes = $bytes
        Write-Host ("   已复制 {0}/{1} 个，{2} GB / 约 {3} GB" -f $files.Count, $wanted.Count, [math]::Round($bytes / 1GB, 2), [math]::Round($totalEst / 1GB, 2))
        if ($files.Count -ge $wanted.Count -or $stable -ge 10) { break }
    }
    if ($lastCount -lt $wanted.Count) { Write-Host '   批量复制已停止推进，转入逐个复制/补漏 ...' }
}

# ------------------------------------------------------- verify and retry
$manifest = New-Object System.Collections.ArrayList
$sw = [Diagnostics.Stopwatch]::StartNew()
for ($round = 1; $round -le 4; $round++) {
    $missing = @()
    foreach ($w in $wanted) {
        $local = Join-Path $stage $w.Rel
        if (-not (Test-Path -LiteralPath $local)) { $missing += $w; continue }
        $len = (Get-Item -LiteralPath $local).Length
        if ($len -le 0) { $missing += $w }
    }
    if ($missing.Count -eq 0) { break }
    Write-Host ("   第 {0} 轮：还需复制 {1} 个文件" -f $round, $missing.Count)
    $done = 0; $roundBytes = 0
    foreach ($w in $missing) {
        $dir = Split-Path (Join-Path $stage $w.Rel) -Parent
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $t = Join-Path $stage $w.Rel
        try {
            $shell.NameSpace($dir).CopyHere($w.Item, 1556)
            $n = 0
            while (-not (Test-Path -LiteralPath $t) -and $n -lt 240) { Start-Sleep -Milliseconds 500; $n++ }
        }
        catch { }
        $done++
        if (Test-Path -LiteralPath $t) { $roundBytes += (Get-Item -LiteralPath $t).Length }
        if ($done % 25 -eq 0 -or $done -eq $missing.Count) {
            Write-Host ("     {0}/{1}  已 {2} GB  用时 {3} 分钟" -f $done, $missing.Count, [math]::Round($roundBytes / 1GB, 2), [math]::Round($sw.Elapsed.TotalMinutes, 1))
        }
    }
}

# ------------------------------------------------------------------ report
$okCount = 0; $badCount = 0; $localBytes = 0
foreach ($w in $wanted) {
    $local = Join-Path $stage $w.Rel
    $status = 'MISSING'
    $len = 0
    if (Test-Path -LiteralPath $local) {
        $len = (Get-Item -LiteralPath $local).Length
        # the device only reports a rounded size, so compare rounded values
        $estRounded = if ($w.EstBytes -gt 0) { [math]::Round($w.EstBytes / 1MB, 0) } else { -1 }
        $gotRounded = [math]::Round($len / 1MB, 0)
        $status = if ($len -le 0) { 'EMPTY' } elseif ($estRounded -ge 0 -and [math]::Abs($estRounded - $gotRounded) -gt 2) { 'SIZE-DIFF' } else { 'ok' }
        $localBytes += $len
    }
    if ($status -eq 'ok') { $okCount++ } else { $badCount++ }
    [void]$manifest.Add([pscustomobject]@{
        RelPath = $w.Rel; DeviceSize = $w.SizeText; LocalBytes = $len; Status = $status
    })
}
$csv = Join-Path $stage '_camera-import-manifest.csv'
$manifest | Export-Csv -Path $csv -NoTypeInformation -Encoding UTF8

Write-Host ''
Write-Host '=== 相机导入结果 ==='
Write-Host ("来源设备 : {0}（{1}）" -f $target.Name, ($parts -join ' \ '))
Write-Host ("暂存目录 : {0}" -f $stage)
Write-Host ("成功     : {0} / {1}" -f $okCount, $wanted.Count)
Write-Host ("异常     : {0}" -f $badCount)
Write-Host ("已落地   : {0} GB" -f [math]::Round($localBytes / 1GB, 2))
Write-Host ("清单     : {0}" -f $csv)
if ($badCount -gt 0) {
    Write-Host ''
    Write-Host '异常明细：'
    $manifest | Where-Object { $_.Status -ne 'ok' } | Select-Object -First 30 | ForEach-Object { Write-Host ("   {0}  {1}  {2}" -f $_.Status, $_.RelPath, $_.DeviceSize) }
}
Write-Host ''
Write-Host ("STAGING-PATH: {0}" -f $stage)
Write-Host '相机/存储卡上的文件没有被改动过（只读复制）。'
