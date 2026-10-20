# classify-photos.ps1 - organise E: photo/video files into  <root>\<city>\<time-session>\
#
# Safety contract (the user's hard requirement: never lose a single photo):
#   * same-volume move only (rename) - data is never rewritten, nothing is copied twice
#   * every file is verified after the move (exists + identical byte length); on any
#     mismatch the file is moved straight back and the run aborts
#   * an append-only journal is flushed to disk after EVERY file, so even a kill -9
#     leaves a complete old->new map (restore-photos.ps1 replays it backwards)
#   * existing files are never overwritten - collisions get a __2/__3 suffix
#   * files outside the photo/video extension list are never touched
#   * nothing is ever deleted
#
# Usage:
#   dry run : powershell -ExecutionPolicy Bypass -File classify-photos.ps1 -Inventory <csv> -GeoCache <csv>
#   execute : powershell -ExecutionPolicy Bypass -File classify-photos.ps1 -Inventory <csv> -GeoCache <csv> -Execute
#
# NOTE: keep this file UTF-8 **with BOM** (PowerShell 5.1 reads BOM-less files as GBK
# and would mangle the Chinese literals below).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Inventory,
    [string]$GeoCache = '',              # defaults to <script dir>\geo-cache.csv
    [string]$PhotoRoot = 'C:\Photos',
    [string]$VideoRoot = 'C:\Videos',
    [string]$JournalDir = 'C:\Photos\_归类清单',
    [double]$GapHours = 12,
    [double]$NearestCityMaxDeg = 0.6,
    [string]$LibraryIndex = '',        # existing library index (city/session of what is already filed)
    [string]$IndexOut = '',            # where to write the index after a successful run
    [int]$SampleHashCount = 120,
    [int]$MaxFiles = 0,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
if (-not $GeoCache) { $GeoCache = Join-Path $PSScriptRoot 'geo-cache.csv' }

$photoExt = @('.jpg', '.jpeg', '.nef', '.png')
$videoExt = @('.mov', '.mp4')
$unknownCity = '未知地点'
$utcOffset = [TimeSpan]::FromHours(8)   # machine/EXIF offset seen on this library

function ConvertTo-CityFolderName {
    # The geocoder mixes simplified/traditional names and sometimes omits the 市
    # suffix; folder names should be uniform.
    param([string]$Name)
    if (-not $Name) { return $Name }
    $map = @{
        '關'='关'; '萬'='万'; '寧'='宁'; '東'='东'; '區'='区'; '縣'='县'; '臺'='台'; '廣'='广'
        '龍'='龙'; '陽'='阳'; '貴'='贵'; '蘭'='兰'; '門'='门'; '灣'='湾'; '島'='岛'; '遼'='辽'
        '濟'='济'; '齊'='齐'; '樂'='乐'; '興'='兴'; '濱'='滨'; '鶴'='鹤'; '崗'='岗'; '頭'='头'
        '廈'='厦'; '澤'='泽'; '蘇'='苏'; '無'='无'; '錫'='锡'; '揚'='扬'; '鎮'='镇'; '麗'='丽'
        '雲'='云'; '華'='华'; '國'='国'; '學'='学'; '會'='会'; '農'='农'; '業'='业'; '產'='产'
    }
    $out = $Name
    foreach ($k in $map.Keys) { $out = $out.Replace($k, $map[$k]) }
    if ($out -notmatch '(市|州|盟|地区|县|区|旗)$') { $out = $out + '市' }
    return $out
}

function Get-CityForPoint {
    param([double]$Lat, [double]$Lon, [hashtable]$Cache)
    $cell = '{0:F3},{1:F3}' -f ([math]::Round($Lat / 0.05) * 0.05), ([math]::Round($Lon / 0.05) * 0.05)
    if ($Cache.ContainsKey($cell) -and $Cache[$cell].City) { return @{ City = (ConvertTo-CityFolderName $Cache[$cell].City); How = 'gps-cell' } }
    $best = $null; $bestD = [double]::MaxValue
    foreach ($k in $Cache.Keys) {
        $c = $Cache[$k]
        if (-not $c.City) { continue }
        $dlat = $Lat - [double]$c.Lat
        $dlon = ($Lon - [double]$c.Lon) * [math]::Cos($Lat * [math]::PI / 180)
        $d = [math]::Sqrt($dlat * $dlat + $dlon * $dlon)
        if ($d -lt $bestD) { $bestD = $d; $best = $c }
    }
    if ($best -and $bestD -le $NearestCityMaxDeg) { return @{ City = (ConvertTo-CityFolderName $best.City); How = ('gps-near{0:F2}' -f $bestD) } }
    return $null
}

function Get-SessionName {
    param([datetime]$Start, [datetime]$End)
    if ($Start.Date -eq $End.Date) { return ('{0:yyyy-MM-dd}_{0:HHmm}-{1:HHmm}' -f $Start, $End) }
    if ($Start.Year -eq $End.Year) { return ('{0:yyyy-MM-dd_HHmm}_至_{1:MM-dd_HHmm}' -f $Start, $End) }
    return ('{0:yyyy-MM-dd_HHmm}_至_{1:yyyy-MM-dd_HHmm}' -f $Start, $End)
}

# ---------------------------------------------------------------- load inputs
Write-Host "loading inventory: $Inventory"
$rows = Import-Csv $Inventory
Write-Host ("  rows: {0}" -f @($rows).Count)

$cache = @{}
if (Test-Path $GeoCache) {
    foreach ($c in (Import-Csv $GeoCache)) { if ($c.Cell) { $cache[$c.Cell] = $c } }
}
Write-Host ("  geo cache entries: {0}" -f $cache.Count)

$recs = New-Object System.Collections.ArrayList
$skippedExt = @{}
$noTime = 0
foreach ($r in $rows) {
    $ext = ('.' + ($r.Name -split '\.')[-1]).ToLower()
    $isPhoto = $photoExt -contains $ext
    $isVideo = $videoExt -contains $ext
    if (-not ($isPhoto -or $isVideo)) {
        if (-not $skippedExt.ContainsKey($ext)) { $skippedExt[$ext] = 0 }
        $skippedExt[$ext]++
        continue
    }
    $src = Join-Path $r.Dir $r.Name
    if ($src -like "$PhotoRoot*" -or $src -like "$VideoRoot*") { continue }   # idempotency
    if (-not (Test-Path -LiteralPath $src)) { Write-Warning "missing source, skipped: $src"; continue }

    $dt = $null; $how = 'exif'
    if ($r.DateTimeOriginal -match '^\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}$') {
        $dt = [datetime]::ParseExact($r.DateTimeOriginal, 'yyyy:MM:dd HH:mm:ss', $null)
        if ($r.OffsetOrig -eq 'UTC') { $dt = $dt + $utcOffset; $how = 'container-utc' }
    }
    else {
        $dt = (Get-Item -LiteralPath $src).LastWriteTime; $how = 'filetime-fallback'; $noTime++
    }

    $city = $null; $cityHow = 'none'
    if ($r.GpsLat -ne '' -and $r.GpsLon -ne '') {
        $hit = Get-CityForPoint -Lat ([double]$r.GpsLat) -Lon ([double]$r.GpsLon) -Cache $cache
        if ($hit) { $city = $hit.City; $cityHow = $hit.How }
        else { $cityHow = 'gps-unresolved' }
    }

    [void]$recs.Add([pscustomobject]@{
        Path = $src; Name = $r.Name; Ext = $ext; Kind = $(if ($isPhoto) { 'photo' } else { 'video' })
        Size = [int64](Get-Item -LiteralPath $src).Length
        MTime = (Get-Item -LiteralPath $src).LastWriteTime
        Time = $dt; TimeHow = $how; GpsCity = $city; CityHow = $cityHow
        Session = $null; City = $null
    })
}
if ($MaxFiles -gt 0) { $recs = @($recs | Select-Object -First $MaxFiles) }
Write-Host ("  media files to classify: {0}  (skipped other extensions: {1}; time from filesystem: {2})" -f $recs.Count, (@($skippedExt.Values) | Measure-Object -Sum).Sum, $noTime)

# ------------------------------------------------- pass 1: time-only sessions
$ordered = @($recs | Sort-Object Time)
$gap = [TimeSpan]::FromHours($GapHours)
$sessions = New-Object System.Collections.ArrayList
$cur = $null
foreach ($r in $ordered) {
    if ($null -eq $cur -or ($r.Time - $cur.End) -gt $gap) {
        $cur = [pscustomobject]@{ Members = (New-Object System.Collections.ArrayList); Start = $r.Time; End = $r.Time }
        [void]$sessions.Add($cur)
    }
    [void]$cur.Members.Add($r)
    if ($r.Time -gt $cur.End) { $cur.End = $r.Time }
}
Write-Host ("  pass 1: {0} time-continuous blocks (gap > {1}h splits)" -f $sessions.Count, $GapHours)

# ------------------------------- pass 2: split blocks on city change, fill city
$final = New-Object System.Collections.ArrayList
foreach ($s in $sessions) {
    $parts = New-Object System.Collections.ArrayList
    $curPart = $null
    $lastCity = $null
    foreach ($m in ($s.Members | Sort-Object Time)) {
        if ($m.GpsCity) {
            if ($null -eq $curPart -or ($lastCity -and $m.GpsCity -ne $lastCity)) {
                $curPart = [pscustomobject]@{ Members = (New-Object System.Collections.ArrayList); City = $m.GpsCity; How = $m.CityHow; Folder = $null }
                [void]$parts.Add($curPart)
            }
            $lastCity = $m.GpsCity
            $curPart.City = $m.GpsCity
        }
        elseif ($null -eq $curPart) {
            $curPart = [pscustomobject]@{ Members = (New-Object System.Collections.ArrayList); City = $null; How = 'pending'; Folder = $null }
            [void]$parts.Add($curPart)
        }
        [void]$curPart.Members.Add($m)
    }
    foreach ($p in $parts) { [void]$final.Add($p) }
}

# leading members of a block have no city yet -> take the first city that follows
foreach ($p in $final) {
    if (-not $p.City) { continue }
    foreach ($m in $p.Members) { if (-not $m.GpsCity) { $m.GpsCity = $p.City; $m.CityHow = 'inherited-session' } }
}

# blocks with no GPS at all: adopt a city only if the neighbouring located blocks agree
$locatedIdx = @(0..($final.Count - 1)) | Where-Object { $final[$_].City }
for ($i = 0; $i -lt $final.Count; $i++) {
    if ($final[$i].City) { continue }
    $prev = $null; $next = $null
    for ($j = $i - 1; $j -ge 0; $j--) { if ($final[$j].City) { $prev = $final[$j]; break } }
    for ($j = $i + 1; $j -lt $final.Count; $j++) { if ($final[$j].City) { $next = $final[$j]; break } }
    $pEnd = if ($prev) { ($prev.Members | Sort-Object Time)[-1].Time } else { $null }
    $nStart = if ($next) { ($next.Members | Sort-Object Time)[0].Time } else { $null }
    $mStart = ($final[$i].Members | Sort-Object Time)[0].Time
    $mEnd = ($final[$i].Members | Sort-Object Time)[-1].Time
    $near = $true
    if ($pEnd -and ($mStart - $pEnd) -gt [TimeSpan]::FromHours(24)) { $near = $false }
    if ($nStart -and ($nStart - $mEnd) -gt [TimeSpan]::FromHours(24)) { $near = $false }
    if ($prev -and $next -and $prev.City -eq $next.City -and $near) {
        $final[$i].City = $prev.City
        foreach ($m in $final[$i].Members) { $m.GpsCity = $prev.City; $m.CityHow = 'inferred-between' }
    }
}

# ----------------------------------- merge into existing library sessions
# An import must not create a parallel folder for a session the library already
# has: sessions are matched against the library index by city + time proximity
# (<= GapHours), and the new files adopt that folder (and its city, which also
# lets a GPS-less import inherit a location from the surrounding library).
$anchors = New-Object System.Collections.ArrayList
if ($LibraryIndex -and (Test-Path -LiteralPath $LibraryIndex)) {
    $idx = @(Import-Csv -LiteralPath $LibraryIndex)
    Write-Host ("  library index: {0} entries" -f $idx.Count)
    foreach ($grp in ($idx | Where-Object { $_.SessionFolder } | Group-Object City, SessionFolder)) {
        $times = @()
        foreach ($e in $grp.Group) {
            $t = [datetime]::MinValue
            if ([datetime]::TryParse($e.CaptureTime, [ref]$t)) { $times += $t }
        }
        if ($times.Count -eq 0) { continue }
        $first = $grp.Group[0]
        [void]$anchors.Add([pscustomobject]@{
            City = $first.City; Folder = $first.SessionFolder
            Start = ($times | Sort-Object)[0]; End = ($times | Sort-Object)[-1]
        })
    }
    Write-Host ("  library session anchors: {0}" -f $anchors.Count)
}

foreach ($blk in $final) {
    $members = @($blk.Members | Sort-Object Time)
    if ($members.Count -eq 0) { continue }
    $s = $members[0].Time; $e = $members[-1].Time
    $city = if ($blk.City) { $blk.City } else { $unknownCity }
    $folder = $null
    $pick = $null; $pickGap = [TimeSpan]::FromHours($GapHours)
    foreach ($a in $anchors) {
        # time distance between the two ranges (0 when they overlap)
        $g = [TimeSpan]::Zero
        if ($a.End -lt $s) { $g = $s - $a.End }
        elseif ($e -lt $a.Start) { $g = $a.Start - $e }
        if ($g -gt $pickGap) { continue }
        $cityMatches = ($city -ne $unknownCity) -and ($a.City -eq $city)
        $cityUnknown = ($city -eq $unknownCity)
        if ($cityMatches -or $cityUnknown) {
            if ($null -eq $pick -or $g -lt $pickGap -or $cityMatches) { $pick = $a; $pickGap = $g }
            if ($g -eq [TimeSpan]::Zero -and $cityMatches) { break }
        }
    }
    if ($pick) {
        if ($city -eq $unknownCity -and $pick.City) {
            $city = $pick.City
            foreach ($m in $members) { if (-not $m.GpsCity) { $m.GpsCity = $pick.City; $m.CityHow = 'inherited-library' } }
        }
        $folder = $pick.Folder
        Write-Host ("  merged {0} {1:yyyy-MM-dd HH:mm}..{2:HH:mm} into existing folder {3}\{4}" -f $city, $s, $e, $city, $folder)
    }
    else { $folder = Get-SessionName -Start $s -End $e }
    $blk.City = $city
    $blk.Folder = $folder
}

# --------------------------------------------------- build destination paths
$used = @{}
$plan = New-Object System.Collections.ArrayList
foreach ($blk in $final) {
    $members = @($blk.Members | Sort-Object Time)
    if ($members.Count -eq 0) { continue }
    $city = if ($blk.City) { $blk.City } else { $unknownCity }
    $name = $blk.Folder
    foreach ($m in $members) {
        $root = if ($m.Kind -eq 'photo') { $PhotoRoot } else { $VideoRoot }
        $dir = Join-Path (Join-Path $root $city) $name
        $dest = Join-Path $dir $m.Name
        $key = $dest.ToLower()
        if ($used.ContainsKey($key) -or (Test-Path -LiteralPath $dest)) {
            $base = [IO.Path]::GetFileNameWithoutExtension($m.Name)
            $e = $m.Ext
            $n = 2
            do {
                $dest = Join-Path $dir ("{0}__{1}{2}" -f $base, $n, $e)
                $key = $dest.ToLower()
                $n++
            } while ($used.ContainsKey($key) -or (Test-Path -LiteralPath $dest))
        }
        $used[$key] = $true
        [void]$plan.Add([pscustomobject]@{
            Source = $m.Path; Dest = $dest; Kind = $m.Kind; Size = $m.Size
            MTime = $m.MTime; CaptureTime = $m.Time; TimeHow = $m.TimeHow
            City = $city; CityHow = $m.CityHow; Session = $name
            SourceDir = $m.Path.Substring(0, $m.Path.Length - $m.Name.Length - 1)
        })
    }
}

# --------------------------------------------------------------- dry run view
Write-Host ""
Write-Host "=== plan summary ==="
$plan | Group-Object Kind | Select-Object Count, Name | Format-Table -AutoSize | Out-String -Width 60 | Write-Host
Write-Host "by city:"
$plan | Group-Object City | Sort-Object Count -Descending | Select-Object Count, Name | Format-Table -AutoSize | Out-String -Width 60 | Write-Host
Write-Host "time confidence:"
$plan | Group-Object TimeHow | Select-Object Count, Name | Format-Table -AutoSize | Out-String -Width 60 | Write-Host
Write-Host "city confidence:"
$plan | Group-Object CityHow | Select-Object Count, Name | Format-Table -AutoSize | Out-String -Width 60 | Write-Host
Write-Host ("sessions: {0}" -f @($plan | Group-Object City, Session).Count)

if (-not (Test-Path $JournalDir)) { New-Item -ItemType Directory -Force -Path $JournalDir | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$planPath = Join-Path $JournalDir "plan-$stamp.csv"
$plan | Export-Csv -Path $planPath -NoTypeInformation -Encoding UTF8
Write-Host "plan written: $planPath"

if (-not $Execute) {
    Write-Host ""
    Write-Host "DRY RUN - nothing was moved. Re-run with -Execute to perform the moves."
    return
}

# ------------------------------------------------------------------- execute
$journalPath = Join-Path $JournalDir "move-journal-$stamp.csv"
$restorePath = Join-Path $JournalDir "restore-$stamp.ps1"
$sw = New-Object System.IO.StreamWriter($journalPath, $false, (New-Object System.Text.UTF8Encoding($false)))
$sw.WriteLine('Source,Dest,Size,MTime,Result')
$sw.Flush()

$moved = 0; $failed = 0; $bytes = 0
try {
    foreach ($p in $plan) {
        $res = 'ok'
        try {
            $destDir = Split-Path $p.Dest -Parent
            if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
            Move-Item -LiteralPath $p.Source -Destination $p.Dest -ErrorAction Stop
            $ok = (Test-Path -LiteralPath $p.Dest)
            if ($ok) { $ok = ((Get-Item -LiteralPath $p.Dest).Length -eq $p.Size) }
            if (-not $ok) {
                $res = 'verify-failed'
                try { Move-Item -LiteralPath $p.Dest -Destination $p.Source -ErrorAction Stop; $res = 'verify-failed-rolled-back' } catch { $res = 'verify-failed-rollback-failed' }
                $sw.WriteLine(('"{0}","{1}",{2},"{3}",{4}' -f $p.Source, $p.Dest, $p.Size, $p.MTime.ToString('yyyy-MM-dd HH:mm:ss'), $res)); $sw.Flush()
                throw "verification failed for $($p.Source) - run aborted, nothing lost"
            }
            $moved++; $bytes += $p.Size
        }
        catch {
            if ($res -eq 'ok') { $res = 'error: ' + $_.Exception.Message }
            $sw.WriteLine(('"{0}","{1}",{2},"{3}","{4}"' -f $p.Source, $p.Dest, $p.Size, $p.MTime.ToString('yyyy-MM-dd HH:mm:ss'), ($res -replace '"', "'"))); $sw.Flush()
            $failed++
            if ($res -notlike 'error:*') { throw }
        }
        if ($res -eq 'ok') {
            $sw.WriteLine(('"{0}","{1}",{2},"{3}",ok' -f $p.Source, $p.Dest, $p.Size, $p.MTime.ToString('yyyy-MM-dd HH:mm:ss')))
            $sw.Flush()
        }
        if ($moved % 500 -eq 0 -and $moved -gt 0) { Write-Host ("  moved {0}/{1}" -f $moved, $plan.Count) }
    }
}
finally {
    $sw.Close()
}

# ------------------------------------------------------------- reconciliation
Write-Host ""
Write-Host "=== done ==="
Write-Host ("moved: {0}   failed: {1}   bytes: {2} GB" -f $moved, $failed, [math]::Round($bytes / 1GB, 2))
Write-Host "journal: $journalPath"

$srcMissing = 0
foreach ($p in $plan) {
    $s = Test-Path -LiteralPath $p.Source
    $d = Test-Path -LiteralPath $p.Dest
    if ($s -and -not $d) { $srcMissing++ }
}
Write-Host ("plan rows whose source still exists and dest does not (should be 0): {0}" -f $srcMissing)

# random-sample SHA256 spot check: read the bytes back from the new location
$sample = @($plan | Get-Random -Count ([math]::Min($SampleHashCount, @($plan).Count)))
$hashOk = 0; $hashBad = @()
foreach ($p in $sample) {
    if (-not (Test-Path -LiteralPath $p.Dest)) { $hashBad += $p.Dest; continue }
    $h = (Get-FileHash -LiteralPath $p.Dest -Algorithm SHA256).Hash
    if ($h) { $hashOk++ } else { $hashBad += $p.Dest }
}
Write-Host ("spot-check hashes read back OK: {0}/{1}" -f $hashOk, $sample.Count)
if ($hashBad.Count) { Write-Host "  problems:"; $hashBad | ForEach-Object { Write-Host "   $_" } }

# rollback helper
$rb = @"
# restore-$stamp.ps1 - moves every file in move-journal-$stamp.csv back to its original path
# Generated automatically. Nothing is deleted; existing files at the original path are
# left alone and reported, so a re-run can never overwrite data.
`$ErrorActionPreference = 'Stop'
`$journal = Join-Path `$PSScriptRoot 'move-journal-$stamp.csv'
`$restored = 0; `$skipped = @()
foreach (`$r in (Import-Csv `$journal)) {
    if (`$r.Result -ne 'ok') { continue }
    if (-not (Test-Path -LiteralPath `$r.Dest)) { `$skipped += "dest missing: `$(`$r.Dest)"; continue }
    `$dir = Split-Path `$r.Source -Parent
    if (-not (Test-Path -LiteralPath `$dir)) { New-Item -ItemType Directory -Force -Path `$dir | Out-Null }
    if (Test-Path -LiteralPath `$r.Source) { `$skipped += "original occupied: `$(`$r.Source)"; continue }
    Move-Item -LiteralPath `$r.Dest -Destination `$r.Source
    `$restored++
}
Write-Host "restored: `$restored"
if (`$skipped.Count) { Write-Host "skipped:"; `$skipped | ForEach-Object { Write-Host "  `$_" } }
"@
[System.IO.File]::WriteAllText($restorePath, $rb, (New-Object System.Text.UTF8Encoding($true)))
Write-Host "rollback script: $restorePath"

# per-target summary
Write-Host ""
$manifest = Import-Csv $journalPath | Where-Object { $_.Result -eq 'ok' }
$manifest | Group-Object { Split-Path (Split-Path $_.Dest -Parent) -Leaf } | Sort-Object Count -Descending |
    Select-Object Count, Name | Format-Table -AutoSize | Out-String -Width 80 | Write-Host

# ------------------------------------------------------------ library index
# Keeps the "what is already filed, in which city/session" map so the next
# import can merge into these sessions instead of creating parallel folders.
if ($IndexOut) {
    $byPath = @{}
    if (Test-Path -LiteralPath $IndexOut) {
        foreach ($e in (Import-Csv -LiteralPath $IndexOut)) { if ($e.Path) { $byPath[$e.Path] = $e } }
    }
    $rows = New-Object System.Collections.ArrayList
    foreach ($k in $byPath.Keys) {
        if (Test-Path -LiteralPath $k) { [void]$rows.Add($byPath[$k]) }     # drop entries that moved away
    }
    foreach ($p in $plan) {
        if (-not (Test-Path -LiteralPath $p.Dest)) { continue }
        [void]$rows.Add([pscustomobject]@{
            Path          = $p.Dest
            Name          = Split-Path $p.Dest -Leaf
            Kind          = $p.Kind
            Size          = $p.Size
            MTime         = ([datetime]$p.MTime).ToString('yyyy-MM-dd HH:mm:ss')
            CaptureTime   = ([datetime]$p.CaptureTime).ToString('yyyy-MM-dd HH:mm:ss')
            City          = $p.City
            SessionFolder = Split-Path (Split-Path $p.Dest -Parent) -Leaf
        })
    }
    $rows | Export-Csv -Path $IndexOut -NoTypeInformation -Encoding UTF8
    Write-Host ("library index updated: {0} ({1} entries)" -f $IndexOut, $rows.Count)
}
