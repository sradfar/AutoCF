@echo off
setlocal
set "NO_PAUSE="
if /i "%~1"=="--non-interactive" set "NO_PAUSE=1"
if defined _PYI_APPLICATION_HOME_DIR set "NO_PAUSE=1"
if not defined NO_PAUSE (
  mode con >nul 2>&1
  if errorlevel 1 set "NO_PAUSE=1"
)
set "BACKEND=%~dp0AutoCF-Windows.ps1"
set "WORKROOT=%~dp0.."
if exist "%~dp0windows\AutoCF-Windows.ps1" (
  set "BACKEND=%~dp0windows\AutoCF-Windows.ps1"
  set "WORKROOT=%~dp0"
)
cd /d "%WORKROOT%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%BACKEND%" -Action Install
set "INSTALL_EXIT=%ERRORLEVEL%"
if "%INSTALL_EXIT%"=="3010" (
  echo.
  echo Windows restart required. Use the AutoCF restart button to continue.
  if not defined NO_PAUSE pause
  exit /b 3010
)
if not "%INSTALL_EXIT%"=="0" (
  echo.
  echo AutoCF installation did not complete ^(exit code %INSTALL_EXIT%^).
  if not defined NO_PAUSE pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -Command "$distros = @(& wsl.exe --list --quiet 2>$null) -replace [char]0, ''; if ($distros -contains 'AutoCF') { exit 0 } else { exit 1 }"
if errorlevel 1 (
  echo.
  echo Windows accepted the prerequisite installation, but the AutoCF runtime
  echo is not registered yet. Restart from the AutoCF UI to continue.
  if not defined NO_PAUSE pause
  exit /b 3010
)
echo.
echo AutoCF installation finished and verified.
if not defined NO_PAUSE pause
