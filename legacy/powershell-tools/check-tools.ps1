# check-tools.ps1 - safety net for this toolkit
#
# Why this exists: PowerShell 5.1 reads a BOM-less file as GBK, so a UTF-8 script
# with Chinese literals gets mangled and can fail to parse at all. That is exactly
# how photo-pipeline.ps1 once ended up broken after an edit. Run this after any
# edit to the tools:
#
#   1) re-encode every .ps1 in this folder as UTF-8 **with BOM**
#   2) parse every script and report syntax errors
#
# Usage: powershell -ExecutionPolicy Bypass -File check-tools.ps1 [-WhatIf]
#
# NOTE: keep this file UTF-8 with BOM.

[CmdletBinding()]
param(
    [string]$Dir = $PSScriptRoot,
    [switch]$WhatIf
)

$utf8Bom = New-Object System.Text.UTF8Encoding($true)
$fixed = 0
$bad = 0
$n = 0

foreach ($f in (Get-ChildItem -LiteralPath $Dir -Filter *.ps1 -File | Sort-Object Name)) {
    $n++
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    if (-not $hasBom) {
        $text = [System.IO.File]::ReadAllText($f.FullName, [System.Text.Encoding]::UTF8)
        if (-not $WhatIf) { [System.IO.File]::WriteAllText($f.FullName, $text, $utf8Bom) }
        Write-Host ("BOM fixed : {0}" -f $f.Name)
        $fixed++
    }

    $err = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$err)
    if (@($err).Count -gt 0) {
        $bad++
        Write-Host ("PARSE FAIL: {0}  ({1} errors)" -f $f.Name, @($err).Count)
        $err | Select-Object -First 3 | ForEach-Object { Write-Host ("   line {0} col {1}: {2}" -f $_.Extent.StartLineNumber, $_.Extent.StartColumnNumber, $_.Message) }
    }
    else {
        Write-Host ("ok        : {0}" -f $f.Name)
    }
}

Write-Host ''
Write-Host ("脚本总数 {0}，补 BOM {1} 个，语法错误 {2} 个{3}" -f $n, $fixed, $bad, $(if ($WhatIf) { '（-WhatIf：未写盘）' } else { '' }))
if ($bad -gt 0) { exit 1 } else { exit 0 }
