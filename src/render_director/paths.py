"""Pfad-Resolver für das Render-Director-Projekt.

Auto-Detect-via-Project-Root (User-Entscheidung Phase-4-Schritt-1):
Statt jeden Caller einen `PROMPTS_DIR`/`GENERATIONS_DIR` übergeben zu
lassen, läuft das Package vom eigenen Datei-Pfad aufwärts und sucht
`pyproject.toml`. Alle Standard-Daten-Ordner sind davon relativ.

Webapp und CLI nutzen dieselben Default-Pfade — Override geht über
die `*_dir`-Parameter in `run_iteration()`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def project_root(start: Optional[Path] = None) -> Path:
    """Wandert vom Modul-Pfad aufwärts zur ersten `pyproject.toml`.

    Wird einmal beim Import dieses Moduls aufgelöst (siehe `PROJECT_ROOT`-
    Konstante unten). Funktion bleibt exponiert, falls ein anderer Caller
    (z.B. ein Test-Skript) explizit von einem anderen Start-Pfad suchen
    will.
    """
    here = (start or Path(__file__)).resolve()
    for parent in [here] + list(here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError(
        f"Kein pyproject.toml im Pfad-Walk-up von {here} gefunden — "
        "Project-Root nicht ermittelbar."
    )


PROJECT_ROOT: Path = project_root()
PROMPTS_DIR: Path = PROJECT_ROOT / "prompts"
SCENES_DIR: Path = PROJECT_ROOT / "data" / "scenes"
GENERATIONS_DIR: Path = PROJECT_ROOT / "data" / "generations"
REFERENCE_LIBRARY_DIR: Path = PROJECT_ROOT / "data" / "reference_library"
