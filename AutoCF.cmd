@echo off
setlocal
set "BACKEND=%~dp0AutoCF-Windows.ps1"
set "WORKROOT=%~dp0.."
if exist "%~dp0windows\AutoCF-Windows.ps1" (
  set "BACKEND=%~dp0windows\AutoCF-Windows.ps1"
  set "WORKROOT=%~dp0"
)
cd /d "%WORKROOT%"
if "%~1"=="" (
  echo Usage: AutoCF.cmd CONFIG.yml [build^|run^|full^|evaluate^|impact]
  echo.
  echo   build  Build the HydroMT-SFINCS project only
  echo   run    Run an existing project with CPU SFINCS
  echo   full   Build and run the project on CPU
  echo   evaluate  Create flood maps and gauge evaluation metrics
  echo   impact    Attribute flooding to buildings, roads, and population
  exit /b 2
)
set "MODE=%~2"
if "%MODE%"=="" set "MODE=full"
if /i "%MODE%"=="evaluate" (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%BACKEND%" -Action Evaluate -Config "%~1"
  exit /b %ERRORLEVEL%
)
if /i "%MODE%"=="impact" (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%BACKEND%" -Action Impact -Config "%~1"
  exit /b %ERRORLEVEL%
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%BACKEND%" -Action Run -Config "%~1" -Mode "%MODE%"
exit /b %ERRORLEVEL%
