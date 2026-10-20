# read-exif.ps1 - dependency-free EXIF reader (JPEG APP1 + TIFF/NEF RAW)
# Usage:
#   powershell -ExecutionPolicy Bypass -File read-exif.ps1 -Path <file-or-dir> [-Recurse] [-AsObject]
#
# Purpose: decide whether an image carries a real capture time (EXIF
# DateTimeOriginal + SubSec + UTC offset) or only filesystem timestamps.
#
# NOTE: PowerShell's -shl keeps the left operand's type, so [byte]255 -shl 8
# collapses to 0. All shifts below cast to [int] first - that bug silently made
# every file look EXIF-less on the first pass.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string[]]$Path,
    [switch]$Recurse,
    [switch]$AsObject
)

$script:TagNames = @{
    0x010F = 'Make'
    0x0110 = 'Model'
    0x0131 = 'Software'
    0x0132 = 'DateTime'                 # IFD0 tag: last file-change time
    0x013B = 'Artist'
    0x014A = 'SubIFDs'
    0x829A = 'ExposureTime'
    0x829D = 'FNumber'
    0x8769 = 'ExifIFDPointer'
    0x8825 = 'GPSIFDPointer'
    0x8827 = 'ISOSpeedRatings'
    0x9003 = 'DateTimeOriginal'         # the real shooting time
    0x9004 = 'DateTimeDigitized'
    0x9010 = 'OffsetTime'
    0x9011 = 'OffsetTimeOriginal'       # UTC offset of the shooting time
    0x9012 = 'OffsetTimeDigitized'
    0x920A = 'FocalLength'
    0x9290 = 'SubSecTime'
    0x9291 = 'SubSecTimeOriginal'
    0x9292 = 'SubSecTimeDigitized'
    0xA002 = 'PixelXDimension'
    0xA003 = 'PixelYDimension'
    0x9296 = 'DateTimeOriginalLocal'    # some vendors
}

function New-ExifReader {
    param([byte[]]$bytes, [int]$tiff)

    $ctx = @{ bytes = $bytes; tiff = $tiff; len = $bytes.Length; little = ($bytes[$tiff] -eq 0x49) }
    $ctx.u16 = {
        param($o)
        $b = $ctx.bytes
        if ($ctx.little) { return ([int]$b[$o]) -bor (([int]$b[$o + 1]) -shl 8) }
        return (([int]$b[$o]) -shl 8) -bor ([int]$b[$o + 1])
    }
    $ctx.u32 = {
        param($o)
        $b = $ctx.bytes
        if ($ctx.little) {
            return ([uint32]$b[$o]) -bor (([uint32]$b[$o + 1]) -shl 8) -bor (([uint32]$b[$o + 2]) -shl 16) -bor (([uint32]$b[$o + 3]) -shl 24)
        }
        return (([uint32]$b[$o]) -shl 24) -bor (([uint32]$b[$o + 1]) -shl 16) -bor (([uint32]$b[$o + 2]) -shl 8) -bor ([uint32]$b[$o + 3])
    }
    return $ctx
}

function Read-ExifIfd {
    param([hashtable]$ctx, [int]$dirOffset, [hashtable]$into, [int]$depth = 0)

    if ($depth -gt 3) { return @{} }
    if ($dirOffset -lt 0 -or ($dirOffset + 2) -gt $ctx.len) { return @{} }

    $count = & $ctx.u16 $dirOffset
    $ptrs = @{}
    for ($i = 0; $i -lt $count; $i++) {
        $entry = $dirOffset + 2 + ($i * 12)
        if (($entry + 12) -gt $ctx.len) { break }
        $tag = & $ctx.u16 $entry
        $type = & $ctx.u16 ($entry + 2)
        $cnt = & $ctx.u32 ($entry + 4)
        $valOff = $entry + 8

        if ($tag -eq 0x8769 -or $tag -eq 0x8825 -or $tag -eq 0x014A) {
            $ptrs[$tag] = & $ctx.u32 $valOff
            continue
        }

        $sizes = @{ 1 = 1; 2 = 1; 3 = 2; 4 = 4; 5 = 8; 7 = 1; 9 = 4; 10 = 8 }
        if (-not $sizes.ContainsKey($type)) { continue }
        $size = $sizes[$type] * [int]$cnt
        $data = $valOff
        if ($size -gt 4) { $data = $ctx.tiff + [int](& $ctx.u32 $valOff) }
        if ($data -lt 0 -or ($data + $size) -gt $ctx.len) { continue }

        $name = if ($script:TagNames.ContainsKey($tag)) { $script:TagNames[$tag] } else { 'Tag0x{0:X4}' -f $tag }
        if ($into.ContainsKey($name)) { continue }

        $b = $ctx.bytes
        $val = switch ($type) {
            1 { $b[$data] }
            2 {
                $s = ''
                for ($k = 0; $k -lt $cnt; $k++) {
                    $c = $b[$data + $k]
                    if ($c -eq 0) { break }
                    $s += [char]$c
                }
                $s.Trim()
            }
            3 { & $ctx.u16 $data }
            4 { & $ctx.u32 $data }
            5 {
                $num = & $ctx.u32 $data
                $den = & $ctx.u32 ($data + 4)
                if ($den -eq 0) { "$num/0" } else { [math]::Round($num / $den, 4) }
            }
            9 { ConvertFrom-U32 ([uint32](& $ctx.u32 $data)) }
            10 {
                $num = ConvertFrom-U32 ([uint32](& $ctx.u32 $data))
                $den = ConvertFrom-U32 ([uint32](& $ctx.u32 ($data + 4)))
                if ($den -eq 0) { "$num/0" } else { [math]::Round($num / $den, 4) }
            }
            default { $null }
        }
        if ($null -ne $val) { $into[$name] = $val }
    }

    if ($ptrs.ContainsKey(0x8769)) { Read-ExifIfd $ctx ($ctx.tiff + [int]$ptrs[0x8769]) $into ($depth + 1) | Out-Null }
    if ($ptrs.ContainsKey(0x8825)) { Read-GpsIfd $ctx ($ctx.tiff + [int]$ptrs[0x8825]) $into }
    if ($ptrs.ContainsKey(0x014A)) {
        # SubIFDs: array of offsets; the first usually holds the full-res IFD
        $subOff = $ctx.tiff + [int]$ptrs[0x014A]
        $first = & $ctx.u32 $subOff
        if ($first -gt 0) { Read-ExifIfd $ctx ($ctx.tiff + [int]$first) $into ($depth + 1) | Out-Null }
    }
    return @{}
}

function Read-GpsIfd {
    # GPS IFD uses its own tag numbering, so it gets its own reader.
    param([hashtable]$ctx, [int]$dirOffset, [hashtable]$into)

    if ($dirOffset -lt 0 -or ($dirOffset + 2) -gt $ctx.len) { return }
    $count = & $ctx.u16 $dirOffset
    $raw = @{}
    for ($i = 0; $i -lt $count; $i++) {
        $entry = $dirOffset + 2 + ($i * 12)
        if (($entry + 12) -gt $ctx.len) { break }
        $tag = & $ctx.u16 $entry
        $type = & $ctx.u16 ($entry + 2)
        $cnt = [int](& $ctx.u32 ($entry + 4))
        $valOff = $entry + 8
        $sizes = @{ 1 = 1; 2 = 1; 3 = 2; 4 = 4; 5 = 8; 7 = 1; 9 = 4; 10 = 8 }
        if (-not $sizes.ContainsKey($type)) { continue }
        $size = $sizes[$type] * $cnt
        $data = $valOff
        if ($size -gt 4) { $data = $ctx.tiff + [int](& $ctx.u32 $valOff) }
        if ($data -lt 0 -or ($data + $size) -gt $ctx.len) { continue }

        $b = $ctx.bytes
        switch ($type) {
            2 {
                $s = ''
                for ($k = 0; $k -lt $cnt; $k++) { $c = $b[$data + $k]; if ($c -eq 0) { break }; $s += [char]$c }
                $raw[$tag] = $s.Trim()
            }
            5 {
                $vals = @()
                for ($k = 0; $k -lt $cnt; $k++) {
                    $num = & $ctx.u32 ($data + ($k * 8))
                    $den = & $ctx.u32 ($data + ($k * 8) + 4)
                    $vals += $(if ($den -eq 0) { 0.0 } else { [double]$num / [double]$den })
                }
                $raw[$tag] = $vals
            }
            1 { $raw[$tag] = $b[$data] }
            3 { $raw[$tag] = & $ctx.u16 $data }
            4 { $raw[$tag] = & $ctx.u32 $data }
        }
    }

    # tag 1/2 = latitude ref/value, 3/4 = longitude, 5/6 = altitude, 7 = UTC time, 29 = date
    if ($raw.ContainsKey(2) -and $raw[2].Count -ge 3) {
        $d = [double]$raw[2][0] + ([double]$raw[2][1] / 60.0) + ([double]$raw[2][2] / 3600.0)
        if ($raw[1] -eq 'S') { $d = -$d }
        $into['GpsLat'] = [math]::Round($d, 6)
        $into['GpsLatRef'] = [string]$raw[1]
    }
    if ($raw.ContainsKey(4) -and $raw[4].Count -ge 3) {
        $d = [double]$raw[4][0] + ([double]$raw[4][1] / 60.0) + ([double]$raw[4][2] / 3600.0)
        if ($raw[3] -eq 'W') { $d = -$d }
        $into['GpsLon'] = [math]::Round($d, 6)
        $into['GpsLonRef'] = [string]$raw[3]
    }
    if ($raw.ContainsKey(6) -and $raw[6].Count -ge 1) {
        $alt = [double]$raw[6][0]
        if ($raw.ContainsKey(5) -and $raw[5] -eq 1) { $alt = -$alt }
        $into['GpsAltitude'] = [math]::Round($alt, 1)
    }
    if ($raw.ContainsKey(29)) { $into['GpsDateStamp'] = [string]$raw[29] }
}

function Get-Mp4CreationTime {
    # QuickTime/MP4 'mvhd' creation_time: seconds since 1904-01-01 UTC.
    # moov can sit at the end of the file, so stream-search the whole file.
    param([string]$File)

    $pattern = [byte[]](0x6D, 0x76, 0x68, 0x64)   # 'mvhd'
    try {
        $fs = [System.IO.File]::OpenRead($File)
        try {
            $chunk = 1048576
            $buf = New-Object byte[] ($chunk + 3)
            $carry = 0
            while ($true) {
                $read = $fs.Read($buf, $carry, $chunk)
                if ($read -le 0) { break }
                $total = $carry + $read
                for ($i = 0; $i -le ($total - 4); $i++) {
                    if ($buf[$i] -eq 0x6D -and $buf[$i + 1] -eq 0x76 -and $buf[$i + 2] -eq 0x68 -and $buf[$i + 3] -eq 0x64) {
                        $base = $fs.Position - $total + $i + 4   # byte right after 'mvhd'
                        $head = New-Object byte[] 12
                        $save = $fs.Position
                        $fs.Position = $base
                        $got = $fs.Read($head, 0, 12)
                        $fs.Position = $save
                        if ($got -lt 12) { return $null }
                        $ver = $head[0]
                        if ($ver -eq 1) {
                            $secs = 0L
                            for ($k = 4; $k -lt 12; $k++) { $secs = ($secs -shl 8) -bor [int64]$head[$k] }
                        }
                        else {
                            $secs = 0L
                            for ($k = 4; $k -lt 8; $k++) { $secs = ($secs -shl 8) -bor [int64]$head[$k] }
                        }
                        if ($secs -le 0) { return $null }
                        return [datetime]::new(1904, 1, 1, 0, 0, 0, [DateTimeKind]::Utc).AddSeconds($secs)
                    }
                }
                for ($k = 0; $k -lt 3; $k++) { $buf[$k] = $buf[$total - 3 + $k] }
                $carry = 3
            }
        }
        finally { $fs.Dispose() }
    }
    catch { return $null }
    return $null
}

function ConvertFrom-U32 {
    param([uint32]$Value)
    if ($Value -gt [int]::MaxValue) { return [int64]$Value - 4294967296 }
    return [int64]$Value
}

function Get-XmpCreateTime {
    # XMP is a second, independent date source (survives some EXIF strip/export
    # paths). Returns @{ DateTime='yyyy:MM:dd HH:mm:ss'; SubSec='..' } or $null.
    param([byte[]]$Bytes)

    try { $text = [System.Text.Encoding]::UTF8.GetString($Bytes) } catch { return $null }
    $patterns = @(
        'xmp:CreateDate\s*=\s*"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+)',
        '<xmp:CreateDate>\s*([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+)',
        'photoshop:DateCreated\s*=\s*"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+)',
        '<photoshop:DateCreated>\s*([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+)'
    )
    foreach ($p in $patterns) {
        $m = [regex]::Match($text, $p)
        if ($m.Success) {
            $raw = $m.Groups[1].Value                    # e.g. 2025-11-01T13:30:04.34
            $parts = $raw -split 'T'
            $datePart = $parts[0] -replace '-', ':'
            $timePart = $parts[1]
            $sec = $timePart
            $subSec = ''
            if ($timePart.Contains('.')) {
                $bits = $timePart.Split('.')
                $sec = $bits[0]
                $subSec = $bits[1]
            }
            return @{ DateTime = "$datePart $sec"; SubSec = $subSec }
        }
    }
    return $null
}

function Get-ExifTags {
    param([string]$File)

    # EXIF lives in the first APP1/IFD block; cap the read so scanning RAW
    # libraries does not pull tens of GB through disk.
    $maxBytes = 1048576
    try {
        $fs = [System.IO.File]::OpenRead($File)
        try {
            $take = [int][Math]::Min([int64]$fs.Length, [int64]$maxBytes)
            if ($take -lt 16) { return $null }
            $bytes = New-Object byte[] $take
            $read = 0
            while ($read -lt $take) {
                $n = $fs.Read($bytes, $read, $take - $read)
                if ($n -le 0) { break }
                $read += $n
            }
            if ($read -lt $take) { $bytes = $bytes[0..($read - 1)] }
        }
        finally { $fs.Dispose() }
    }
    catch { return $null }
    if ($bytes.Length -lt 16) { return $null }

    $tags = @{}
    $tiff = $null

    if ($bytes[0] -eq 0xFF -and $bytes[1] -eq 0xD8) {
        # JPEG: walk segments to APP1/Exif
        $pos = 2
        while ($pos -lt ($bytes.Length - 4)) {
            if ($bytes[$pos] -ne 0xFF) { $pos++; continue }
            $marker = $bytes[$pos + 1]
            if ($marker -eq 0xD8 -or $marker -eq 0x01 -or ($marker -ge 0xD0 -and $marker -le 0xD7)) { $pos += 2; continue }
            if ($marker -eq 0xDA -or $marker -eq 0xD9) { break }
            $segLen = (([int]$bytes[$pos + 2]) -shl 8) -bor ([int]$bytes[$pos + 3])
            if ($segLen -lt 2) { break }
            if ($marker -eq 0xE1) {
                $seg = $pos + 4
                if ($bytes[$seg] -eq 0x45 -and $bytes[$seg + 1] -eq 0x78 -and $bytes[$seg + 2] -eq 0x69 -and $bytes[$seg + 3] -eq 0x66) {
                    $tiff = $seg + 6
                    break
                }
            }
            $pos += 2 + $segLen
        }
    }
    elseif (($bytes[0] -eq 0x49 -and $bytes[1] -eq 0x49 -and $bytes[2] -eq 0x2A) -or
            ($bytes[0] -eq 0x4D -and $bytes[1] -eq 0x4D -and $bytes[2] -eq 0x00 -and $bytes[3] -eq 0x2A)) {
        # TIFF container (NEF/CR2/ARW/DNG/TIF)
        $tiff = 0
    }
    elseif ($bytes[0] -eq 0x89 -and $bytes[1] -eq 0x50 -and $bytes[2] -eq 0x4E -and $bytes[3] -eq 0x47) {
        # PNG: metadata lives in chunks (eXIf carries a raw TIFF block, tEXt/iTXt text)
        $pos = 8
        while (($pos + 8) -le $bytes.Length) {
            $clen = (([uint32]$bytes[$pos]) -shl 24) -bor (([uint32]$bytes[$pos + 1]) -shl 16) -bor (([uint32]$bytes[$pos + 2]) -shl 8) -bor ([uint32]$bytes[$pos + 3])
            if ($clen -gt [int]::MaxValue) { break }
            $ctype = [System.Text.Encoding]::ASCII.GetString($bytes, $pos + 4, 4)
            if ($ctype -eq 'eXIf') { $tiff = $pos + 8; break }
            if ($ctype -eq 'tEXt' -or $ctype -eq 'iTXt') {
                $end = [int][Math]::Min([int64]($pos + 8 + [int]$clen - 1), [int64]($bytes.Length - 1))
                if ($end -ge ($pos + 8)) {
                    $txt = [System.Text.Encoding]::UTF8.GetString($bytes, $pos + 8, $end - ($pos + 8) + 1)
                    if ($txt.Length -gt 0) { $tags['PngText:' + ($txt -split "`0")[0]] = $txt }
                }
            }
            if ($ctype -eq 'IEND') { break }
            $pos += 12 + [int]$clen
        }
    }

    $xmp = Get-XmpCreateTime $bytes
    if ($xmp) {
        $tags['XmpCreateDate'] = $xmp.DateTime
        if ($xmp.SubSec) { $tags['XmpSubSec'] = $xmp.SubSec }
    }

    if ($null -eq $tiff) {
        if ($tags.Count -gt 0) { return $tags }
        return $null
    }

    $ctx = New-ExifReader -bytes $bytes -tiff $tiff
    $ifd0 = & $ctx.u32 ($tiff + 4)
    Read-ExifIfd $ctx ($tiff + [int]$ifd0) $tags | Out-Null
    if ($tags.Count -eq 0) { return $null }
    return $tags
}

$files = New-Object System.Collections.ArrayList
foreach ($p in $Path) {
    $item = Get-Item -LiteralPath $p -ErrorAction SilentlyContinue
    if ($null -eq $item) { Write-Warning "not found: $p"; continue }
    if ($item.PSIsContainer) {
        if ($Recurse) { Get-ChildItem -LiteralPath $item.FullName -Recurse -File | ForEach-Object { [void]$files.Add($_) } }
        else { Get-ChildItem -LiteralPath $item.FullName -File | ForEach-Object { [void]$files.Add($_) } }
    }
    else { [void]$files.Add($item) }
}

$jpegExt = @('.jpg', '.jpeg', '.jpe')
$tiffExt = @('.nef', '.tif', '.tiff', '.dng', '.arw', '.cr2', '.orf', '.rw2', '.raf', '.pef', '.srw')
$pngExt = @('.png')
$videoExt = @('.mp4', '.mov', '.m4v', '.3gp')

$results = foreach ($f in $files) {
    $ext = $f.Extension.ToLower()
    $tags = $null
    $kind = 'unsupported'
    $videoUtc = $null
    if ($jpegExt -contains $ext -or $tiffExt -contains $ext -or $pngExt -contains $ext) {
        $tags = Get-ExifTags $f.FullName
        $src = 'none'
        if ($tags -and $tags.ContainsKey('DateTimeOriginal')) { $src = 'exif' }
        elseif ($tags -and $tags.ContainsKey('XmpCreateDate')) { $src = 'xmp' }
        $kind = "$($ext.TrimStart('.'))-$src"
    }
    elseif ($videoExt -contains $ext) {
        $videoUtc = Get-Mp4CreationTime $f.FullName
        $kind = if ($null -eq $videoUtc) { "$($ext.TrimStart('.'))-no-time" } else { "$($ext.TrimStart('.'))-creation-time-utc" }
    }
    $dto = ''
    $sub = ''
    $off = ''
    if ($videoUtc) {
        $dto = $videoUtc.ToString('yyyy:MM:dd HH:mm:ss')
        $off = 'UTC'
    }
    elseif ($tags -and $tags.ContainsKey('DateTimeOriginal')) {
        $dto = [string]$tags['DateTimeOriginal']
        if ($tags.ContainsKey('SubSecTimeOriginal')) { $sub = [string]$tags['SubSecTimeOriginal'] }
        if ($tags.ContainsKey('OffsetTimeOriginal')) { $off = [string]$tags['OffsetTimeOriginal'] }
    }
    elseif ($tags -and $tags.ContainsKey('XmpCreateDate')) {
        $dto = [string]$tags['XmpCreateDate']
        if ($tags.ContainsKey('XmpSubSec')) { $sub = [string]$tags['XmpSubSec'] }
        $off = 'XMP(local)'
    }
    [pscustomobject]@{
        Name              = $f.Name
        Dir               = $f.DirectoryName
        Kind              = $kind
        ExifCount         = if ($tags) { $tags.Count } else { 0 }
        DateTimeOriginal  = $dto
        ExifDateTime      = if ($tags -and $tags.ContainsKey('DateTimeOriginal')) { [string]$tags['DateTimeOriginal'] } else { '' }
        XmpDateTime       = if ($tags -and $tags.ContainsKey('XmpCreateDate')) { [string]$tags['XmpCreateDate'] } else { '' }
        SubSecTimeOrig    = $sub
        OffsetOrig        = $off
        DateTimeDigitized = if ($tags -and $tags.ContainsKey('DateTimeDigitized')) { [string]$tags['DateTimeDigitized'] } else { '' }
        DateTimeIFD0      = if ($tags -and $tags.ContainsKey('DateTime')) { [string]$tags['DateTime'] } else { '' }
        Make              = if ($tags -and $tags.ContainsKey('Make')) { [string]$tags['Make'] } else { '' }
        Model             = if ($tags -and $tags.ContainsKey('Model')) { [string]$tags['Model'] } else { '' }
        GpsLat            = if ($tags -and $tags.ContainsKey('GpsLat')) { $tags['GpsLat'] } else { '' }
        GpsLon            = if ($tags -and $tags.ContainsKey('GpsLon')) { $tags['GpsLon'] } else { '' }
        GpsAltitude       = if ($tags -and $tags.ContainsKey('GpsAltitude')) { $tags['GpsAltitude'] } else { '' }
        Software          = if ($tags -and $tags.ContainsKey('Software')) { [string]$tags['Software'] } else { '' }
        FileWriteTime     = $f.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
        FileCreateTime    = $f.CreationTime.ToString('yyyy-MM-dd HH:mm:ss')
    }
}

if ($AsObject) { $results } else { $results | Format-Table -AutoSize | Out-String -Width 250 }
