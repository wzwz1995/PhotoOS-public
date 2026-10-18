# resolve-cities.ps1 - reverse-geocode photo GPS clusters to Chinese prefecture-level cities
#
# Reads the inventory CSV produced by read-exif.ps1 (-AsObject), picks representative
# points for the GPS clusters, reverse geocodes them (no API key needed) and caches the
# result, so repeat runs cost zero network calls.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File resolve-cities.ps1 -Inventory <csv>
# Safe by design: writes only the cache CSV, never touches photos.
#
# NOTE: PowerShell variable names are case-insensitive - never name a local $cache
# next to a -Cache parameter.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Inventory,
    [string]$CachePath = '',          # defaults to <script dir>\geo-cache.csv
    [double]$CellDeg = 0.05,
    [double]$MinSepDeg = 0.20,     # keep selected sample points this far apart
    [int]$MaxCalls = 900,
    [int]$DelayMs = 300,
    [switch]$Quiet
)

function Write-Log($msg) { if (-not $Quiet) { Write-Host $msg } }
if (-not $CachePath) { $CachePath = Join-Path $PSScriptRoot 'geo-cache.csv' }

$rows = Import-Csv $Inventory
$gps = @($rows | Where-Object { $_.GpsLat -and $_.GpsLat -ne '' -and $_.GpsLon -and $_.GpsLon -ne '' })
Write-Log ("inventory rows: {0}   rows with GPS: {1}" -f @($rows).Count, $gps.Count)

# ---- cell aggregation -------------------------------------------------------
$cells = @{}
foreach ($r in $gps) {
    $lat = [double]$r.GpsLat; $lon = [double]$r.GpsLon
    if ($lat -eq 0 -and $lon -eq 0) { continue }
    $key = '{0:F3},{1:F3}' -f ([math]::Round($lat / $CellDeg) * $CellDeg), ([math]::Round($lon / $CellDeg) * $CellDeg)
    if (-not $cells.ContainsKey($key)) { $cells[$key] = @{ n = 0; latSum = 0.0; lonSum = 0.0 } }
    $cells[$key].n++
    $cells[$key].latSum += $lat
    $cells[$key].lonSum += $lon
}
Write-Log ("distinct {0}deg cells: {1}" -f $CellDeg, $cells.Count)

# ---- load cache -------------------------------------------------------------
$geo = @{}
if (Test-Path $CachePath) {
    foreach ($c in (Import-Csv $CachePath)) { if ($c.Cell) { $geo[$c.Cell] = $c } }
    Write-Log ("cache entries loaded: {0}" -f $geo.Count)
}

$candidates = foreach ($k in $cells.Keys) {
    if (-not $geo.ContainsKey($k)) {
        [pscustomobject]@{ Cell = $k; Count = $cells[$k].n
                           Lat = $cells[$k].latSum / $cells[$k].n; Lon = $cells[$k].lonSum / $cells[$k].n }
    }
}
$candidates = @($candidates | Sort-Object Count -Descending)
Write-Log ("uncached cells: {0}" -f $candidates.Count)

# greedy spatial spread: take the busiest cells but keep them MinSepDeg apart
$selected = New-Object System.Collections.ArrayList
foreach ($c in $candidates) {
    if ($selected.Count -ge $MaxCalls) { break }
    $ok = $true
    foreach ($s in $selected) {
        $dlat = [math]::Abs($c.Lat - $s.Lat)
        $dlon = [math]::Abs($c.Lon - $s.Lon) * [math]::Cos($c.Lat * [math]::PI / 180)
        if ([math]::Sqrt($dlat * $dlat + $dlon * $dlon) -lt $MinSepDeg) { $ok = $false; break }
    }
    if ($ok) { [void]$selected.Add($c) }
}
Write-Log ("cells selected for geocoding: {0}" -f $selected.Count)

function Get-City([double]$lat, [double]$lon) {
    $urls = @(
        "https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=$lat&longitude=$lon&localityLanguage=zh",
        "https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=$lat&lon=$lon&accept-language=zh&zoom=10"
    )
    foreach ($u in $urls) {
        for ($try = 1; $try -le 2; $try++) {
            try {
                $r = Invoke-RestMethod -Uri $u -TimeoutSec 25 -Headers @{ 'User-Agent' = 'photo-city-classifier/1.0' }
                if ($u -like '*bigdatacloud*') {
                    if ($r.city) { return @{ City = $r.city; Province = $r.principalSubdivision; Locality = $r.locality; Src = 'bigdatacloud' } }
                }
                else {
                    $a = $r.address
                    $city = $a.city; if (-not $city) { $city = $a.state_district }; if (-not $city) { $city = $a.county }
                    if ($city) { return @{ City = $city; Province = $a.state; Locality = $a.city_district; Src = 'nominatim' } }
                }
            }
            catch {
                if ($try -eq 2) { break }
                Start-Sleep -Milliseconds (600 * $try)
            }
        }
    }
    return $null
}

$done = 0; $failed = 0
foreach ($c in $selected) {
    $res = Get-City $c.Lat $c.Lon
    if ($res) {
        $geo[$c.Cell] = [pscustomobject]@{
            Cell = $c.Cell; Lat = [math]::Round($c.Lat, 6); Lon = [math]::Round($c.Lon, 6)
            City = $res.City; Province = $res.Province; Locality = $res.Locality; Src = $res.Src
        }
        $done++
    }
    else { $failed++ }
    if ((($done + $failed) % 25) -eq 0) {
        $geo.Values | Sort-Object Cell | Export-Csv -Path $CachePath -NoTypeInformation -Encoding UTF8
        Write-Log ("  progress: geocoded={0} failed={1} / {2}" -f $done, $failed, $selected.Count)
    }
    Start-Sleep -Milliseconds $DelayMs
}

$geo.Values | Sort-Object Cell | Export-Csv -Path $CachePath -NoTypeInformation -Encoding UTF8
Write-Log ("cache written: {0} entries ({1} new, {2} failed) -> {3}" -f $geo.Count, $done, $failed, $CachePath)

$geo.Values | Group-Object City | Sort-Object Count -Descending | Select-Object Count, Name |
    Format-Table -AutoSize | Out-String -Width 120 | Write-Host
