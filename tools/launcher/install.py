# -*- coding: utf-8 -*-
"""Render Director — Bootstrap-Installer für einen frischen Rechner.

Zweck: Einrichtung mit möglichst wenig Handgriffen. Wird auf dem
SYSTEM-Python ausgeführt (bevor die venv existiert), also **nur Stdlib**.

Ablauf (idempotent, nicht-destruktiv):
  1. Python-Version prüfen (>= 3.11, wie in pyproject.toml gefordert).
  2. `.venv` anlegen, falls sie fehlt.
  3. `pip install -e .` in die venv (aktualisiert auch bei Re-Run).
  4. `.env` aus `.env.example` erzeugen, falls sie fehlt — bestehende
     Keys bleiben erhalten.
  5. Die drei API-Keys abfragen (nativer tkinter-Dialog, Paste möglich;
     Konsolen-Fallback, wenn kein tkinter). Vorhandene Werte vorbelegt,
     sodass Enter/Abbrechen nichts überschreibt.
  6. Kurze Erfolgsmeldung mit dem Hinweis, wie gestartet wird.

Aufruf:
    python tools\\launcher\\install.py
    (bequemer: install.cmd im Repo-Root doppelklicken)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

MIN_PY = (3, 11)
API_KEYS = [
    ("ANTHROPIC_API_KEY", "Anthropic (Director + Validator + Advisor)"),
    ("GOOGLE_API_KEY", "Google AI Studio (Generator NB1/NB2)"),
    ("OPENAI_API_KEY", "OpenAI (Generator GPT / gpt-image-1) — optional"),
]


# ---------------------------------------------------------------------------
# Helpers

def find_repo_root() -> Path:
    """Vom Skript aufwärts zur ersten pyproject.toml (wie launch.py)."""
    here = Path(__file__).resolve()
    for parent in [here] + list(here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    print("FEHLER: pyproject.toml nicht gefunden — läuft das Skript aus dem Repo?")
    sys.exit(1)


def venv_python(repo_root: Path) -> Path:
    """Pfad zur python.exe in der venv (Windows-Layout)."""
    return repo_root / ".venv" / "Scripts" / "python.exe"


def parse_env_values(path: Path) -> dict:
    """Liest KEY=VALUE-Zeilen (ignoriert Kommentare/Leerzeilen)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        values[k.strip()] = v.strip()
    return values


def render_env(template_path: Path, values: dict) -> str:
    """Baut den .env-Inhalt aus dem .env.example-Template und ersetzt die
    KEY=-Zeilen durch die gesammelten Werte. Kommentare bleiben erhalten.
    Keys, die im Template fehlen, werden am Ende angehängt."""
    lines = template_path.read_text(encoding="utf-8").splitlines()
    seen = set()
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key in values:
        if key not in seen:
            out.append(f"{key}={values[key]}")
    return "\n".join(out) + "\n"


def ask_keys_gui(existing: dict) -> dict:
    """Fragt die Keys per tkinter ab (Paste möglich). Vorhandene Werte sind
    vorbelegt; Abbrechen/leer lässt den bisherigen Wert stehen. Gibt None
    zurück, wenn kein tkinter verfügbar ist (→ Konsolen-Fallback)."""
    try:
        import tkinter as tk
        from tkinter import simpledialog
    except Exception:
        return None

    root = tk.Tk()
    root.withdraw()
    result = dict(existing)
    try:
        for key, label in API_KEYS:
            cur = existing.get(key, "")
            prompt = (
                f"{label}\n\nKey einfügen (Strg+V). Leer lassen behält den "
                f"bisherigen Wert."
            )
            val = simpledialog.askstring(
                "Render Director — API-Key", prompt, initialvalue=cur, parent=root,
            )
            if val is not None and val.strip():
                result[key] = val.strip()
    finally:
        root.destroy()
    return result


def ask_keys_console(existing: dict) -> dict:
    """Konsolen-Fallback für die Key-Abfrage."""
    result = dict(existing)
    print("\nAPI-Keys eintragen (Enter behält den bisherigen Wert):")
    for key, label in API_KEYS:
        cur = existing.get(key, "")
        shown = (cur[:6] + "…") if cur else "(leer)"
        val = input(f"  {label}\n    {key} [{shown}]: ").strip()
        if val:
            result[key] = val
    return result


def native_info(message: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "Render Director", 0x40)
    except Exception:
        print(message)


# ---------------------------------------------------------------------------
# Main

def main() -> int:
    print("=== Render Director — Installation ===\n")

    if sys.version_info < MIN_PY:
        print(
            "FEHLER: Python {}.{}+ nötig, gefunden {}.{}.\n"
            "Bitte aktuelles Python von python.org installieren "
            "(mit 'Add to PATH').".format(
                MIN_PY[0], MIN_PY[1], sys.version_info[0], sys.version_info[1],
            )
        )
        return 1

    repo_root = find_repo_root()
    print(f"Projekt: {repo_root}\n")

    # 1) venv anlegen (falls fehlt)
    vpy = venv_python(repo_root)
    if vpy.is_file():
        print("venv existiert bereits — überspringe Anlegen.")
    else:
        print("Lege virtuelle Umgebung an (.venv) …")
        try:
            subprocess.check_call([sys.executable, "-m", "venv", str(repo_root / ".venv")])
        except subprocess.CalledProcessError as exc:
            print(f"FEHLER beim venv-Anlegen: {exc}")
            return 1

    # 2) Abhängigkeiten installieren (auch bei Re-Run = Update)
    print("\nInstalliere Abhängigkeiten (kann 1-2 Minuten dauern) …\n")
    try:
        subprocess.check_call([str(vpy), "-m", "pip", "install", "--upgrade", "pip", "-q"])
        subprocess.check_call([str(vpy), "-m", "pip", "install", "-e", ".", "-q"], cwd=str(repo_root))
    except subprocess.CalledProcessError as exc:
        print(f"FEHLER bei pip install: {exc}")
        return 1
    print("Abhängigkeiten installiert. ✓")

    # 3) .env vorbereiten + Keys abfragen
    env_path = repo_root / ".env"
    example_path = repo_root / ".env.example"
    if not example_path.is_file():
        print("WARNUNG: .env.example fehlt — überspringe Key-Setup.")
    else:
        existing = parse_env_values(env_path)
        # Wenn ALLE Keys schon in der .env stehen (z.B. bei einem erneuten
        # Installer-Lauf), die Abfrage überspringen. Zum Ändern: .env
        # bearbeiten (oder die Key-Zeile leeren) und install.cmd erneut.
        if all(existing.get(k) for k, _ in API_KEYS):
            keys = existing
            print(
                "\nAlle API-Keys sind bereits in der .env gesetzt — "
                "überspringe die Abfrage.\n"
                "(Zum Ändern: .env bearbeiten bzw. eine Key-Zeile leeren und "
                "install.cmd erneut ausführen.)"
            )
        else:
            keys = ask_keys_gui(existing)
            if keys is None:
                keys = ask_keys_console(existing)
        content = render_env(example_path, keys)
        env_path.write_text(content, encoding="utf-8")
        have = [k for k, _ in API_KEYS if keys.get(k)]
        print(f"\n.env geschrieben. Eingetragene Keys: {', '.join(have) or '(keine)'}")

    native_info(
        "Render Director ist installiert.\n\n"
        "Starten: 'Render Director.cmd' im Ordner tools\\launcher\\ "
        "(oder auf den Desktop kopieren/anpinnen).\n\n"
        "Zum Ändern der Keys diesen Installer einfach erneut ausführen."
    )
    print("\n=== Fertig. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
