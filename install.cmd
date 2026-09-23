@echo off
REM ==========================================================================
REM  Render Director - Installer fuer einen frischen Rechner.
REM  Doppelklick nach dem Auspacken. Legt die venv an, installiert die
REM  Abhaengigkeiten und fragt die API-Keys ab.
REM ==========================================================================
setlocal
cd /d "%~dp0"

set "PYCMD="
where py     >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD ( where python >nul 2>nul && set "PYCMD=python" )

if not defined PYCMD (
  echo.
  echo Python wurde nicht gefunden.
  echo Bitte Python 3.11+ von https://www.python.org/downloads/ installieren
  echo und bei der Installation "Add Python to PATH" ankreuzen. Danach diese
  echo Datei erneut doppelklicken.
  echo.
  pause
  exit /b 1
)

%PYCMD% "tools\launcher\install.py"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo Installation abgebrochen ^(Fehlercode %RC%^). Siehe Meldungen oben.
)
pause
exit /b %RC%
