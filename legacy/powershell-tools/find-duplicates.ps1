# find-duplicates.ps1 - content-level duplicate finder (and optional quarantine)
#
# Two stages so a large library does not need a full re-read every run:
#   stage 1: fingerprint = SHA256(size + first 256KB + last 256KB)
#   stage 2: full SHA256, only for files sharing a stage-1 fingerprint
# Both results are cached in one CSV keyed by path+size+mtime, so a re-run over an
# unchanged library costs almost nothing (new/changed files are the only reads).
#
# Only byte-identical files are ever called duplicates (full SHA256 decides), so
# RAW+JPEG pairs and burst frames are never treated as duplicates.
#
# Nothing is ever deleted. Extras are moved to a quarantine folder on the same
# volume, with a journal and an undo script.
#
# Usage:
#   # whole library (incremental; this is what the pipeline uses)
#   powershell -ExecutionPolicy Bypass -File find-duplicates.ps1 -Roots C:\Photos,C:\Videos
#   # or a specific list of files (e.g. a move journal from classify-photos.ps1)
#   powershell -ExecutionPolicy Bypass -File find-duplicates.ps1 -Journal <move-journal.csv>
#   # add -Quarantine to actually move the extras aside
#
# NOTE: keep this file UTF-8 with BOM (PowerShell 5.1 reads BOM-less files as GBK).

[CmdletBinding(DefaultParameterSetName = 'Journal')]
param(
    [Parameter(ParameterSetName = 'Journal', Mandatory = $true)][string]$Journal,
    [Parameter(ParameterSetName = 'Roots', Mandatory = $true)][string[]]$Roots,
    [string]$OutDir = 'C:\Photos\_归类清单',
    [string]$CachePath = '',                        # defaults to <OutDir>\dup-cache.csv
    [string]$QuarantineRoot = '',   # defaults to <OutDir>\_重复文件（确认后可删除）
    [string]$PhotoRoot = 'C:\Photos',
    [string]$VideoRoot = 'C:\Videos',
    [int]$HeadTailBytes = 262144,
    [switch]$AllowCrossFolder,                      # default: cross-folder copies are reported, not moved
    [switch]$Quarantine
)

$ErrorActionPreference = 'Stop'
$sha = [System.Security.Cryptography.SHA256]::Create()
if (-not $CachePath) { $CachePath = Join-Path $OutDir 'dup-cache.csv' }
if (-not $QuarantineRoot) { $QuarantineRoot = Join-Path $OutDir '_重复文件（确认后可删除）' }

function Get-Fingerprint {
    param([string]$Path, [int64]$Size)
    $len = [int]$HeadTailBytes
    if ($Size -le ([int64]$len * 2)) {
        return [BitConverter]::ToString($sha.ComputeHash([System.IO.File]::ReadAllBytes($Path))).Replace('-', '')
    }
    $buf = New-Object byte[] ($len * 2)
    $fs = [System.IO.File]::OpenRead($Path)
    try {
        $got = 0
        while ($got -lt $len) { $n = $fs.Read($buf, $got, $len - $got); if ($n -le 0) { break }; $got += $n }
        $fs.Seek(-$len, [System.IO.SeekOrigin]::End) | Out-Null
        $got2 = 0
        while ($got2 -lt $len) { $n = $fs.Read($buf, $len + $got2, $len - $got2); if ($n -le 0) { break }; $got2 += $n }
    }
    finally { $fs.Dispose() }
    return [BitConverter]::ToString($sha.ComputeHash($buf)).Replace('-', '')
}

# ---------------------------------------------------------------- load input
if ($PSCmdlet.ParameterSetName -eq 'Journal') {
    if (-not (Test-Path $Journal)) { throw "journal not found: $Journal" }
    $rows = @(Import-Csv $Journal | Where-Object { $_.Result -eq 'ok' -and (Test-Path -LiteralPath $_.Dest) } |
        ForEach-Object { [pscustomobject]@{ Dest = $_.Dest; Size = $_.Size; MTime = $_.MTime } })
    Write-Host ("input: journal {0} -> {1} files" -f $Journal, $rows.Count)
}
else {
    $rows = New-Object System.Collections.ArrayList
    foreach ($r in $Roots) {
        if (-not (Test-Path $r)) { Write-Warning "root not found: $r"; continue }
        foreach ($f in (Get-ChildItem -LiteralPath $r -Recurse -File -Force -ErrorAction SilentlyContinue)) {
            if ($f.FullName -like '*\_归类清单\*') { continue }     # never touch journals / quarantine
            [void]$rows.Add([pscustomobject]@{ Dest = $f.FullName; Size = $f.Length; MTime = $f.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss') })
        }
    }
    Write-Host ("input: roots {0} -> {1} files" -f ($Roots -join ', '), $rows.Count)
}

$bySize = $rows | Group-Object Size | Where-Object { $_.Count -gt 1 }
$cand = @($bySize | ForEach-Object { $_.Group })
Write-Host ("size-identical candidates: {0} ({1} size groups); skipped (unique size): {2}" -f `
    $cand.Count, @($bySize).Count, ($rows.Count - $cand.Count))

# ------------------------------------------------------------- shared cache
$cache = @{}
if (Test-Path $CachePath) {
    foreach ($c in (Import-Csv $CachePath)) { if ($c.Path) { $cache[$c.Path] = $c } }
    Write-Host ("cache entries loaded: {0}" -f $cache.Count)
}
$newCache = @{}

# ------------------------------------------------- stage 1: cheap fingerprint
$sw = [Diagnostics.Stopwatch]::StartNew()
$fp = @{}
$i = 0; $fpHit = 0
foreach ($r in $cand) {
    $i++
    $f = $null
    $prev = if ($cache.ContainsKey($r.Dest)) { $cache[$r.Dest] } else { $null }
    if ($prev -and $prev.FP -and [int64]$prev.Size -eq [int64]$r.Size -and $prev.MTime -eq $r.MTime) { $f = $prev.FP; $fpHit++ }
    if (-not $f) {
        try { $f = Get-Fingerprint -Path $r.Dest -Size ([int64]$r.Size) }
        catch { Write-Warning "cannot read: $($r.Dest)"; continue }
    }
    $carryHash = ''
    if ($prev -and $prev.Hash -and [int64]$prev.Size -eq [int64]$r.Size -and $prev.MTime -eq $r.MTime) { $carryHash = $prev.Hash }
    $newCache[$r.Dest] = [pscustomobject]@{ Path = $r.Dest; Size = $r.Size; MTime = $r.MTime; FP = $f; Hash = $carryHash }
    $key = "$($r.Size)|$f"
    if (-not $fp.ContainsKey($key)) { $fp[$key] = New-Object System.Collections.ArrayList }
    [void]$fp[$key].Add($r)
    if ($i % 2000 -eq 0) { Write-Host ("  fingerprinted {0}/{1}  ({2} min, cache hits {3})" -f $i, $cand.Count, [math]::Round($sw.Elapsed.TotalMinutes, 1), $fpHit) }
}
$fpGroups = @($fp.Values | Where-Object { $_.Count -gt 1 })
$fpFiles = @($fpGroups | ForEach-Object { $_ })
Write-Host ("stage 1 done in {0} min: {1} fingerprint groups, {2} files to hash (cache hits {3})" -f `
    [math]::Round($sw.Elapsed.TotalMinutes, 1), $fpGroups.Count, $fpFiles.Count, $fpHit)

# ------------------------------------------------------ stage 2: full SHA256
$sw2 = [Diagnostics.Stopwatch]::StartNew()
$byHash = @{}
$j = 0; $hashHit = 0
foreach ($r in $fpFiles) {
    $j++
    $h = $null
    if ($newCache.ContainsKey($r.Dest) -and $newCache[$r.Dest].Hash) { $h = $newCache[$r.Dest].Hash; $hashHit++ }
    if (-not $h) { $h = (Get-FileHash -LiteralPath $r.Dest -Algorithm SHA256).Hash }
    $newCache[$r.Dest].Hash = $h
    if (-not $byHash.ContainsKey($h)) { $byHash[$h] = New-Object System.Collections.ArrayList }
    [void]$byHash[$h].Add($r)
    if ($j % 2000 -eq 0) { Write-Host ("  hashed {0}/{1}  ({2} min, cache hits {3})" -f $j, $fpFiles.Count, [math]::Round($sw2.Elapsed.TotalMinutes, 1), $hashHit) }
}
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
# keep untouched entries so the cache stays useful next run
$merged = @{}
foreach ($k in $cache.Keys) { if (-not $newCache.ContainsKey($k)) { $merged[$k] = $cache[$k] } }
foreach ($k in $newCache.Keys) { $merged[$k] = $newCache[$k] }
$merged.Values | Export-Csv -Path $CachePath -NoTypeInformation -Encoding UTF8
$dupGroups = @($byHash.Values | Where-Object { $_.Count -gt 1 })
Write-Host ("stage 2 done in {0} min: {1} true duplicate groups (cache hits {2})" -f `
    [math]::Round($sw2.Elapsed.TotalMinutes, 1), $dupGroups.Count, $hashHit)

# --------------------------------------------------------------- plan output
$plan = New-Object System.Collections.ArrayList
$gi = 0
foreach ($entry in $byHash.GetEnumerator()) {
    if ($entry.Value.Count -lt 2) { continue }
    $gi++
    $g = $entry.Value
    # keeper: no __N suffix first, then earliest mtime, then shortest path
    $sorted = @($g | Sort-Object `
        @{ Expression = { if ((Split-Path $_.Dest -Leaf) -match '__\d+\.[^.]+$') { 1 } else { 0 } } }, `
        @{ Expression = { [datetime]$_.MTime } }, `
        @{ Expression = { $_.Dest.Length } })
    $keep = $sorted[0]
    $keepDir = Split-Path $keep.Dest -Parent
    foreach ($m in $sorted) {
        $isDup = ($m.Dest -ne $keep.Dest)
        $sameDir = ((Split-Path $m.Dest -Parent) -eq $keepDir)
        $role = 'keep'
        if ($isDup) { $role = if ($sameDir -or $AllowCrossFolder) { 'duplicate' } else { 'duplicate-other-folder' } }
        [void]$plan.Add([pscustomobject]@{
            GroupId = ('G{0:D4}' -f $gi)
            Hash = $entry.Key
            Size = [int64]$m.Size
            Role = $role
            Path = $m.Dest
            KeepPath = $keep.Dest
            MTime = $m.MTime
        })
    }
}

$removable = @($plan | Where-Object { $_.Role -eq 'duplicate' })
$crossFolder = @($plan | Where-Object { $_.Role -eq 'duplicate-other-folder' })
$dupBytes = 0; foreach ($d in $removable) { $dupBytes += $d.Size }

Write-Host ""
Write-Host "=== duplicate report ==="
Write-Host ("duplicate groups        : {0}" -f $dupGroups.Count)
Write-Host ("copies to remove        : {0}" -f $removable.Count)
Write-Host ("cross-folder (kept)     : {0}" -f $crossFolder.Count)
Write-Host ("unique kept             : {0}" -f @($plan | Where-Object { $_.Role -eq 'keep' }).Count)
Write-Host ("reclaimable             : {0} GB" -f [math]::Round($dupBytes / 1GB, 2))
if ($removable.Count) {
    Write-Host ""
    Write-Host "copies by city:"
    $removable | Group-Object { $p = Split-Path $_.Path -Parent; Split-Path (Split-Path $p -Parent) -Leaf } |
        Sort-Object Count -Descending | Select-Object Count, Name | Format-Table -AutoSize | Out-String -Width 60 | Write-Host
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$planPath = Join-Path $OutDir "duplicate-plan-$stamp.csv"
$plan | Export-Csv -Path $planPath -NoTypeInformation -Encoding UTF8
Write-Host "plan written: $planPath"

if (-not $Quarantine) {
    Write-Host ""
    Write-Host "DRY RUN - nothing moved. Re-run with -Quarantine to move the extra copies aside."
    return
}

# ------------------------------------------------------------- quarantine
$journalPath = Join-Path $OutDir "dedupe-journal-$stamp.csv"
$sw3 = New-Object System.IO.StreamWriter($journalPath, $false, (New-Object System.Text.UTF8Encoding($false)))
$sw3.WriteLine('Source,Quarantine,Size,MTime,Result')
$sw3.Flush()

$moved = 0; $failed = 0
try {
    foreach ($d in $removable) {
        $src = $d.Path
        $root = if ($src -like "$VideoRoot\*") { $VideoRoot } else { $PhotoRoot }
        $rel = $src.Substring($root.Length).TrimStart('\')
        $dest = Join-Path (Join-Path $QuarantineRoot (Split-Path $root -Leaf)) $rel
        $res = 'ok'
        try {
            $destDir = Split-Path $dest -Parent
            if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
            if (Test-Path -LiteralPath $dest) {
                # an older quarantine run may already hold a file with this name -
                # never overwrite it, park this one next to it with a _qN suffix
                $qb = [IO.Path]::GetFileNameWithoutExtension($dest)
                $qe = [IO.Path]::GetExtension($dest)
                $qn = 2
                do { $dest = Join-Path $destDir ('{0}_q{1}{2}' -f $qb, $qn, $qe); $qn++ } while (Test-Path -LiteralPath $dest)
            }
            Move-Item -LiteralPath $src -Destination $dest -ErrorAction Stop
            if (-not (Test-Path -LiteralPath $dest) -or (Get-Item -LiteralPath $dest).Length -ne $d.Size) {
                throw "verification failed after moving $src"
            }
            $moved++
        }
        catch { $res = 'error: ' + $_.Exception.Message; $failed++ }
        $sw3.WriteLine(('"{0}","{1}",{2},"{3}","{4}"' -f $src, $dest, $d.Size, $d.MTime, ($res -replace '"', "'")))
        $sw3.Flush()
        if ($moved % 500 -eq 0 -and $moved -gt 0) { Write-Host ("  quarantined {0}/{1}" -f $moved, $removable.Count) }
    }
}
finally { $sw3.Close() }

Write-Host ""
Write-Host ("quarantined: {0}   failed: {1}" -f $moved, $failed)
Write-Host "journal: $journalPath"
Write-Host ("keepers missing (must be 0): {0}" -f @($plan | Where-Object { $_.Role -eq 'keep' -and -not (Test-Path -LiteralPath $_.Path) }).Count)
Write-Host ("duplicates still in the library (must be 0): {0}" -f @($removable | Where-Object { Test-Path -LiteralPath $_.Path }).Count)

$rb = @"
# undo-dedupe-$stamp.ps1 - move every quarantined file back where it came from
`$ErrorActionPreference = 'Stop'
`$journal = Join-Path `$PSScriptRoot 'dedupe-journal-$stamp.csv'
`$n = 0; `$skipped = @()
foreach (`$r in (Import-Csv `$journal)) {
    if (`$r.Result -ne 'ok') { continue }
    if (-not (Test-Path -LiteralPath `$r.Quarantine)) { `$skipped += "missing: `$(`$r.Quarantine)"; continue }
    `$dir = Split-Path `$r.Source -Parent
    if (-not (Test-Path -LiteralPath `$dir)) { New-Item -ItemType Directory -Force -Path `$dir | Out-Null }
    if (Test-Path -LiteralPath `$r.Source) { `$skipped += "occupied: `$(`$r.Source)"; continue }
    Move-Item -LiteralPath `$r.Quarantine -Destination `$r.Source
    `$n++
}
Write-Host "restored: `$n"
if (`$skipped.Count) { Write-Host "skipped:"; `$skipped | ForEach-Object { Write-Host "  `$_" } }
"@
$rbPath = Join-Path $OutDir "undo-dedupe-$stamp.ps1"
[System.IO.File]::WriteAllText($rbPath, $rb, (New-Object System.Text.UTF8Encoding($true)))
Write-Host "undo script: $rbPath"
Write-Host ""
Write-Host "Extras are now in: $QuarantineRoot"
Write-Host "Empty that folder when you are happy with the result to actually free the space."
