@echo off
rem ---------------------------------------------------------------
rem Photo auto-classify tool  (double-click me)
rem Everything it needs is in this folder.
rem ---------------------------------------------------------------
set "HERE=%~dp0"
set "GUI=%HERE%photo-tool-gui.ps1"
if not exist "%GUI%" (
  echo.
  echo   Cannot find %GUI%
  echo.
  pause
  exit /b 1
)
start "PhotoTool" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%GUI%"