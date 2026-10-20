# PhotoOS technical-reference docs: read-only recompute wrapper (Windows).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File verify-docs.ps1
#
# Why -ExecutionPolicy Bypass: this machine's execution policy blocks local
# scripts and reports "running scripts is disabled" -- that is the policy, not a
# broken script (same trap as HANDOVER.md:1029).
#
# This wrapper is deliberately ASCII-only so it cannot be broken by console
# code-page guessing. All check logic and all Chinese output live in
# verify_docs.py, which is UTF-8. The Python script is READ-ONLY: it opens the
# database with a mode=ro URI, never walks the photo drives, and writes no files.

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$candidates = @(
    'C:\PhotoOS\.venv\Scripts\python.exe',
    (Join-Path $here '..\..\..\.venv\Scripts\python.exe'),
    'python'
)

$python = $null
foreach ($candidate in $candidates) {
    if ($candidate -eq 'python') { $python = 'python'; break }
    if (Test-Path -LiteralPath $candidate) { $python = (Resolve-Path -LiteralPath $candidate).Path; break }
}

if (-not $python) {
    Write-Error 'No python interpreter found (tried .venv and PATH).'
    exit 2
}

$env:PYTHONIOENCODING = 'utf-8'
& $python '-u' (Join-Path $here 'verify_docs.py') @args
exit $LASTEXITCODE
