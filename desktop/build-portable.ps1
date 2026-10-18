<#
.SYNOPSIS
  把 PhotoOS 打成"解压即用"的绿色包（内置 embeddable Python）。

.DESCRIPTION
  产物：desktop\_dist\PhotoOS-<版本>-win64.zip
  内容：PhotoOS.exe + resources/app + runtime/python + runtime/backend + BUILD.txt

  设计约束（见 打包方案.md）：
  * 只出 `electron-builder --dir`，不碰 NSIS（本机 26+11 会报 No JSON content found）
  * 依赖必须全部是 wheel（--only-binary=:all:），要现场编译就失败
  * 不内置 AI、不内置 ffmpeg 之外的任何东西（imageio-ffmpeg 自带的 ffmpeg 除外，见下）

.PARAMETER DryRun
  只做前置检查并打印计划，不下载、不安装、不打包。

.EXAMPLE
  pwsh -File desktop\build-portable.ps1 -DryRun
  pwsh -File desktop\build-portable.ps1
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$PythonVersion = '3.12.10',
    [string]$Mirror = 'https://registry.npmmirror.com/-/binary/python'
)

$ErrorActionPreference = 'Stop'
$Desktop  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo     = Split-Path -Parent $Desktop
$Runtime  = Join-Path $Desktop 'runtime'
$PyDir    = Join-Path $Runtime 'python'
$Staging  = Join-Path $Desktop '_dist\win-unpacked'
$OutDir   = Join-Path $Desktop '_dist'
# package.json 是 UTF-8；PS 5.1 的 Get-Content -Raw 会按 GBK 读，中文注释会把 JSON 读坏
$Version  = ($null = [System.IO.File]::ReadAllText((Join-Path $Desktop 'package.json'), [System.Text.UTF8Encoding]::new($false)) | ConvertFrom-Json).version

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Need($cond, $msg) { if (-not $cond) { throw $msg } }

Step "前置检查"
Need (Test-Path (Join-Path $Repo 'photoos\pyproject.toml')) '找不到 photoos/pyproject.toml（脚本要在仓库里跑）'
Need (Test-Path (Join-Path $Desktop 'requirements.lock')) '缺少 desktop/requirements.lock'
Need (Test-Path (Join-Path $Desktop 'node_modules\electron\dist\electron.exe')) '缺少 Electron 运行时（先 pnpm install）'
$electronBuilder = Join-Path $Desktop 'node_modules\electron-builder\out\cli\cli.js'
Need (Test-Path $electronBuilder) '缺少 electron-builder（先 pnpm install）'
$pkgCount = (Select-String -Path (Join-Path $Desktop 'requirements.lock') -Pattern '^[a-zA-Z]' | Measure-Object).Count
Write-Host "    版本 $Version · 依赖 $pkgCount 个 · Python $PythonVersion"

if ($DryRun) {
    Write-Host ''
    Write-Host '计划（DryRun，不执行）：'
    Write-Host "  1) 下载 embeddable Python $PythonVersion -> $PyDir"
    Write-Host "  2) 改 python312._pth（加 Lib\site-packages）"
    Write-Host "  3) get-pip + pip install -r requirements.lock --only-binary=:all: --target $PyDir\Lib\site-packages"
    Write-Host "  4) 复制 photoos 后端 -> $Runtime\backend\photoos"
    Write-Host "  5) electron-builder --dir -> $Staging"
    Write-Host "  6) 组装 + BUILD.txt + 压缩 -> $OutDir\PhotoOS-$Version-win64.zip"
    exit 0
}

# 1) embeddable Python ------------------------------------------------------
$zip = Join-Path $Runtime "python-$PythonVersion-embed-amd64.zip"
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
if (-not (Test-Path (Join-Path $PyDir 'python.exe'))) {
    Step "下载 embeddable Python $PythonVersion"
    if (-not (Test-Path $zip)) {
        $url = "$Mirror/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    }
    Remove-Item $PyDir -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive -Path $zip -DestinationPath $PyDir -Force
} else { Step 'embeddable Python 已存在，跳过下载' }

# 2) _pth：让 *.pth 生效，并把 site-packages 挂上 ---------------------------
$pth = Get-ChildItem $PyDir -Filter 'python*._pth' | Select-Object -First 1
if ($pth) {
    # ⚠️ embeddable Python 一旦有 ._pth 就进入**隔离模式**：PYTHONPATH 与环境变量全部失效，
    # 只有这里列出的目录可用。所以后端目录必须写进来（`..\backend` 相对 python 目录），
    # 否则 `python -m photoos` 永远 No module named photoos。
    $needed = @('..\backend', 'Lib\site-packages')
    $content = @(Get-Content $pth.FullName)
    $changed = $false
    foreach ($line in $needed) {
        if ($content -notcontains $line) { $content = @($line) + $content; $changed = $true }
    }
    $content = $content | ForEach-Object { $_ -replace '^#\s*import site$', 'import site' }
    if ($content -notcontains 'import site') { $content += 'import site'; $changed = $true }
    if ($changed) { Step '修正 ._pth（加 ..\backend 与 site-packages）'; Set-Content -Path $pth.FullName -Value $content -Encoding ascii }
}

# 3) 依赖 -------------------------------------------------------------------
$python = Join-Path $PyDir 'python.exe'
$sitePkgs = Join-Path $PyDir 'Lib\site-packages'
if (-not (Test-Path (Join-Path $sitePkgs 'fastapi'))) {
    Step '安装运行时依赖（只允许 wheel）'
    if (-not (Test-Path (Join-Path $PyDir 'Lib\site-packages\pip'))) {
        $getpip = Join-Path $Runtime 'get-pip.py'
        if (-not (Test-Path $getpip)) {
            Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getpip -UseBasicParsing
        }
        & $python $getpip --no-warn-script-location
        Need ($LASTEXITCODE -eq 0) 'get-pip 失败'
    }
    & $python -m pip install -r (Join-Path $Desktop 'requirements.lock') 'uvicorn[standard]==0.54.0' `
        --target $sitePkgs --only-binary=:all: --no-warn-script-location `
        -i 'https://pypi.tuna.tsinghua.edu.cn/simple'
    Need ($LASTEXITCODE -eq 0) '依赖安装失败（可能需要现场编译 → 不可复制，请换 wheel）'
} else { Step '依赖已就位，跳过安装' }

# 3b) virtualenv：打包版解释器是 embeddable，没有标准库 `venv`，
#     AI 组件安装要靠 virtualenv 建独立环境 —— 必须预装，否则要联网才能装。
# 3d) onnxruntime：相似照片 / 以图搜图的运行时时。15 MB，随包预装，
#      免得用户第一次用相似照片时被"运行时未安装"挡住。
if (-not (Test-Path (Join-Path $sitePkgs 'onnxruntime'))) {
    Step '补装 onnxruntime（相似照片 / 以图搜图的运行时）'
    & $python -m pip install 'onnxruntime==1.31.0' --target $sitePkgs `
        --only-binary=:all: --no-warn-script-location `
        -i 'https://pypi.tuna.tsinghua.edu.cn/simple'
    Need ($LASTEXITCODE -eq 0) 'onnxruntime 安装失败'
}

# 3c) imageio-ffmpeg：视频缩略图要用，且必须**随包**（用户机器没有 ffmpeg）
if (-not (Test-Path (Join-Path $sitePkgs 'imageio_ffmpeg'))) {
    Step '补装 imageio-ffmpeg（视频缩略图依赖自带 ffmpeg）'
    & $python -m pip install 'imageio-ffmpeg==0.6.0' --target $sitePkgs `
        --only-binary=:all: --no-warn-script-location `
        -i 'https://pypi.tuna.tsinghua.edu.cn/simple'
    Need ($LASTEXITCODE -eq 0) 'imageio-ffmpeg 安装失败'
}

if (-not (Test-Path (Join-Path $sitePkgs 'virtualenv'))) {
    Step '补装 virtualenv（embeddable Python 没有 venv 模块）'
    & $python -m pip install 'virtualenv==21.14.11' --target $sitePkgs `
        --only-binary=:all: --no-warn-script-location `
        -i 'https://pypi.tuna.tsinghua.edu.cn/simple'
    Need ($LASTEXITCODE -eq 0) 'virtualenv 安装失败'
}

# 4) 后端源码 ---------------------------------------------------------------
Step '复制后端源码'
$backendOut = Join-Path $Runtime 'backend\photoos'
Remove-Item $backendOut -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Split-Path $backendOut -Parent) | Out-Null
Copy-Item (Join-Path $Repo 'photoos\backend\photoos') $backendOut -Recurse
Get-ChildItem $backendOut -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
# 冒烟：用打包进来的解释器直接 import 后端
& $python -c "import sys; sys.path.insert(0, r'$Runtime\backend'); import photoos; print('backend ok', photoos.__version__ if hasattr(photoos,'__version__') else '')"
Need ($LASTEXITCODE -eq 0) '打包后的后端无法导入（依赖或路径不对）'

# 5) Electron 解包目录 ------------------------------------------------------
# ---- 预检：结束正在运行的打包版（否则 _dist 里的文件被占用，压缩会失败）
function Stop-DistProcesses {
    $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$OutDir\*" }
    if (-not $procs) { return 0 }
    Write-Host "  结束 $($procs.Count) 个占用 _dist 的进程" -ForegroundColor Yellow
    foreach ($proc in $procs) {
        try { $proc.Kill(); Write-Host "    已结束 $($proc.ProcessName) PID $($proc.Id)" }
        catch { Write-Host "    无法结束 $($proc.ProcessName) PID $($proc.Id)：$($_.Exception.Message)" -ForegroundColor DarkYellow }
    }
    Start-Sleep -Seconds 3
    return $procs.Count
}
Stop-DistProcesses | Out-Null

Step '组装 Electron 运行时（不用 electron-builder）'
# ⚠️ 为什么绕开 electron-builder：本机 electron-builder 26 + pnpm 11 在收集 node_modules
# 时报 `No JSON content found in output`（README 与调研 §4.5 都记着），连 `--dir` 也过不去。
# 我们的壳**没有运行时 node 依赖**（全部是 devDependencies），所以直接拿 electron 的
# dist 目录 + 自己的 app 目录，效果与 --dir 一致，还少一层工具链。
$electronDist = Join-Path $Desktop 'node_modules\electron\dist'
Need (Test-Path (Join-Path $electronDist 'electron.exe')) "缺少 Electron 运行时：$electronDist"
Remove-Item $Staging -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Staging | Out-Null
Copy-Item (Join-Path $electronDist '*') $Staging -Recurse -Force

# 应用目录：只放壳自己需要的文件（与 package.json 的 build.files 保持一致）
$appDir = Join-Path $Staging 'resources\app'
Remove-Item $appDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $appDir | Out-Null
foreach ($item in 'main.js', 'preload.js', 'preload-prompt.js', 'prompt.html', 'loading.html', 'package.json') {
    Copy-Item (Join-Path $Desktop $item) $appDir -Force
}
foreach ($item in 'lib', 'assets') {
    Copy-Item (Join-Path $Desktop $item) $appDir -Recurse -Force
}
Remove-Item (Join-Path $Staging 'resources\default_app.asar') -Force -ErrorAction SilentlyContinue

# 改名为 PhotoOS.exe（Electron 按"exe 同级 resources/app"找应用，改名不影响）
Move-Item (Join-Path $Staging 'electron.exe') (Join-Path $Staging 'PhotoOS.exe') -Force

# 语言包只留中文/英文（其余每个 ~1MB）
Get-ChildItem (Join-Path $Staging 'locales') -Filter '*.pak' -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notin 'zh-CN.pak', 'en-US.pak' } |
    Remove-Item -Force
Remove-Item (Join-Path $Staging 'LICENSES.chromium.html') -Force -ErrorAction SilentlyContinue

# 6) 组装 -------------------------------------------------------------------
Step '组装绿色包'
$stage = Join-Path $OutDir "PhotoOS-$Version-win64"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
# ⚠️ 删不掉（被占用/杀毒）时不能继续：否则 Copy-Item 会把 win-unpacked **嵌进去**，
# 后面 Get-FileHash $stage\PhotoOS.exe 直接报"找不到路径"（实测踩到）。
if (Test-Path $stage) {
    $trash = "$stage-old-$(Get-Random)"
    Write-Host "  旧目录删不掉，改名为 $trash（不影响本次构建）" -ForegroundColor Yellow
    Rename-Item $stage $trash -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item (Join-Path $Staging '*') $stage -Recurse -Force
# ⚠️ 必须先清空目标 runtime：`Copy-Item $Runtime $stage\runtime` 在目标已存在时
# 会**嵌套成 runtime\runtime\python**，包里的解释器就"消失"了 ——
# 表现是桌面壳找不到后端，转而 attach 到 8760 上正在跑的服务（于是显示别人的库）。
$runtimeOut = Join-Path $stage 'runtime'
Remove-Item $runtimeOut -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $runtimeOut | Out-Null
Copy-Item (Join-Path $Runtime '*') $runtimeOut -Recurse -Force
Copy-Item (Join-Path $Desktop '首次运行说明.txt') $stage -ErrorAction SilentlyContinue
Copy-Item (Join-Path $Repo 'LICENSE') $stage -ErrorAction SilentlyContinue

$sha = (Get-FileHash (Join-Path $stage 'PhotoOS.exe') -Algorithm SHA256).Hash
@"
PhotoOS $Version
构建时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
Python  : $PythonVersion (embeddable)
PhotoOS.exe sha256: $sha
依赖    : $pkgCount 个（见 requirements.lock）
"@ | Set-Content (Join-Path $stage 'BUILD.txt') -Encoding utf8

$zipOut = Join-Path $OutDir "PhotoOS-$Version-win64.zip"
Remove-Item $zipOut -Force -ErrorAction SilentlyContinue
$attempt = 0
while ($true) {
    $attempt++
    try {
        # ---- 组装后自检：把"开发时能跑、打包后少东西"这类问题挡在交付前 ----
Step '自检：包内是否完整'
$problems = @()
if (-not (Test-Path (Join-Path $stage 'runtime\python\python.exe'))) { $problems += '缺少 runtime\python\python.exe' }
if (-not (Test-Path (Join-Path $stage 'resources\app\main.js')))  { $problems += '缺少 resources\app\main.js' }
if (Test-Path (Join-Path $stage 'runtime\runtime'))                { $problems += 'runtime 目录被嵌套（runtime\runtime）' }

# 后端源码必须与仓库逐文件一致（含新增的 web 资源，它们也在 photoos\web 下）
$srcRoot = Join-Path $Repo 'photoos\backend\photoos'
$pkgRoot = Join-Path $stage 'runtime\backend\photoos'
$srcFiles = Get-ChildItem $srcRoot -Recurse -File | Where-Object { $_.FullName -notmatch '__pycache__' }
foreach ($file in $srcFiles) {
    $rel = $file.FullName.Substring($srcRoot.Length).TrimStart('\')
    $target = Join-Path $pkgRoot $rel
    if (-not (Test-Path $target)) { $problems += "包内缺文件：$rel"; continue }
    if ((Get-FileHash $file.FullName -Algorithm SHA256).Hash -ne (Get-FileHash $target -Algorithm SHA256).Hash) {
        $problems += "包内文件与源码不一致：$rel"
    }
}

# 运行时依赖必须能被包内解释器真的导入（httpx 那次就是"开发环境有、包里没有"）
$py = Join-Path $stage 'runtime\python\python.exe'
$probe = & $py -c "import fastapi, uvicorn, pydantic, PIL, numpy, virtualenv, imageio_ffmpeg, onnxruntime; print('deps-ok')" 2>&1
if ($LASTEXITCODE -ne 0) { $problems += "包内依赖导入失败：$($probe -join ' ')" }
$ffmpegOk = & $py -c "import imageio_ffmpeg,os;print('ok' if os.path.isfile(imageio_ffmpeg.get_ffmpeg_exe()) else 'missing')" 2>&1
if (($ffmpegOk -join '').Trim() -ne 'ok') { $problems += "包内 ffmpeg 可执行文件缺失（视频缩略图会失败）" }

if ($problems.Count) {
    $problems | ForEach-Object { Write-Host "  ✗ $_" -ForegroundColor Red }
    throw "自检未通过（$($problems.Count) 项），已停止打包"
}
Write-Host "  ✓ 源码一致 / 依赖可导入 / 目录结构正常" -ForegroundColor Green

Compress-Archive -Path $stage -DestinationPath $zipOut -CompressionLevel Optimal -Force
        break
    }
    catch {
        # 典型原因：某个进程还开着 _dist 里的 exe/dll（杀毒扫描、资源管理器预览）
        Write-Host "  压缩失败（第 $attempt 次）：$($_.Exception.Message)" -ForegroundColor Yellow
        if ($attempt -ge 3) { throw }
        Stop-DistProcesses | Out-Null
        Remove-Item $zipOut -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
    }
}
Write-Host ''
Write-Host ("完成: {0}  ({1:N0} MB)" -f $zipOut, ((Get-Item $zipOut).Length / 1MB)) -ForegroundColor Green
