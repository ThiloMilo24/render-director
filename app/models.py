"""Pydantic-Schemas für Request/Response-Bodies.

Bewusst flach gehalten — Webapp-State-Komplexität gehört ins
Frontend, das Backend liefert das Roh-Material (Snapshot-Metadaten,
Bild-URLs, Iteration-Output).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class SnapshotSummary(BaseModel):
    """Listing-Eintrag pro Snapshot. Genug für eine Sidebar-Auswahl."""

    view: str = Field(description="Snapshot-Ordner-Name = View-Name")
    project_name: str
    view_type: Literal["exterior", "interior"]
    has_depth: bool
    has_reference: bool
    n_runs: int = Field(description="Anzahl bisheriger Iterationen für diese View")


class SnapshotDetail(SnapshotSummary):
    """Detail-Ansicht eines Snapshots.

    `beauty_url` / `depth_url` / `reference_url` sind relative Pfade,
    die der StaticFiles-Mount unter `/files/snapshots/` ausliefert.
    `meta` ist der rohe Inhalt von `meta.json` als dict.
    """

    beauty_url: str
    depth_url: Optional[str] = None
    reference_url: Optional[str] = None
    material_id_url: Optional[str] = None
    object_id_url: Optional[str] = None
    site_reference_url: Optional[str] = None
    meta: dict


class IterationRequest(BaseModel):
    """POST-Body für `/iterate`.

    `iteration_type` bestimmt den Pipeline-Pfad:
    - `initial` — V1-Lauf, parent_run_id muss None sein.
    - `regenerate` — V2 vom Beauty mit neuem Director-Prompt.
    - `refine` — V2 vom vorherigen Output (Edit-Anweisung).
    """

    snapshot: str = Field(description="View-Name = Snapshot-Ordner-Name")
    user_prompt: str
    mode: Literal["A", "B", "C", "D"] = "A"
    iteration_type: Literal["initial", "regenerate", "refine"] = "initial"
    parent_run_id: Optional[str] = None
    previous_run_dir: Optional[Path] = Field(
        default=None,
        description=(
            "Run-Dir des vorherigen Runs (für regenerate/refine). Liest "
            "result.png + final_prompt.txt + validator_reply.md daraus."
        ),
    )
    user_feedback: Optional[str] = None
    validator_language: Literal["de", "it"] = "de"
    generator_model: str = Field(
        default="gemini-2.5-flash-image",
        description=(
            "Generator-Backend. 'gemini-2.5-flash-image' = NB1 (Default), "
            "'gemini-3.1-flash-image-preview' = NB2 (mit Thinking), "
            "'gpt-image-1' = GPT (OpenAI, input_fidelity=high)."
        ),
    )
    generator_thinking_budget: Optional[int] = Field(
        default=None,
        description=(
            "Nur für NB2/Gemini-3. None = kein Thinking-Config (NB1-Default). "
            "0 = Thinking aus. -1 = Dynamic-Budget (in UI 'high')."
        ),
    )
    attachment_images: list[Any] = Field(
        default_factory=list,
        description=(
            "User-Per-Turn-Bild-Attachments (PIL.Image-Objekte). Max 3 "
            "Stück, werden vom Director gesehen und ggf. an Generator "
            "weitergeleitet (siehe parse_forward_attachments). Typed als "
            "Any weil PIL nicht Pydantic-nativ ist."
        ),
    )
    attachment_description: Optional[str] = Field(
        default=None,
        description="Freitext-Beschreibung der Attachments vom User.",
    )
    use_reference_library: bool = Field(
        default=False,
        description=(
            "Tier-0-Referenzbibliothek nutzen: bei Exterior-Renders ähnliche "
            "Fotos aus der Referenzbibliothek (Match auf Material + Tageszeit) als Stil-"
            "Anker an den Generator geben. Augment — füllt nur auf, wenn der "
            "User weniger eigene Referenzen mitgibt."
        ),
    )
    reference_project_tag: Optional[str] = Field(
        default=None,
        description=(
            "Optionaler Projekt-Tag (Freitext). Passt er zu einem Ordner in "
            "der Bildbibliothek, werden dessen Fotos bevorzugt als Referenz "
            "gezogen — überschreibt den BIM-Projektnamen. Nur wirksam, wenn "
            "use_reference_library aktiv ist."
        ),
    )
    project_dir: Optional[str] = Field(
        default=None,
        description=(
            "URL-driven Project-Scoping. Wenn gesetzt: das Snapshot wird "
            "aus diesem Pfad geladen statt aus settings.snapshot_root. "
            "Kommt aus dem pyRevit-Render-Tool-Button via Query-Param."
        ),
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)


class IterationResponse(BaseModel):
    """Antwort auf `/iterate`. Alles, was die Chat-UI rendern muss."""

    run_id: str
    run_dir: str
    result_url: str
    final_prompt: str
    director_reply: str
    validator_reply: str
    final_score: Optional[float]
    section_means: dict[str, Optional[float]]
    iteration_type: str
    parent_run_id: Optional[str]
    # DEPRECATED 2026-05-27: ersetzt durch advisor_* (siehe unten).
    # Bleibt fuer Backward-Kompat von alten Run-Artefakten.
    director_recommendation: Optional[str]
    director_recommendation_reason: Optional[str]
    # Director-Advisor — die eigentliche Iterations-Empfehlung seit
    # 2026-05-27. Wird nach Validator generiert und ist im UI vor der
    # naechsten User-Entscheidung sichtbar.
    advisor_action: Optional[str] = None  # "regenerate" | "refine" | "stop"
    advisor_reason: Optional[str] = None
    advisor_confidence: Optional[str] = None  # "high" | "medium" | "low"
