@echo off
rem ============================================================
rem  PhotoOS 桌面壳启动器（日常用这个；不需要装 Node）
rem
rem  它做三件事：切到脚本目录、清掉可能干扰 Electron 的环境变量、
rem  把自己目录作为 app 交给本地的 electron.exe。
rem
rem  后端由壳自己拉起（端口被浏览器版占着会自动换一个），
rem  关窗口 = 收进托盘，右键托盘图标可以退出。
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "ELECTRON=%~dp0node_modules\electron\dist\electron.exe"
if not exist "%ELECTRON%" (
  echo [x] 没找到 Electron：%ELECTRON%
  echo     在开发目录跑一次 `pnpm install` 就有了。
  pause
  exit /b 1
)

rem 某些环境（例如从别的 Electron 应用里继承来的）会带这两个变量，
rem 它们会让 electron.exe 变成 node、或去连别的调试端口。
set "ELECTRON_RUN_AS_NODE="
set "ELECTRON_NO_ATTACH_CONSOLE="

start "" "%ELECTRON%" "%~dp0."
endlocal
