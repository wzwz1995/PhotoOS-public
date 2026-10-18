@echo off
rem ============================================================
rem  PhotoOS 启动器（开发目录版）
rem
rem  用法：start-photoos.cmd [端口]        默认 8760
rem  关掉这个窗口 = 停止服务。
rem
rem  日志走 --log-file：不带值时写进「数据目录\logs\serve.log」。
rem  ⚠️ 不加这个参数时，uvicorn 会把每一个请求都打到这个窗口（一天近 1 MB），
rem     窗口卡住或会话被回收时，看起来就像“服务自己没了” —— 实测踩过。
rem
rem  ⚠️ 这是**开发用**启动器：只起服务，**默认不打开浏览器**（桌面端才是完成态）。
rem     功能开发时想直接用浏览器看，设 PHOTOOS_OPEN_BROWSER=1 再运行。
rem
rem  ⚠️ 本文件必须是 UTF-8（无 BOM）+ CRLF：中文一旦用 GBK 写，
rem     某个尾字节可能正好是 | 或 ^，cmd 会在 rem 行上报“不是内部命令”。
rem ============================================================
chcp 65001 >nul
setlocal
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8760"
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [x] 找不到 %PY%
  echo     这是开发目录用的启动器；安装版请用安装目录里的 start-photoos.cmd
  pause
  exit /b 1
)

rem 已经在跑就别再起一个（会撞端口），直接开界面
"%PY%" -c "import socket,sys;s=socket.socket();s.settimeout(0.6);sys.exit(0 if s.connect_ex(('127.0.0.1',%PORT%))==0 else 1)" >nul 2>&1
if not errorlevel 1 (
  echo PhotoOS 已经在端口 %PORT% 上跑着了，直接打开界面。
  if "%PHOTOOS_OPEN_BROWSER%"=="1" start "" http://127.0.0.1:%PORT%
  exit /b 0
)

echo 正在启动 PhotoOS（端口 %PORT%）……
echo   界面：http://127.0.0.1:%PORT%
echo   日志：数据目录\logs\serve.log
echo   停止：关掉这个窗口

rem 起一个等待者：服务健康之后再开浏览器（免得打开一个连不上的页面）
if "%PHOTOOS_OPEN_BROWSER%"=="1" start "" /B powershell -NoProfile -Command "for($i=0;$i -lt 120;$i++){try{Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:%PORT%/api/health' -TimeoutSec 1 | Out-Null; break}catch{Start-Sleep -Milliseconds 500}}; Start-Process 'http://127.0.0.1:%PORT%'"

"%PY%" -m photoos serve --host 127.0.0.1 --port %PORT% --log-file
endlocal
