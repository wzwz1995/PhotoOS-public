@echo off
rem ---------------------------------------------------------------
rem Photo pipeline (command line) - all arguments go to photo-pipeline.ps1
rem   import-photos.cmd "E:\new-photos"              (dry run)
rem   import-photos.cmd "E:\new-photos" -Execute     (do it)
rem ---------------------------------------------------------------
set "HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%photo-pipeline.ps1" %*