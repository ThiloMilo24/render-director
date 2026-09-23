@echo off
REM ============================================================
REM  Render Director - Desktop-Launcher (Ad-hoc / direkter Start,
REM  ohne Revit). Startet die Webapp (falls noetig) und oeffnet
REM  das Tool im Chrome-App-Fenster auf /adhoc.
REM
REM  Nutzung: Doppelklick. Fuer den Desktop:
REM    Rechtsklick auf diese Datei -> "Verknuepfung erstellen"
REM    -> Verknuepfung auf den Desktop ziehen (Icon spaeter).
REM ============================================================
setlocal
set "HERE=%~dp0"

REM Repo-Root = zwei Ebenen ueber tools\launcher\ (voll aufgeloest).
for %%I in ("%HERE%..\..") do set "REPO=%%~fI"

set "VENVPYW=%REPO%\.venv\Scripts\pythonw.exe"
if exist "%VENVPYW%" (
    start "" "%VENVPYW%" "%HERE%launch.py" --page adhoc
) else (
    REM Fallback: pythonw aus dem PATH (venv nicht gefunden).
    start "" pythonw "%HERE%launch.py" --page adhoc
)
endlocal
