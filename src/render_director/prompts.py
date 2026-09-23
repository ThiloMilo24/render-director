"""System-Prompt-Loading + User-Text-Templates.

Drei Verantwortlichkeiten:

1. **System-Prompts laden** (`load_director_system`, `load_validator_system`)
   — wählt das Exterior- oder Interior-Pendant basierend auf
   `view_type` aus `SceneMetadata`.
2. **Sprach-Direktive** (`LANG_DIRECTIVE`) — Validator-Output-Sprache
   ('de' Default, 'it' verfügbar).
3. **User-Text-Templates** (`build_director_user_text`,
   `build_validator_user_text`, `build_iteration_user_text`) — zentral,
   sodass Webapp und CLI denselben Wortlaut schicken. Werden bewusst als reine Templates
   gehalten (keine API-Calls), sodass sie unit-testbar bleiben.
"""
from __future__ import annotations

import json
import os
from typing import Literal, Optional

from render_director.clients import _ensure_dotenv
from render_director.paths import PROMPTS_DIR
from render_director.utils import MODE_LABELS, Mode, SceneBundle, SceneMetadata

Language = Literal["de", "it"]
ViewType = Literal["exterior", "interior"]

# Basis-Sprach-Direktive (reine Prosa-Sprache). Bewusst NICHT direkt an die
# Rollen gehängt — jede Rolle hat maschinen-geparste Teile, die NICHT
# übersetzt werden dürfen (Director: EN-Code-Block + FORWARD_ATTACHMENTS;
# Validator: JSON-Keys; Advisor: ACTION/GRUND/KONFIDENZ-Labels + Tokens).
# Deshalb die drei rollen-spezifischen Direktiven unten, die den jeweiligen
# Schutz explizit dazuschreiben. 'de' Default, 'it' für italienisch-
# sprachige Nutzer.
LANG_DIRECTIVE: dict[str, str] = {
    "de": "Antworte ausschließlich auf Deutsch.",
    "it": "Rispondi esclusivamente in italiano — non in tedesco.",
}


def director_lang_directive(language: Language) -> str:
    """Sprach-Direktive für den Director-Call.

    Nutzer-Prosa (der einleitende Vorspann) wird lokalisiert; der finale
    Bildgenerierungs-Prompt im ```-Code-Block bleibt IMMER Englisch (der
    Generator erwartet EN). Auch der FORWARD_ATTACHMENTS-Block ist ein
    fixes Token und bleibt unverändert.
    """
    if language == "it":
        return (
            "LINGUA: scrivi il tuo testo introduttivo rivolto all'utente in "
            "ITALIANO. Il prompt finale di generazione immagine nel blocco di "
            "codice ``` resta SEMPRE in inglese."
        )
    return (
        "SPRACHE: Schreibe deinen an den Nutzer gerichteten Vorspann auf "
        "DEUTSCH. Der finale Bildgenerierungs-Prompt im ```-Code-Block bleibt "
        "IMMER Englisch."
    )


def validator_lang_directive(language: Language) -> str:
    """Sprach-Direktive für den Validator-Call.

    Die fünf Report-Sektionen werden lokalisiert; der abschließende JSON-
    Score-Block behält Keys UND Struktur exakt bei (deutsche Keys wie
    `geometrie`/`material`/`anforderung` — der Parser hängt daran).
    """
    if language == "it":
        return (
            "LINGUA: scrivi le cinque sezioni del report in ITALIANO. Il "
            "blocco JSON di punteggio finale mantiene le CHIAVI e la struttura "
            "invariate (chiavi in tedesco come nello schema); solo i valori "
            "numerici cambiano."
        )
    return (
        "SPRACHE: Schreibe die fünf Report-Sektionen auf DEUTSCH. Der "
        "abschließende JSON-Score-Block behält Keys und Struktur unverändert "
        "(deutsche Keys laut Schema)."
    )


def advisor_lang_directive(language: Language) -> str:
    """Sprach-Direktive für den Director-Advisor-Call.

    Nur der Fließtext der `GRUND`-Zeile wird lokalisiert. Die Labels
    `ACTION`/`GRUND`/`KONFIDENZ` und die Tokens (regenerate/refine/stop,
    high/medium/low) bleiben unverändert — der Parser matcht sie exakt.
    """
    if language == "it":
        return (
            "LINGUA: scrivi il testo della riga GRUND in ITALIANO. Le etichette "
            "ACTION/GRUND/KONFIDENZ e i token (regenerate/refine/stop, "
            "high/medium/low) restano invariati."
        )
    return (
        "SPRACHE: Schreibe den Text der GRUND-Zeile auf DEUTSCH. Die Labels "
        "ACTION/GRUND/KONFIDENZ und die Tokens (regenerate/refine/stop, "
        "high/medium/low) bleiben unverändert."
    )


# Regionaler Default-Kontext der Director-Prompts. Die Prompt-Dateien
# enthalten den Platzhalter `{{DEFAULT_REGION}}`; er wird beim Laden durch
# `RENDER_DEFAULT_REGION` (aus `.env`) ersetzt, Default „Alpenraum".
DEFAULT_REGION_PLACEHOLDER = "{{DEFAULT_REGION}}"
DEFAULT_REGION = "Alpenraum"


def default_region() -> str:
    """Regionaler Default-Kontext für die Director-Prompts."""
    _ensure_dotenv()
    return os.environ.get("RENDER_DEFAULT_REGION", "").strip() or DEFAULT_REGION


def _read_prompt(name: str) -> str:
    """Liest eine Prompt-Datei frisch von Disk und setzt Platzhalter ein."""
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    return text.replace(DEFAULT_REGION_PLACEHOLDER, default_region())


def load_director_system(view_type: ViewType) -> str:
    """Liest den passenden Director-System-Prompt frisch von Disk.

    Exterior → `prompts/director_system.md` (Phase 0/2-Schiene).
    Interior → `prompts/director_interior_system.md` (Phase 1-Schiene).
    """
    name = "director_interior_system.md" if view_type == "interior" else "director_system.md"
    return _read_prompt(name)


def load_validator_system(view_type: ViewType) -> str:
    """Liest den passenden Validator-System-Prompt frisch von Disk.

    Exterior → `prompts/validator_system.md`.
    Interior → `prompts/validator_interior_system.md`.
    """
    name = "validator_interior_system.md" if view_type == "interior" else "validator_system.md"
    return _read_prompt(name)


def load_validator_adhoc_system() -> str:
    """Liest den Validator-System-Prompt für den Ad-hoc-Modus.

    Vision-only-Variante ohne BIM/Depth/ID-Passes: das User-Input-Bild
    ist die Geometrie-Ground-Truth. Nutzt bewusst das GLEICHE JSON-Score-
    Schema wie der Exterior-Validator (`VALIDATOR_SCORE_SCHEMA`), damit
    Parsing + Aggregation + UI unverändert greifen.
    """
    return _read_prompt("validator_adhoc_system.md")


def load_director_adhoc_system() -> str:
    """Liest den Director-Adhoc-System-Prompt von Disk.

    Ad-hoc-Modus: ein einzelnes Input-Bild (Enscape/Foto/Skizze/...), kein
    BIM, keine Mode-Auswahl, kein Geo-Kontext. Vision-only-Variante von
    `director_system.md`, destilliert auf das wesentliche Prompt-Format
    und die Grundprinzipien.
    """
    return _read_prompt("director_adhoc_system.md")


def load_advisor_system() -> str:
    """Liest den Director-Advisor-System-Prompt von Disk.

    Der Advisor ist view-type-unabhängig — er gibt nur die Iterations-
    Empfehlung (ACTION/GRUND/KONFIDENZ) aus, die je nach Validator-
    Report passt. Wenn später Exterior- vs Interior-spezifische
    Schwellen nötig werden, kann hier auf zwei Files aufgeteilt werden.
    """
    return _read_prompt("director_advisor_system.md")


_MODE_D_INSTRUCTION = (
    "\n"
    "WICHTIG — MODUS D (Kreative Exploration):\n"
    "In diesem Modus ist die Geometrie ABSICHTLICH NICHT geschützt. Der "
    "User ist in einer frühen Konzeptphase und möchte gezielt mit Form, "
    "Massing, Fassaden-Gliederung, Volumen oder räumlicher Komposition "
    "experimentieren. Du darfst (und sollst, wo es zur Nutzeranfrage "
    "passt) im EN-Prompt geometrische Vorschläge machen — z.B. Volumina "
    "addieren/subtrahieren, Fassaden-Rhythmus ändern, Dachform variieren, "
    "Material-Texturen frei vorschlagen. Der [GEOMETRY LOCK]-Block aus "
    "deinem System-Prompt entfällt in diesem Modus, oder wird durch einen "
    "[GEOMETRY EXPLORATION]-Block ersetzt, der die erlaubten/erwünschten "
    "Variationen explizit benennt. Andere Constraints (Materialien aus "
    "Meta, Standort-Kontext, Modus-passende Tonalität) bleiben gültig.\n"
)


def _format_geo_context_block(
    meta: SceneMetadata,
    site_analysis: Optional[str] = None,
) -> str:
    """Baut einen GEO-KONTEXT-Block aus meta.site_location + optional
    der vorgekachten Standort-Analyse (Wikimedia + OSM, Phase-5).

    Geeignet nur bei Exterior + Lat/Lon gesetzt. Sonst leerer String.

    Struktur:
      GEO-KONTEXT (aus Revit-SiteLocation):
        Koordinaten / Höhe / Zeitzone / Ort
      SITE-ANALYSE (optional, aus Wikimedia + OSM, falls Cache vorhanden):
        VEGETATION / TOPOGRAFIE / LICHT / ARCHITEKTUR-KONTEXT / ANTI-PATTERNS
    """
    if meta.view_type != "exterior":
        return ""
    sl = meta.site_location
    if sl is None or sl.latitude_deg is None or sl.longitude_deg is None:
        return ""
    lines = ["GEO-KONTEXT (aus Revit-SiteLocation):"]
    lines.append("  Koordinaten: {:.4f}° N, {:.4f}° E".format(
        sl.latitude_deg, sl.longitude_deg,
    ))
    if sl.elevation_m is not None:
        lines.append("  Höhe ü.M.:   {:.0f} m".format(sl.elevation_m))
    if sl.time_zone_utc_offset_h is not None:
        tz = sl.time_zone_utc_offset_h
        lines.append("  Zeitzone:    UTC{:+.0f}".format(tz))
    if sl.place_name:
        lines.append("  Ort:         {}".format(sl.place_name))
    block = "\n".join(lines) + "\n\n"

    if site_analysis and site_analysis.strip():
        block += (
            "SITE-ANALYSE (regional charakterisiert aus öffentlichen "
            "Quellen, lizenz-bereinigt):\n"
            + site_analysis.strip()
            + "\n\n"
        )
    return block


def _meta_dump_for_director(meta: SceneMetadata) -> str:
    """JSON-Dump der Meta für den Director, mit site_location entfernt
    (wird ueber den dedizierten GEO-KONTEXT-Block prominent gemacht,
    nicht im JSON-Blob versteckt)."""
    data = meta.model_dump()
    data.pop("site_location", None)
    return json.dumps(data, indent=2, ensure_ascii=False)


def build_director_user_text(
    bundle: SceneBundle,
    mode: Mode,
    user_request: str,
    language: Language = "de",
) -> str:
    """User-Text für den initialen (V1) Director-Call.

    Modus-Block + optionale Modus-D-Erweiterung + GEO-KONTEXT-Block inkl.
    optionaler SITE-ANALYSE aus dem snapshot-internen Cache.

    `language` steuert die Sprache des Nutzer-Vorspanns (der EN-Prompt im
    Code-Block bleibt sprachunabhängig Englisch).
    """
    mode_extra = _MODE_D_INSTRUCTION if mode == "D" else ""
    geo_block = _format_geo_context_block(bundle.meta, bundle.site_analysis)
    return (
        f"{director_lang_directive(language)}\n"
        "\n"
        f"MODUS: {mode} ({MODE_LABELS[mode]})\n"
        f"{mode_extra}"
        "\n"
        f"{geo_block}"
        "METADATEN (aus Revit/BIM):\n"
        f"{_meta_dump_for_director(bundle.meta)}\n"
        "\n"
        "NUTZERANFRAGE:\n"
        f"{user_request}\n"
        "\n"
        "Im Anhang: Enscape Beauty Render der aktuellen View.\n"
    )


def build_validator_user_text(
    bundle: SceneBundle,
    mode: Mode,
    user_request: str,
    final_prompt: str,
    language: Language = "de",
) -> str:
    """User-Text für den Validator-Call.

    ANHANG-Block reflektiert dynamisch, welche Bilder der
    Validator sieht: Beauty + ggf. Depth + ggf. Material-ID + ggf.
    Object-ID + Result (immer als letztes).

    Die ID-Pass-Beschreibung mit „arbitrary colors as region labels" ist
    kritisch — verhindert dass der Validator die Pass-Farben als
    erwartete Output-Farben missversteht (Phase-3-Spec-Pivot).
    """
    anhang_lines = [
        "Bild 1 = Original Enscape Beauty Render (Geometrie-Ground-Truth)"
    ]
    idx = 2
    if bundle.depth is not None:
        anhang_lines.append(
            "Bild {} = Depth Pass (räumlicher Anker — nutze ihn, "
            "um Geometrie nicht zu spekulieren)".format(idx)
        )
        idx += 1
    if bundle.material_id is not None:
        anhang_lines.append(
            "Bild {} = Material-ID-Pass aus Enscape. Arbitrary Farb-Labels "
            "pro Material-Region (NICHT die zu rendernden Farben). Prüfe "
            "bei Material-Bewertung, ob die Region-Grenzen im Result erhalten "
            "geblieben sind — Material-Bleed zwischen Regionen ist ein "
            "Issue.".format(idx)
        )
        idx += 1
    if bundle.object_id is not None:
        anhang_lines.append(
            "Bild {} = Object-ID-Pass (analog, pro Objekt-Instanz). "
            "Hilft beim Beurteilen, ob einzelne Objekte im Result intakt "
            "geblieben sind.".format(idx)
        )
        idx += 1
    anhang_lines.append(
        "Bild {} (letztes) = Generierter Nano-Banana-Render (zu prüfen)".format(idx)
    )

    return (
        f"{validator_lang_directive(language)}\n"
        "\n"
        "URSPRÜNGLICHE NUTZER-ANFORDERUNG:\n"
        f"{user_request}\n"
        "\n"
        f"MODUS: {mode} ({MODE_LABELS[mode]})\n"
        "\n"
        "BIM-METADATEN (Ground Truth für Materialien):\n"
        f"{json.dumps(bundle.meta.model_dump(), indent=2, ensure_ascii=False)}\n"
        "\n"
        "DIRECTOR-PROMPT (zum Kontext, was angefordert wurde):\n"
        f"{final_prompt}\n"
        "\n"
        "ANHANG:\n  "
        + "\n  ".join(anhang_lines)
        + "\n"
    )


def build_iteration_user_text(
    bundle: SceneBundle,
    mode: Mode,
    user_request: str,
    final_prompt_v1: str,
    validator_reply_v1: str,
    user_feedback: Optional[str] = None,
    language: Language = "de",
) -> str:
    """User-Text für den V2-Director-Call (Iterations-Planer).

    Director bekommt V1-Prompt + V1-Validator-Report + optionales
    User-Feedback und entscheidet `regenerate` vs `refine` (siehe
    `parse_director_recommendation`).

    Bilder (Beauty + V1-Render + ggf. Material-ID + Object-ID) werden
    separat als Image-Blocks in `call_director_iteration` angehängt;
    der ANHANG-Block hier reflektiert die tatsächliche Bild-Reihenfolge
    dynamisch je nach `bundle`-Inhalt.
    """
    mode_extra = _MODE_D_INSTRUCTION if mode == "D" else ""
    geo_block = _format_geo_context_block(bundle.meta, bundle.site_analysis)
    text = (
        f"{director_lang_directive(language)}\n"
        "\n"
        f"MODUS: {mode} ({MODE_LABELS[mode]})\n"
        f"{mode_extra}"
        "\n"
        f"{geo_block}"
        "URSPRÜNGLICHE NUTZERANFORDERUNG:\n"
        f"{user_request}\n"
        "\n"
        "VORHERIGER BILDGENERIERUNGS-PROMPT (V1):\n"
        "```\n"
        f"{final_prompt_v1}\n"
        "```\n"
        "\n"
        "VALIDATOR-REPORT zu V1:\n"
        f"{validator_reply_v1}\n"
    )
    if user_feedback:
        text += (
            "\nZUSÄTZLICHES NUTZER-FEEDBACK (parallel zum Validator):\n"
            f"{user_feedback}\n"
        )
    text += (
        "\nAUFGABE:\n"
        "Überarbeite den V1-Prompt anhand des Validator-Reports und der\n"
        "Nutzer-Wünsche. Adressiere maximal **zwei Top-Issues** (Faustregel:\n"
        "das ✗-Issue plus eine ⚠-Sache). Gib den neuen Bildgenerierungs-\n"
        "Prompt im üblichen Code-Block aus.\n"
        "\n"
        "Die Wahl regenerate-vs-refine triffst NICHT du — das macht der\n"
        "Director-Advisor in einem separaten Schritt. Du schreibst nur den\n"
        "Prompt, der dann je nach User-Klick mit Beauty (regenerate) oder\n"
        "V1-Output (refine) an den Generator geht.\n"
    )

    # ANHANG dynamisch — Bild 1+2 immer (Beauty + V1-Result), 3+ optional.
    anhang_lines = [
        "Bild 1 = Original Enscape Beauty Render (Geometrie-Ground-Truth)",
        "Bild 2 = V1-Render (das Resultat, das der Validator kritisiert hat)",
    ]
    idx = 3
    if bundle.site_reference is not None:
        marker_hint = (
            " Der rote Pin markiert die geplante Gebaeude-Position auf dem "
            "Grundstueck — beschreibe die Umgebung relativ dazu."
            if bundle.meta.site_reference_marker is not None
            else ""
        )
        anhang_lines.append(
            "Bild {} = REAL-WORLD-Standort-Referenz (Drohne / Google-Maps / "
            "Site-Visit-Foto) — Wahrheit fuer Vegetation, Umgebung, "
            "Bergsilhouetten, Atmosphaere. KEINE Komposition uebernehmen, "
            "der Render hat seine eigene Komposition aus Bild 1.{}".format(
                idx, marker_hint,
            )
        )
        idx += 1
    if bundle.material_id is not None:
        anhang_lines.append(
            "Bild {} = Material-ID-Pass (arbitrary Farb-Labels pro Material-"
            "Region — NICHT als zu rendernde Farben verstehen; nutze sie um "
            "Material-Grenzen im neuen Prompt verbal-präzise zu beschreiben)"
            .format(idx)
        )
        idx += 1
    if bundle.object_id is not None:
        anhang_lines.append(
            "Bild {} = Object-ID-Pass (analog, pro Objekt-Instanz)".format(idx)
        )
        idx += 1
    text += "\nANHANG:\n  " + "\n  ".join(anhang_lines) + "\n"
    return text
