# -*- coding: utf-8 -*-
"""Render Director — gemeinsamer Launcher (ein Einstiegspunkt für alle Wege).

Wird aufgerufen von:
  - der Desktop-Verknüpfung `Render Director.cmd` (Option B, ohne Revit)
  - dem pyRevit-Button, Option A (nach dem Beauty-Export)
  - dem pyRevit-Button, Option B (Ad-hoc)

Aufgabe (idempotent):
  1. Prüfen, ob die Webapp schon läuft  (GET /health).
  2. Falls nicht: uvicorn im Hintergrund starten — OHNE --reload, ohne
     Konsolenfenster (pythonw), Logs nach <repo>/logs/webapp.log — und
     auf /health warten.
  3. Chrome im --app-Modus auf der gewünschten Seite öffnen (rechte
     Bildschirmhälfte). Fällt auf den Standardbrowser zurück, wenn kein
     Chrome gefunden wird.

Läuft als CPython (venv), NICHT IronPython — daher kein
System.Windows.Forms; Bildschirmgröße kommt via ctypes. Fatale Fehler
werden dem Laien-User als natives Meldungsfenster gezeigt (MessageBox),
weil der Launcher via pythonw ohne Konsole startet.

Aufruf:
    pythonw launch.py [--page home|adhoc]
                          [--snapshot <id>] [--project-dir <pfad>]
                          [--url <voll-url>] [--ensure-only]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

# ---------------------------------------------------------------------------
# Konstanten

PORT = 8765
BASE_URL = "http://localhost:{}".format(PORT)
HEALTH_URL = BASE_URL + "/health"

# Wie lange nach dem Start auf /health gewartet wird, bevor aufgegeben wird.
HEALTH_PING_TIMEOUT_S = 1.5
STARTUP_WAIT_TOTAL_S = 25.0
STARTUP_POLL_INTERVAL_S = 0.5

CHROME_CANDIDATES = [
    os.path.join(
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        "Google", "Chrome", "Application", "chrome.exe",
    ),
    os.path.join(
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        "Google", "Chrome", "Application", "chrome.exe",
    ),
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Google", "Chrome", "Application", "chrome.exe",
    ),
]


# ---------------------------------------------------------------------------
# Fehler-Feedback für Laien (pythonw = keine Konsole)

def fatal(message):
    """Zeigt eine native Fehlermeldung und beendet den Launcher."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, message, "Render Director — Fehler", 0x10  # MB_ICONERROR
        )
    except Exception:
        sys.stderr.write(message + "\n")
    sys.exit(1)


def info(message):
    """Native Info-Meldung (kein Fehler; pythonw hat keine Konsole)."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "Render Director", 0x40)  # MB_ICONINFORMATION
    except Exception:
        sys.stdout.write(message + "\n")


def stop_webapp():
    """Beendet ALLE laufenden Render-Director-Webapp-Server (uvicorn app.main:app),
    egal ob venv- oder System-Python. Filtert bewusst auf 'app.main:app',
    damit fremde uvicorn-Server (anderes Projekt) unangetastet bleiben."""
    ps = (
        "Get-CimInstance Win32_Process -Filter "
        "\"name='pythonw.exe' or name='python.exe'\" "
        "| Where-Object { $_.CommandLine -like '*uvicorn*app.main:app*' } "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        subprocess.call(["powershell", "-NoProfile", "-Command", ps])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Repo-Root + venv-Python

def find_repo_root():
    """Vom Launcher-Pfad aufwärts zur ersten pyproject.toml (analog
    src/render_director/paths.py, hier bewusst inline für Import-Robustheit)."""
    here = Path(__file__).resolve()
    for parent in [here] + list(here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    fatal(
        "Konnte das Projekt-Verzeichnis nicht finden (keine pyproject.toml "
        "im Pfad oberhalb von:\n  {}\n\nLäuft der Launcher noch aus dem "
        "richtigen Ordner?".format(here)
    )


def venv_python(repo_root, windowless=True):
    """Bevorzugt pythonw.exe (kein Konsolenfenster) aus der venv; fällt auf
    python.exe bzw. den aktuellen Interpreter zurück."""
    name = "pythonw.exe" if windowless else "python.exe"
    cand = repo_root / ".venv" / "Scripts" / name
    if cand.is_file():
        return str(cand)
    # Fallback: aktueller Interpreter (falls Launcher schon mit venv läuft)
    return sys.executable


# ---------------------------------------------------------------------------
# Webapp: erreichbar? starten?

def webapp_reachable():
    try:
        resp = urlopen(HEALTH_URL, timeout=HEALTH_PING_TIMEOUT_S)
        return resp.getcode() == 200
    except Exception:
        return False


def start_webapp(repo_root):
    """Startet uvicorn detached im Hintergrund (ohne --reload), Logs in
    <repo>/logs/webapp.log. Wartet NICHT — Caller pollt danach /health."""
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "webapp.log"

    py = venv_python(repo_root, windowless=True)
    cmd = [py, "-m", "uvicorn", "app.main:app",
           "--host", "127.0.0.1", "--port", str(PORT)]

    # Detached, ohne Konsolenfenster, überlebt den Launcher.
    flags = 0
    for attr in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, attr, 0)

    logfile = open(str(log_path), "ab", buffering=0)
    try:
        subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdout=logfile,
            stderr=logfile,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        fatal(
            "uvicorn konnte nicht gestartet werden:\n  {}\n\n"
            "Python:\n  {}\nLog:\n  {}".format(exc, py, log_path)
        )
    return log_path


def ensure_webapp(repo_root):
    """Idempotent: läuft die Webapp, sofort zurück; sonst starten + auf
    /health warten. Gibt True zurück, wenn sie am Ende erreichbar ist."""
    if webapp_reachable():
        return True
    log_path = start_webapp(repo_root)
    deadline = time.time() + STARTUP_WAIT_TOTAL_S
    while time.time() < deadline:
        if webapp_reachable():
            return True
        time.sleep(STARTUP_POLL_INTERVAL_S)
    fatal(
        "Die Render-Director-Webapp ist nach {:.0f}s nicht hochgekommen.\n\n"
        "Prüfe das Log:\n  {}".format(STARTUP_WAIT_TOTAL_S, log_path)
    )
    return False


# ---------------------------------------------------------------------------
# Chrome / Fenster

def find_chrome():
    for p in CHROME_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def right_half_bounds():
    """Rechte Hälfte der Arbeitsfläche (ohne Taskleiste) via SPI_GETWORKAREA.
    Fällt bei Fehler auf None zurück → Chrome öffnet mit Default-Größe."""
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        # SPI_GETWORKAREA = 0x0030
        if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return None
        work_w = rect.right - rect.left
        work_h = rect.bottom - rect.top
        half_w = work_w // 2
        return (rect.left + half_w, rect.top, work_w - half_w, work_h)
    except Exception:
        return None


def open_in_chrome(url):
    """Öffnet Chrome im --app-Modus rechts; Fallback = Standardbrowser."""
    chrome = find_chrome()
    if not chrome:
        try:
            os.startfile(url)  # noqa: S606 — bewusst: lokale, selbst gebaute URL
            return
        except Exception:
            fatal(
                "Chrome wurde nicht gefunden und der Standardbrowser ließ "
                "sich nicht öffnen.\n\nÖffne manuell:\n  {}".format(url)
            )
    args = [chrome, "--app=" + url, "--new-window"]
    bounds = right_half_bounds()
    if bounds:
        x, y, w, h = bounds
        args += ["--window-position={},{}".format(x, y),
                 "--window-size={},{}".format(w, h)]
    try:
        subprocess.Popen(args)
    except Exception as exc:  # noqa: BLE001
        fatal("Chrome-Start fehlgeschlagen:\n  {}\n\nURL:\n  {}".format(exc, url))


# ---------------------------------------------------------------------------
# URL bauen

def build_url(page, snapshot=None, project_dir=None, url_override=None):
    if url_override:
        return url_override
    if page == "adhoc":
        url = BASE_URL + "/adhoc"
        sep = "?"
    else:
        url = BASE_URL + "/"
        sep = "?"
        if snapshot:
            url += "?snapshot=" + quote(snapshot, safe="")
            sep = "&"
    if project_dir:
        url += sep + "project_dir=" + quote(project_dir, safe="")
    return url


# ---------------------------------------------------------------------------
# Main

def main(argv=None):
    parser = argparse.ArgumentParser(description="Render Director Launcher")
    parser.add_argument("--page", choices=["home", "adhoc"], default="adhoc",
                        help="Zielseite (Default: adhoc = direkter Start).")
    parser.add_argument("--snapshot", default=None,
                        help="Snapshot-ID (nur für --page home / Revit-Option-A).")
    parser.add_argument("--project-dir", default=None,
                        help="project_dir-Scope (Revit-Projektordner o. Ziel).")
    parser.add_argument("--url", default=None,
                        help="Vollständige URL überschreiben (Debug).")
    parser.add_argument("--ensure-only", action="store_true",
                        help="Nur Webapp sicherstellen, Chrome NICHT öffnen.")
    parser.add_argument("--stop", action="store_true",
                        help="Laufende Webapp beenden (alle app.main-Server).")
    args = parser.parse_args(argv)

    if args.stop:
        if stop_webapp():
            info("Die Render-Director-Webapp wurde beendet.")
        else:
            fatal("Konnte die Webapp nicht beenden (PowerShell-Fehler).")
        return 0

    repo_root = find_repo_root()
    ensure_webapp(repo_root)

    if args.ensure_only:
        # Für Tests / Headless-Start: Erfolg über Exit-Code signalisieren.
        print("[launch] Webapp erreichbar unter {}".format(BASE_URL))
        return 0

    url = build_url(args.page, args.snapshot, args.project_dir, args.url)
    open_in_chrome(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
