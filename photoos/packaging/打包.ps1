<#
.SYNOPSIS
    ★ 打一个可以发给别人的交付包（**作者这边用**；收件人只需要 install.ps1）。

.DESCRIPTION
    产物默认写到 `C:\PhotoOS\_dist_package\`，里面只有三样东西：

        photoos-<版本>-py3-none-any.whl   程序本体（含前端资源与迁移文件）
        install.ps1                        一键安装脚本
        安装说明.md                         给收件人看的说明

    交付包里**不含**：模型权重（2.1 GB，让收件人自己下）、任何照片、测试与开发文档。

    ⚠️ 这个脚本会**清空输出目录再重建**。所以它自己必须放在输出目录之外
       —— 第一版把它放在交付目录里，等于每次打包都把自己删掉（并且会在
       `Remove-Item` 那一步失败，留下一个半成品目录）。这里加了显式守卫。

.EXAMPLE
    cd C:\PhotoOS\photoos\packaging
    .\打包.ps1              # 只生成目录
    .\打包.ps1 -Zip         # 额外压成一个 zip，方便发出去
#>
[CmdletBinding()]
param(
    [string]$OutDir = 'C:\PhotoOS\_dist_package',
    [switch]$Zip
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot                       # C:\PhotoOS\photoos\packaging
$repo = Split-Path $here -Parent            # C:\PhotoOS\photoos（有 pyproject.toml）
if (-not (Test-Path (Join-Path $repo 'pyproject.toml'))) {
    throw "找不到 pyproject.toml：$repo（这个脚本必须放在仓库的 packaging\ 下）"
}

# 守卫：输出目录不能是脚本自己所在的目录（否则 Remove-Item 会连脚本一起删）
$outFull = [System.IO.Path]::GetFullPath($OutDir)
if ($outFull -eq $here -or $outFull.StartsWith($here + '\')) {
    throw "输出目录不能放在 packaging\ 里面：$outFull"
}

$py = 'C:\PhotoOS\.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

Write-Host "打包 PhotoOS 交付包"
Write-Host "  仓库：$repo"
Write-Host "  输出：$outFull"

if (Test-Path $outFull) { Remove-Item $outFull -Recurse -Force }
New-Item -ItemType Directory -Path $outFull -Force | Out-Null

# ---------------------------------------------------------------- 1. wheel
Write-Host "  [1/3] 构建 wheel…"
# ⚠️ PowerShell 5.1 下 `& 原生程序 2>&1 | ...` 会把 stderr 变成 ErrorRecord，
#    而上面设了 `$ErrorActionPreference='Stop'` —— 于是 pip/build 的**正常进度输出**
#    也会让脚本直接中止（实测踩过）。所以原生调用一律把流丢掉，只看退出码与产物。
function Invoke-Quiet([string]$exe, [string[]]$arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $exe @arguments *> $null } finally { $ErrorActionPreference = $previous }
    return $LASTEXITCODE
}
$code = Invoke-Quiet $py @('-m', 'pip', 'install', '--quiet', 'build')
if ($code -ne 0) { throw "装 build 失败（退出码 $code）" }
$code = Invoke-Quiet $py @('-m', 'build', '--wheel', '--outdir', $outFull, $repo)
if ($code -ne 0) { throw "构建 wheel 失败（退出码 $code）" }
$built = Get-ChildItem "$outFull\photoos-*.whl" -ErrorAction SilentlyContinue
if (-not $built) { throw "构建结束但没有 wheel 产物" }
Write-Host "        $($built.Name)"

# ---------------------------------------------------------------- 2. 脚本与说明
Write-Host "  [2/3] 放入安装脚本与说明…"
Copy-Item (Join-Path $here 'install.ps1') $outFull -Force
Copy-Item (Join-Path $here '安装说明.md') $outFull -Force

# ---------------------------------------------------------------- 3. 自检
# ⚠️ 这一步不能省：本机是可编辑安装，读的是源码树，所以
#    "wheel 里漏了前端" 这种缺陷只有查包本身才查得出来（实测漏过一次）。
Write-Host "  [3/3] 自检 wheel 内容…"
$wheel = Get-ChildItem "$outFull\photoos-*.whl" | Select-Object -First 1
if (-not $wheel) { throw "没有生成 wheel" }
$probe = @'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
need = {"web/index.html": 1, "/web/js/": 5, "/web/css/": 4, "/web/fonts/": 3,
        "migrations/": 9}
bad = [f"{k}(要>={n})" for k, n in need.items() if len([x for x in names if k in x]) < n]
print("OK" if not bad else "缺：" + ", ".join(bad))
'@
$result = $probe | & $py - $wheel.FullName
if ("$result" -notmatch '^OK') { throw "wheel 内容不完整：$result" }
Write-Host "        $result（$([math]::Round($wheel.Length / 1KB)) KB）"

if ($Zip) {
    $zipPath = "$outFull.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path "$outFull\*" -DestinationPath $zipPath
    Write-Host "  压缩包：$zipPath（$([math]::Round((Get-Item $zipPath).Length / 1MB, 1)) MB）"
}

Write-Host ""
Write-Host "交付包就绪："
Get-ChildItem $outFull | ForEach-Object {
    Write-Host ("  {0,-36} {1,8:N0} KB" -f $_.Name, ($_.Length / 1KB))
}
Write-Host ""
Write-Host "发出去之前，建议在这台机器上再跑这两条（都验过一遍才踏实）："
Write-Host "  python C:\PhotoOS\venv\verify_clean_install.py   # 干净 venv 只装 wheel"
Write-Host "  python C:\PhotoOS\venv\verify_installer.py       # 连安装脚本一起装"
