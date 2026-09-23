"""App-Settings via pydantic-settings.

Defaults bevorzugen lokales Testen: Snapshot-Root zeigt auf
`data/scenes/` (vorhandene Phase-0/1/2-Scenes), Generations-Root auf
`data/generations/`. Beide überschreibbar via Env-Vars
`RENDER_SNAPSHOT_ROOT` / `RENDER_GENERATIONS_ROOT`, sodass die App ohne
Code-Änderung auf das pyRevit-Schreibziel (`Revit_Render/<View-Name>/`)
umgeschwenkt werden kann.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from render_director.paths import PROJECT_ROOT, REFERENCE_LIBRARY_DIR


class Settings(BaseSettings):
    snapshot_root: Path = Field(
        default=PROJECT_ROOT / "data" / "scenes",
        description=(
            "Wurzelordner der View-Snapshots (Beauty + meta.json + ggf. "
            "depth/material_id). MVP-Default = data/scenes (Phase-0/1/2). "
            "Produktion: Pfad zu `Revit_Render/` im aktuellen Projekt-"
            "ordner, in den das pyRevit-Plugin schreibt."
        ),
    )
    generations_root: Path = Field(
        default=PROJECT_ROOT / "data" / "generations",
        description=(
            "Wurzelordner für gerenderte Iterationen. Default = "
            "data/generations (gemeinsam mit der CLI)."
        ),
    )
    phase_label: str = Field(
        default="runs",
        description="Subordner-Label unter generations_root, z.B. um Versuchsreihen zu trennen.",
    )
    reference_library_root: Path = Field(
        default=REFERENCE_LIBRARY_DIR,
        description=(
            "Wurzelordner der Referenzbibliothek (Projektfotos mit "
            "Metadaten im Dateinamen). Tier-0-RAG (`render_director.rag.library`) "
            "matcht Material + Tageszeit und reicht ähnliche Fotos als Stil-"
            "Referenz an den Generator. Fehlt der Ordner (z.B. Nutzer ohne "
            "lokale Fotos), läuft der Render einfach ohne Referenzen weiter. "
            "Überschreibbar via `RENDER_REFERENCE_LIBRARY_ROOT`."
        ),
    )

    model_config = SettingsConfigDict(
        env_prefix="RENDER_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
