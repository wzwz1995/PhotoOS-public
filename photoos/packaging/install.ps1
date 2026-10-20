<#
.SYNOPSIS
    PhotoOS 一键安装（在**收件人的机器**上运行）。

.DESCRIPTION
    做四件事，每件都先说清楚再动手：
      1. 找到 Python 3.12（没有就说去哪装，不偷偷装东西）；
      2. 在指定目录建 venv；
      3. 装 PhotoOS（可选连 AI 依赖一起装）；
      4. 初始化数据目录，问你要不要立刻打开界面。

    这个脚本**只写两个地方**：安装目录里的 venv，以及数据目录
    （默认 `%LOCALAPPDATA%\PhotoOS`，可用 -DataRoot 改）。
    它不会碰你的照片：照片是由你在界面里"添加库"之后，PhotoOS 只读索引的。

.EXAMPLE
    # 基础安装（浏览 / 精选 / 主题 / 打包；不装 AI，不需要模型）
    .\install.ps1 -Wheel .\photoos-0.1.0-py3-none-any.whl

.EXAMPLE
    # 连 AI 一起装（人脸分人 + 中文找照片；会多下约 500 MB 依赖）
    .\install.ps1 -Wheel .\photoos-0.1.0-py3-none-any.whl -WithAI

.EXAMPLE
    # 数据放别处
    .\install.ps1 -Wheel .\photoos-0.1.0-py3-none-any.whl -DataRoot C:\PhotoOSData
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Wheel,
    [string]$InstallDir = "$env:LOCALAPPDATA\PhotoOS\app",
    [string]$DataRoot,
    [switch]$WithAI,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
function Say($text) { Write-Host $text }
function Ok($text) { Write-Host "  [OK] $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  [!] $text" -ForegroundColor Yellow }
function Die($text) { Write-Host "  [X] $text" -ForegroundColor Red; exit 1 }

Say "PhotoOS 安装"
Say "  安装目录：$InstallDir"
Say "  数据目录：$(if ($DataRoot) { $DataRoot } else { "$env:LOCALAPPDATA\PhotoOS" })"
Say ""

# ---------------------------------------------------------------- 1. Python
#
# ⚠️ 两个都踩过的坑，别再踩回去：
#   * **不要用 `$args` 当变量名** —— 它是 PowerShell 的自动变量（未绑定参数数组），
#     赋值 + splat 在 Windows PowerShell 5.1 上会串味，实测让 Python 进了 REPL。
#   * **探测版本不要用 `-c "…"`** —— 5.1 向原生程序传参时对内嵌引号的处理很不一致，
#     实测那段程序被拆坏（`File "<string>", line 1`）。
#     改成 `python -V`（一个引号都不需要），再正则匹配。
$pythonExe = $null
$pythonArgs = @()
foreach ($candidate in @(@{ Exe = 'py'; Args = @('-3.12') },
                         @{ Exe = 'python'; Args = @() })) {
    if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
    $banner = & $candidate.Exe @($candidate.Args + @('-V')) 2>&1
    $text = "$banner".Trim()
    if ($text -match 'Python 3\.12') {
        $pythonExe = $candidate.Exe
        $pythonArgs = @($candidate.Args)
        break
    }
    if ($text -match 'Python (\d+\.\d+)') { Warn "$($candidate.Exe) 是 $($Matches[1])（需要 3.12）" }
}
if (-not $pythonExe) {
    Die @"
没找到 Python 3.12。请先装它（免费，约 25 MB）：
    https://www.python.org/downloads/release/python-31210/
  ⚠️ 安装时勾上 "Add python.exe to PATH"（安装器第一屏最下面那个勾）。
装完关掉这个窗口、重新开一个再运行本脚本。
"@
}
Ok "Python 3.12：$pythonExe $($pythonArgs -join ' ')"

# ---------------------------------------------------------------- 2. venv
if (-not (Test-Path $InstallDir)) { New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null }
$venv = Join-Path $InstallDir 'venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    Say "建虚拟环境（不动系统 Python）…"
    & $pythonExe @($pythonArgs + @('-m', 'venv', $venv))
    if ($LASTEXITCODE -ne 0) { Die "建 venv 失败" }
}
$vpy = Join-Path $venv 'Scripts\python.exe'
Ok "虚拟环境：$venv"

# ---------------------------------------------------------------- 3. 装包
Say "安装 PhotoOS（第一次要下依赖，几分钟）…"
if ($WithAI) {
    Warn "选了 -WithAI：会多下约 500 MB（onnxruntime / OpenCV / torch）"
    & $vpy -m pip install --upgrade pip --quiet
    & $vpy -m pip install "$Wheel[ai-cpu]"
} else {
    & $vpy -m pip install --upgrade pip --quiet
    & $vpy -m pip install $Wheel
}
if ($LASTEXITCODE -ne 0) { Die "安装失败：请把上面的报错整段发给我" }
Ok "已安装"

# ---------------------------------------------------------------- 4. 初始化
if ($DataRoot) { $env:PHOTOOS_DATA_ROOT = $DataRoot }
Say "初始化数据目录与数据库…"
& $vpy -m photoos init
if ($LASTEXITCODE -ne 0) { Die "初始化失败" }

$target = if ($DataRoot) { $DataRoot } else { Join-Path $env:LOCALAPPDATA 'PhotoOS\data' }
Say ""
Ok "装好了。数据（索引/缩略图/人物/主题）在：$target"
Say "  你的照片不会被改动：PhotoOS 只读索引；只有你主动用「导入」时才会复制文件。"

# ---------------------------------------------------------------- 5. 起服务
$serve = Join-Path $InstallDir 'start-photoos.cmd'
@"
@echo off
rem PhotoOS 启动器（安装脚本生成）。关掉这个窗口就等于停止服务。
rem --log-file：日志写进数据目录的 logs\serve.log。
rem   不加它时 uvicorn 会把**每一个请求**都打到这个窗口（一天能刷出一兆多行），
rem   窗口被卡住、或者会话被回收时，看起来就像"服务自己没了"。
"$vpy" -m photoos serve --log-file %*
start "" http://127.0.0.1:8760
"@ | Set-Content -Path $serve -Encoding OEM
Ok "启动器：$serve（双击即可；日志在数据目录的 logs\serve.log）"

if (-not $NoLaunch) {
    $answer = Read-Host "现在启动 PhotoOS 吗？(Y/n)"
    if ($answer -ne 'n' -and $answer -ne 'N') {
        Start-Process -FilePath $serve
        Say "已启动。浏览器稍后会自动打开 http://127.0.0.1:8760"
        Say "关掉那个黑窗口就等于停止 PhotoOS（它只在这台机器上监听 127.0.0.1）。"
    }
}
Say ""
Say "下一步（在界面里）："
Say "  1) 「照片库」→ 添加库 → 指向你的照片目录（只读索引，不动文件）"
Say "  2) 「维护」→ 完整索引（几万张要跑一阵，可以随时暂停）"
if (-not $WithAI) {
    Say "  3) 想用「分人」和「中文找照片」？重跑本脚本加 -WithAI，再执行："
    Say "     `"$vpy`" -m photoos ai fetch-model --engine all"
}
