@echo off
REM ============================================================
REM  Render Director - Server beenden.
REM  Stoppt die im Hintergrund laufende Webapp (uvicorn app.main).
REM  Fremde uvicorn-Server (andere Projekte) bleiben unberuehrt.
REM ============================================================
setlocal
set "HERE=%~dp0"
for %%I in ("%HERE%..\..") do set "REPO=%%~fI"

set "VENVPYW=%REPO%\.venv\Scripts\pythonw.exe"
if exist "%VENVPYW%" (
    start "" "%VENVPYW%" "%HERE%launch.py" --stop
) else (
    start "" pythonw "%HERE%launch.py" --stop
)
endlocal
