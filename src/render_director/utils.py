"""Datenstrukturen und Hilfsfunktionen ohne API-Calls.

Scene-Metadaten + Bundle-Loader, Bild-Encoding für Claude-Vision,
Run-Ordner + `inputs.json`, Parser für Director-, Advisor- und
Validator-Antworten sowie die Score-Aggregation. Bewusst nur PIL- und
Pydantic-Abhängigkeiten, damit alles ohne SDKs testbar bleibt.
"""
from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from PIL import Image
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Scene-Metadaten + -Bundle
# ---------------------------------------------------------------------------

class SiteContext(BaseModel):
    """Status des Enscape-Site-Context-Features für diese View.

    Wenn aktiv, sind Nachbargebäude (graue OSM-Volumen), Straßen,
    Vegetation und Topografie schon im Beauty Render enthalten — der
    Director soll sie als echten Kontext respektieren, nicht als
    Artefakt wegoptimieren.
    """

    active: bool = False
    source: Literal["enscape_osm", "manual", "none"] = "none"
    neighbors_visible: bool = False
    notes: str = ""


class SiteLocationInfo(BaseModel):
    """Geo-Referenz aus Revit's `doc.SiteLocation` (Phase 5, 2026-05-27).

    Alle Felder optional - bei alten meta.json Files oder Projekten ohne
    gesetzten Standort bleibt das Feld leer. Lat/Lon in Dezimalgrad
    (positiv = Nord/Ost, negativ = Süd/West).
    """
    latitude_deg: Optional[float] = None
    longitude_deg: Optional[float] = None
    elevation_m: Optional[float] = None
    time_zone_utc_offset_h: Optional[float] = None
    place_name: Optional[str] = None


class SiteReferenceMarker(BaseModel):
    """User-gesetzter Pin auf dem Site-Reference-Bild (Phase 5, 2026-05-28).

    Sagt dem Director wo auf der Drohne / Maps-Screenshot das geplante
    Gebaeude steht. Wird beim Director-Call als roter Pin auf eine Kopie
    des Bildes eingebrannt — der Director sieht es visuell und kann die
    Umgebung relativ zum Pin verbal beschreiben.

    Koordinaten in normalisierten Bild-Prozent (0.0–1.0), x von links,
    y von oben.
    """
    x_pct: float = Field(ge=0.0, le=1.0)
    y_pct: float = Field(ge=0.0, le=1.0)


class SceneMetadata(BaseModel):
    """BIM-/Revit-Metadaten zu einer einzelnen View.

    Felder bewusst flach gehalten — Phase 0 schreibt diese `meta.json`
    manuell. Ab Phase 2 wird das aus Revit heraus befüllt.

    `view_type` schaltet die Schiene (Exterior- vs Interior-Prompts +
    -Schema). `furniture_state` und `program_type` sind nur für
    `view_type='interior'` relevant — bei Exterior werden sie ignoriert.
    Alle drei Felder haben Default-Werte, sodass bestehende Phase-0-
    `meta.json`-Files non-breaking geladen werden können.
    """

    project_name: str
    project_type: str = Field(
        description="z.B. 'Wohnbau', 'Hotel', 'öffentliches Gebäude', 'Weinkellerei'"
    )
    location: str = Field(description="Ort, z.B. 'Innsbruck, Österreich'")
    facade_orientation: str = Field(
        description="Himmelsrichtung der Hauptfassade, z.B. 'Süd-Südwest'"
    )
    materials: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "BIM-Materialien als freies dict. Exterior-Beispiel: "
            "{'Fassade': 'Sichtbeton', 'Dach': 'Zink Stehfalz'}. "
            "Interior-Beispiel: {'Wandbelag': 'Kalkputz', "
            "'Deckenfinish': 'Eichenholz-Paneel', 'Bodenbelag': 'Naturstein'}."
        ),
    )
    camera_focal_length_mm: float
    camera_height_m: float
    time_of_day: str = Field(
        description="z.B. 'Vormittag', 'goldene Stunde', 'klarer Mittag'"
    )
    site_context: SiteContext = Field(
        default_factory=SiteContext,
        description="Enscape-Site-Context-Status (OSM-Umgebung). Default: nicht aktiv. Exterior-spezifisch.",
    )
    # --- View-Type-Schalter + Interior-spezifische Felder -------------------
    view_type: Literal["exterior", "interior"] = Field(
        default="exterior",
        description=(
            "Schaltet die Pipeline-Schiene: 'exterior' lädt "
            "director_system.md + validator_system.md + "
            "VALIDATOR_SCORE_SCHEMA. 'interior' lädt entsprechend die "
            "_interior-Pendants. Default 'exterior' für Phase-0-Kompatibilität."
        ),
    )
    furniture_state: Literal["placeholder", "final"] = Field(
        default="placeholder",
        description=(
            "Nur für view_type='interior'. 'placeholder' = Director darf "
            "Möbel ersetzen/ergänzen (Mannequins, Enscape-Stock, leere "
            "Räume). 'final' = Director muss Möbel exakt erhalten, nur "
            "Material/Licht enhancen. Semantische Entscheidung des Scene-"
            "Vorbereiters, unabhängig von der technischen Herkunft."
        ),
    )
    program_type: Literal[
        "lobby", "wohnraum", "hotelzimmer", "restaurant", "kueche", "spa",
        "bar", "konferenz", "sauna", "sonstiges",
    ] = Field(
        default="sonstiges",
        description=(
            "Nur für view_type='interior'. Programm-Typ des Raumes — "
            "steuert das Modus-A-Default-Lichtprofil im Director "
            "(Lobby/Wohnraum/Hotelzimmer/Restaurant/Kueche/Spa haben "
            "dedizierte Profile, die anderen + 'sonstiges' fallen auf "
            "einen generischen Innen-Default zurück). Wird durch "
            "explizite tageszeit/lichtszenario in meta.json überschrieben."
        ),
    )
    notes: str = ""
    # Phase-5-Erweiterung 2026-05-27: Geo-Referenz aus Revit-SiteLocation
    # (pyRevit-Export schreibt's; alte meta.json bekommen None).
    site_location: Optional[SiteLocationInfo] = Field(
        default=None,
        description=(
            "Geo-Referenz aus Revit's doc.SiteLocation: Lat/Lon, Hoehe, "
            "Zeitzone, ggf. Place-Name. None bei alten meta.json oder "
            "Projekten ohne gesetzten Standort. Director nutzt das als "
            "LOCATION_CONTEXT-Block (Region-Stimmigkeit, Sonnenstand)."
        ),
    )
    # Phase-5-Erweiterung 2026-05-28: Pin auf dem Site-Reference-Bild.
    site_reference_marker: Optional[SiteReferenceMarker] = Field(
        default=None,
        description=(
            "Optionaler Pin (x_pct/y_pct) der dem Director sagt wo auf "
            "der Site-Reference das Gebaeude steht. Wird zur Render-Zeit "
            "als roter Pin auf eine Kopie des Bildes eingebrannt."
        ),
    )


Mode = Literal["A", "B", "C", "D"]

MODE_LABELS: dict[Mode, str] = {
    "A": "Kundenpräsentation",
    "B": "Wettbewerb",
    "C": "Stimmung erkunden",
    "D": "Kreative Exploration (geometrie-frei)",
}


# Iterations-Typen
#   initial    = erste Generation in einer Session
#   regenerate = neue Generation vom Original-Beauty (Geometrie-treu)
#   refine     = Edit auf dem letzten Output (Detail-treu, Drift-Risiko)
#   inpaint    = maskierter lokaler Edit auf dem letzten Output; nur das
#                gpt-image-Backend (native Maske), Director wird übersprungen
#                (User-Text = Generator-Prompt). Nur auf der Ad-hoc-Schiene.
IterationType = Literal["initial", "regenerate", "refine", "inpaint"]


@dataclass
class SceneBundle:
    """Alles, was aus Enscape + Revit zu einer View kommt.

    `reference` (optional) ist ein professionelles Pro-Renderer-Referenz-
    Bild derselben Scene, geladen wenn `*_Reference.{png,jpg,jpeg}` im
    Scene-Ordner liegt. Wird nur vom initialen Director-Call (Pfad B
    der Phase-2-Referenz-Strategie) als Stimmungs-/Stil-Vorlage genutzt
    — explizit NICHT als Geometrie-Anker (Geometrie kommt vom Beauty).

    `material_id` und `object_id` (optional) sind Enscape-Pass-Bilder,
    die Material- bzw. Object-Regionen mit arbitrary Farben markieren.
    Ab Phase 4.5 als **Spatial-Anker** an Director + Validator geschickt
    (analytische Vision-Modelle), aber NICHT an den Generator (Risiko,
    dass Nano Banana die ID-Farben als zu rendernde Farben interpretiert
    — siehe Phase-3-Spec-Pivot 2026-05-18, Pixel-zu-Material-Mapping
    falsifiziert). Director übersetzt die Regionen verbal in seinen
    EN-Prompt.
    """

    scene_id: str
    beauty: Image.Image
    depth: Optional[Image.Image]
    material_id: Optional[Image.Image]
    object_id: Optional[Image.Image]
    reference: Optional[Image.Image]
    meta: SceneMetadata
    path: Path
    # Phase-5 (2026-05-27): vorgekachte Standort-Analyse aus
    # `<scene_dir>/site_analysis.md` (Wikimedia + OSM, lizenz-sauber).
    # None wenn Datei nicht da ist — der Director-Prompt schaltet den
    # SITE-ANALYSE-Block dann stumm.
    site_analysis: Optional[str] = None
    # Phase-5 (2026-05-28): User-eigene Standort-Referenz (Drohne, Google-
    # Maps/Earth-Screenshot, Site-Visit-Foto). Strukturell separat von
    # `reference` (Pro-Renderer-Stimmungs-Vorlage) — andere Semantik:
    # site_reference ist REAL-WORLD-Wahrheit fuer Umgebung/Vegetation,
    # Komposition wird explizit NICHT uebernommen. Datei-Konvention
    # `<scene_dir>/site_reference.{png|jpg|jpeg}` oder via _find_pass
    # auch `<prefix>_SiteReference.*`.
    site_reference: Optional[Image.Image] = None


_IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def _normalize_pass_token(s: str) -> str:
    """Lowercase + entfernt `-` und `_` für Suffix-Vergleiche.

    Damit `material_id`, `material-id`, `MaterialID`, `materialid`
    alle auf denselben Vergleichs-Token `materialid` reduziert werden —
    Enscape exportiert die Material-ID-Spalte mal so, mal so.
    """
    return s.lower().replace("_", "").replace("-", "")


def _find_pass(scene_dir: Path, stem: str) -> Optional[Path]:
    """Sucht eine Pass-Datei `<stem>.{png,jpg,jpeg}` in `scene_dir`.

    Match-Reihenfolge:

    1. **Exakter Stem-Match** (case-insensitive): `beauty.jpg`,
       `material_id.png`. Gewinnt immer, falls vorhanden.
    2. **Suffix-Match mit Präfix** für Enscape-Default-Exporte:
       `HotelLobby_Beauty.jpg`, `ProjektX_MaterialID.png`,
       `Lobby-Depth.png`. Erkannt wird `<irgendwas>[_-]<stem>` mit
       `-`/`_`-normalisiertem, case-insensitivem Vergleich, sodass
       `_material_id`, `_material-id`, `_MaterialID`, `_materialid`
       alle gegen Target `material_id` matchen.

    Mehrere Suffix-Matches im selben Ordner (`View1_Beauty.jpg` +
    `View2_Beauty.jpg`) sind ein harter `RuntimeError` — stillschweigend
    einen davon zu wählen wäre echtes Chaos. Der Exakt-Match-Pfad
    kollidiert per File-System-Eindeutigkeit nicht mit sich selbst.
    """
    target_norm = _normalize_pass_token(stem)
    target_lower = stem.lower()

    exact: Optional[Path] = None
    suffix_matches: list[Path] = []

    for p in scene_dir.iterdir():
        if not (p.is_file() and p.suffix.lower() in _IMAGE_EXTS):
            continue
        p_stem = p.stem
        if p_stem.lower() == target_lower:
            exact = p  # exakt schlägt Suffix — weitere Kandidaten egal
            continue
        # Suffix-Match: '_' oder '-' irgendwo im Stem, rechter Teil
        # normalisiert == Target. Mehrere Trenner werden alle probiert
        # (z.B. `Hotel_Lobby_Beauty` muss am letzten `_` greifen).
        for i, ch in enumerate(p_stem):
            if ch in "_-" and _normalize_pass_token(p_stem[i + 1:]) == target_norm:
                suffix_matches.append(p)
                break

    if exact is not None:
        return exact

    if len(suffix_matches) > 1:
        candidates = sorted(p.name for p in suffix_matches)
        raise RuntimeError(
            f"Mehrdeutig: mehrere Pass-Kandidaten für '{stem}' in {scene_dir}: "
            f"{candidates}. Behalte nur den gewünschten oder benenne kanonisch "
            f"({stem}.jpg/.png) um — stillschweigend einen zu wählen wäre Chaos."
        )
    if suffix_matches:
        return suffix_matches[0]
    return None


def _adapt_pyrevit_meta(raw: dict) -> dict:
    """Übersetzt eine pyRevit-Export-`meta.json` in Phase-4-`SceneMetadata`-Shape.

    Hintergrund: das pyRevit-Skript (`ExportScene.pushbutton`) wurde in
    Phase 3 entwickelt und schreibt ein eigenes Schema (project_info-
    Subdict, view-Subdict, materials als flache Liste). `SceneMetadata`
    aus Phase 0/1/2 erwartet project_name/location/materials-Dict/
    camera_focal_length_mm/etc. auf der Top-Ebene.

    Adapter mappt was er kann + füllt fehlende Felder mit Defaults, die
    der Director sinnvoll interpretieren kann. Manuelle Felder (z.B.
    facade_orientation, time_of_day) bekommen `"Unbekannt"`-Defaults
    und können vom User in der meta.json nach-editiert werden.

    View-Type wird aus dem View-Namen geheuristiziert — Innenraum-
    Trigger sind Wörter wie 'innen', 'interior', 'lobby', 'küche',
    'spa', 'bar', 'zimmer'. Default 'exterior'.

    Detection: pyRevit-Shape erkennbar an `view_name_raw` + `project_info`.
    Wenn nicht-pyRevit-Shape: durchreichen unverändert.
    """
    if "view_name_raw" not in raw or "project_info" not in raw:
        return raw

    pi = raw.get("project_info") or {}
    view = raw.get("view") or {}

    view_name = (view.get("view_name") or raw.get("view_name_raw") or "").lower()
    interior_triggers = (
        "innen", "interior", "indoor", "lobby", "küche", "kueche",
        "spa", "bar", "zimmer", "room", "restaurant", "sauna",
        "wohnzimmer", "wohnraum", "konferenz",
    )
    inferred_view_type = (
        "interior" if any(t in view_name for t in interior_triggers) else "exterior"
    )

    materials_raw = raw.get("materials") or []
    if isinstance(materials_raw, list):
        materials_dict = {
            "Material_{:02d}".format(i + 1): name
            for i, name in enumerate(materials_raw)
        }
    elif isinstance(materials_raw, dict):
        materials_dict = materials_raw
    else:
        materials_dict = {}

    return {
        "project_name": pi.get("Name") or raw.get("scene_id") or "Unbekannt",
        "project_type": pi.get("BuildingName") or "Unbekannt",
        "location": pi.get("Address") or "Unbekannt",
        "facade_orientation": "Unbekannt",
        "materials": materials_dict,
        "camera_focal_length_mm": 24.0,
        "camera_height_m": view.get("camera_height_m") or 1.6,
        "time_of_day": "Vormittag",
        "view_type": raw.get("view_type") or inferred_view_type,
        "furniture_state": raw.get("furniture_state") or "placeholder",
        "program_type": raw.get("program_type") or "sonstiges",
        # site_location durchreichen falls vorhanden (pyRevit schreibt's
        # ab Phase 5). None bei alten Exporten.
        "site_location": raw.get("site_location"),
        "notes": (
            "Automatisch aus pyRevit-Export adaptiert. Bitte project_type / "
            "location / facade_orientation / time_of_day bei Bedarf direkt "
            "in meta.json überschreiben — Pipeline respektiert dann deine "
            "manuellen Werte."
        ),
    }


def load_scene_bundle(scene_dir: Path | str) -> SceneBundle:
    """Lädt ein Scene-Bundle aus einem Ordner.

    Erwartet: `beauty.{png|jpg|jpeg}` + `meta.json` (pflicht),
    `depth.{...}` + `material_id.{...}` (optional). Datei-Endung
    ist egal (Enscape exportiert per Default als JPG).

    Akzeptiert zusätzlich Enscape-Default-Exporte mit Scene-Namen-Präfix:
    `<Projekt>_Beauty.jpg`, `<Projekt>_Depth.png`, `<Projekt>_MaterialID.png`
    (Suffix case-insensitiv und `-`/`_`-tolerant). Exakte Namen
    (`beauty.jpg` etc.) haben Vorrang, falls beide existieren. Siehe
    `_find_pass` für die Match-Regeln.

    Erkennt automatisch pyRevit-Export-`meta.json` und adaptiert sie via
    `_adapt_pyrevit_meta` — Phase-3-Exports laden ohne manuelle Konversion.
    """
    scene_dir = Path(scene_dir)

    beauty_path = _find_pass(scene_dir, "beauty")
    if not beauty_path:
        raise FileNotFoundError(
            f"beauty.png/jpg/jpeg (oder <prefix>_Beauty.*) fehlt in {scene_dir} "
            f"(gefunden: {[p.name for p in scene_dir.iterdir()]})"
        )

    meta_path = scene_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"meta.json fehlt in {scene_dir}")

    beauty = Image.open(beauty_path).convert("RGB")

    depth_path = _find_pass(scene_dir, "depth")
    depth = Image.open(depth_path).convert("RGB") if depth_path else None

    mid_path = _find_pass(scene_dir, "material_id")
    material_id = Image.open(mid_path).convert("RGB") if mid_path else None

    oid_path = _find_pass(scene_dir, "object_id")
    object_id = Image.open(oid_path).convert("RGB") if oid_path else None

    ref_path = _find_pass(scene_dir, "reference")
    reference = Image.open(ref_path).convert("RGB") if ref_path else None

    site_ref_path = _find_pass(scene_dir, "site_reference")
    site_reference = Image.open(site_ref_path).convert("RGB") if site_ref_path else None

    raw_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    adapted = _adapt_pyrevit_meta(raw_meta)
    meta = SceneMetadata.model_validate(adapted)

    # Phase 5: vorgekachte Standort-Analyse aus site_analysis.md lesen
    # (wird vom site_analyzer befüllt). Nur Body extrahieren, YAML-Header
    # mit Source-Logs übergehen.
    site_analysis = _read_site_analysis_cache(scene_dir)

    return SceneBundle(
        scene_id=scene_dir.name,
        beauty=beauty,
        depth=depth,
        material_id=material_id,
        object_id=object_id,
        reference=reference,
        meta=meta,
        path=scene_dir,
        site_analysis=site_analysis,
        site_reference=site_reference,
    )


def render_site_reference_marker(
    image: Image.Image,
    x_pct: float,
    y_pct: float,
) -> Image.Image:
    """Brennt einen roten Pin (Kreis + Fadenkreuz) bei (x_pct, y_pct) ins Bild.

    Returns eine neue Kopie — das Original bleibt unveraendert, weil das
    via Webapp-StaticFiles auch im Browser angezeigt wird.

    Pin-Design:
    - Aeusserer halbtransparenter roter Glow-Ring (Sichtbarkeit auf hellen
      und dunklen Hintergruenden)
    - Innerer satter roter Kreis mit weisser Outline (Pop-Out)
    - Kurze Fadenkreuz-Linien (Praezision der Position)

    Pin-Groesse skaliert mit min(width, height) — 3% des kleineren Bild-
    masses fuer den Hauptkreis, sodass es auch auf groesseren Drohnen-
    Fotos sichtbar bleibt ohne zu dominieren.
    """
    from PIL import ImageDraw

    img = image.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    w, h = img.size
    cx = int(round(x_pct * w))
    cy = int(round(y_pct * h))

    # Pin-Geometrie skaliert mit min-Seite.
    base = max(8, int(min(w, h) * 0.03))
    glow_r = int(base * 1.8)
    pin_r = base
    pin_outline = max(2, base // 5)
    cross_len = int(base * 1.5)
    cross_w = max(2, base // 6)

    # Glow-Ring (transluzent)
    draw.ellipse(
        [cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r],
        fill=(255, 60, 60, 70),
    )
    # Pin (satt rot mit weisser Outline)
    draw.ellipse(
        [cx - pin_r, cy - pin_r, cx + pin_r, cy + pin_r],
        fill=(220, 30, 30, 255),
        outline=(255, 255, 255, 255),
        width=pin_outline,
    )
    # Fadenkreuz — weiss innen, rote Anschluesse aussen
    draw.line(
        [cx - cross_len, cy, cx + cross_len, cy],
        fill=(255, 255, 255, 230), width=cross_w,
    )
    draw.line(
        [cx, cy - cross_len, cx, cy + cross_len],
        fill=(255, 255, 255, 230), width=cross_w,
    )

    out = Image.alpha_composite(img, overlay).convert("RGB")
    return out


def _read_site_analysis_cache(scene_dir: Path) -> Optional[str]:
    """Liest `site_analysis.md` Body falls vorhanden. YAML-Frontmatter
    (Source-Logs) wird abgetrennt — der Director sieht nur den
    Analyse-Text, nicht die Logs."""
    cache_path = scene_dir / "site_analysis.md"
    if not cache_path.is_file():
        return None
    raw = cache_path.read_text(encoding="utf-8")
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\n").strip() or None
    return raw.strip() or None


# ---------------------------------------------------------------------------
# Bild-Encoding für die APIs
# ---------------------------------------------------------------------------

def image_to_base64(img: Image.Image, format: str = "PNG") -> str:
    """PIL-Image → base64-String (roh, ohne Resizing). Für Edge-Cases."""
    buf = io.BytesIO()
    img.save(buf, format=format)
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


# Lange-Kante-Cap für Bilder, die an Claude-Vision gehen (Director/Validator/
# Advisor). Anthropic rechnet Vision-Tokens ~nach Bildfläche ab (≈ w·h/750,
# nach internem Downscale auf max. ~1568 px). 1024 px statt 1568 spart grob
# die Hälfte der Bild-Input-Tokens — und die sind der Haupt-Kostentreiber bei
# Claude (3 Vision-Calls/Run × je 2 Bilder). Für ein Urteil (Geometrie/Material/
# Anforderung) reichen 1024 px locker; der Generator bekommt weiter die volle
# Auflösung (er läuft NICHT über diese Funktion). Wert bei Bedarf anheben,
# falls die Material-Bewertung an Detailschärfe verliert.
CLAUDE_VISION_MAX_EDGE = 1024


def to_anthropic_image_block(
    img: Image.Image,
    *,
    max_edge: int = CLAUDE_VISION_MAX_EDGE,
    quality: int = 90,
) -> dict:
    """Skaliert ein Bild auf `max_edge` px (lange Kante) und liefert es
    als fertiger Anthropic-API-Image-Content-Block (JPEG-base64).

    Hintergrund: Anthropic limitiert Bilder auf 5 MB pro Block und rechnet
    Vision-Tokens ~nach Bildfläche ab. `max_edge` steuert damit direkt die
    Kosten (siehe `CLAUDE_VISION_MAX_EDGE`) — Default 1024 px ist ein bewusster
    Kosten/Qualitäts-Kompromiss für die Urteils-Calls.
    """
    img = img.copy()
    long_edge = max(img.size)
    if long_edge > max_edge:
        scale = max_edge / long_edge
        new_size = (int(img.size[0] * scale), int(img.size[1] * scale))
        img = img.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": data,
        },
    }


# Label-Texte für die ID-Passes. Sind zentralisiert hier, damit
# Director- und Validator-Calls die gleiche Erklärung schicken und der
# Wortlaut bei Änderungen an einer Stelle gepflegt wird. Wichtig: die
# Phrasierung „arbitrary colors as region labels" ist kritisch — sie
# verhindert, dass Vision-Modelle die ID-Pass-Farben als „so soll's
# aussehen" missverstehen (siehe Phase-3-Spec-Pivot 2026-05-18).

_MATERIAL_ID_LABEL = (
    "Material-ID-Pass aus Enscape. Jede Farbe markiert eine andere "
    "Material-Region; die Farben selbst sind ARBITRARY LABELS, NICHT die "
    "zu rendernden Material-Farben. Nutze die Regionen, um Material-"
    "Grenzen im Output verbal präzise zu beschreiben (z.B.: das linke "
    "Wandsegment [Region A im Material-Pass] = Kalkputz, das mittlere "
    "[Region B] = Eichenholz-Paneel)."
)

_OBJECT_ID_LABEL = (
    "Object-ID-Pass aus Enscape. Analog zum Material-Pass, aber pro "
    "Objekt-Instanz statt pro Material. Hilft, Objekt-Grenzen verbal "
    "zu fassen (z.B.: der vordere Stuhl [Region X] vs der hintere "
    "Stuhl [Region Y])."
)


def _attachment_blocks(
    attachments: list,
    start_index: int,
    description: Optional[str] = None,
) -> tuple[list[dict], list[str], int]:
    """Helper: Image-Blocks + ANHANG-Textzeilen für User-Per-Turn-Attachments.

    `attachments` ist eine Liste von PIL-Images. `description` ist eine
    optionale freie Beschreibung des Users, was die Bilder zeigen (z.B.
    „der Stuhl links und die Wand-Schraffur sollen so aussehen").

    Wird vom Director gesehen + verbal beschrieben. Optional vom
    Generator gesehen, wenn der Director im Reply einen
    FORWARD_ATTACHMENTS-Block setzt (siehe parse_forward_attachments).
    """
    blocks: list[dict] = []
    lines: list[str] = []
    idx = start_index
    for att in attachments:
        blocks.append(to_anthropic_image_block(att))
        label = (
            "User-Per-Turn-Attachment Nr. {} — visueller Hinweis vom User. "
            "Wenn der User einen Beschreibungstext mitgegeben hat (siehe "
            "USER-ATTACHMENT-BESCHREIBUNG unten), bezieht er sich auf diese "
            "Bilder. Du darfst entscheiden, ob dieses Bild dem Generator "
            "verbal beschrieben werden soll (Default) oder ob es 1:1 als "
            "zusätzliches Eingabe-Bild an den Generator weitergegeben "
            "werden soll (siehe FORWARD_ATTACHMENTS-Anweisung unten).".format(idx - start_index + 1)
        )
        lines.append("Bild {} = {}".format(idx, label))
        idx += 1
    return blocks, lines, idx


_FORWARD_ATTACHMENTS_INSTRUCTION = (
    "\n"
    "ANHANG-FORWARDING-OPTION:\n"
    "Du hast vom User {n_att} Per-Turn-Attachment(s) bekommen. Standard: "
    "du beschreibst sie verbal im EN-Prompt (Beispiel: a chair with a "
    "curved wooden back, cream upholstery, similar to the reference). "
    "Wenn du "
    "aber meinst, dass ein Attachment dem Image-Generator (Nano Banana) "
    "direkt als zusätzliches Eingabe-Bild gezeigt werden sollte (z.B. weil "
    "die visuelle Information zu komplex für eine verbale Beschreibung "
    "ist), füge ans Ende deiner Antwort eine einzelne Zeile ein:\n"
    "\n"
    "    FORWARD_ATTACHMENTS: [1, 2]\n"
    "\n"
    "wobei die Zahlen die 1-indexierten Attachment-Nummern sind (1 = erstes "
    "Attachment, etc.). Lass die Zeile weg oder schreibe "
    "`FORWARD_ATTACHMENTS: []` wenn keine Attachments weitergegeben werden "
    "sollen. Hinweis: Forwarding erhöht das Bild-Set für den Generator "
    "und kann ihn verwirren — nur bei klarem Mehrwert nutzen.\n"
)


def parse_forward_attachments(director_reply: str) -> list[int]:
    """Extrahiert die `FORWARD_ATTACHMENTS: [...]`-Zeile aus dem Director-Reply.

    Liefert eine Liste 1-indexierter Attachment-Nummern. Leere Liste
    wenn der Block fehlt oder explizit `[]` ist.
    """
    import re as _re

    m = _re.search(
        r"FORWARD_ATTACHMENTS:\s*\[([^\]]*)\]",
        director_reply,
    )
    if not m:
        return []
    body = m.group(1).strip()
    if not body:
        return []
    try:
        return [int(x.strip()) for x in body.split(",") if x.strip()]
    except ValueError:
        return []


def _id_pass_blocks(bundle: SceneBundle, start_index: int) -> tuple[list[dict], list[str], int]:
    """Helper: liefert Image-Blocks + ANHANG-Textzeilen für Material-ID +
    Object-ID, wenn im Bundle vorhanden.

    Returns:
        (image_blocks, anhang_lines, next_index) — `next_index` ist der
        Bild-Index, an dem ein nächster Block (z.B. ein weiteres Bild)
        anschließen würde.
    """
    blocks: list[dict] = []
    lines: list[str] = []
    idx = start_index
    if bundle.material_id is not None:
        blocks.append(to_anthropic_image_block(bundle.material_id))
        lines.append("Bild {} = {}".format(idx, _MATERIAL_ID_LABEL))
        idx += 1
    if bundle.object_id is not None:
        blocks.append(to_anthropic_image_block(bundle.object_id))
        lines.append("Bild {} = {}".format(idx, _OBJECT_ID_LABEL))
        idx += 1
    return blocks, lines, idx


def director_user_content_v1(
    bundle: SceneBundle,
    user_text: str,
) -> list[dict]:
    """Content-Liste für den initialen (V1-) Director-Call.

    Bild-Reihenfolge (dynamisch, je nach Bundle-Inhalt):
    1. Beauty (immer)
    2. Reference (wenn vorhanden) — Stimmungs-/Stil-Anker
    3. Material-ID-Pass (wenn vorhanden) — Spatial-Anker für Material-Regionen
    4. Object-ID-Pass (wenn vorhanden) — Spatial-Anker für Objekt-Instanzen

    Wenn mehr als Beauty mitgeschickt wird, bekommt `user_text` einen
    ANHANG-Block prepended, der jedes Bild erklärt — kritisch wegen
    Phase-3-Falsifikation: ID-Pass-Farben sind LABELS, keine Render-
    Farben. Phrasierung in `_MATERIAL_ID_LABEL`/`_OBJECT_ID_LABEL`.
    """
    blocks: list[dict] = [to_anthropic_image_block(bundle.beauty)]
    anhang_lines: list[str] = [
        "Bild 1 = Enscape Beauty Render (Geometrie-Anker — Wandpositionen, "
        "Fenster, feste Einbauten heilig)."
    ]
    next_idx = 2
    if bundle.reference is not None:
        blocks.append(to_anthropic_image_block(bundle.reference))
        anhang_lines.append(
            "Bild {} = professionelles Stil-Referenz-Render derselben Scene "
            "(Stimmungs-/Material-/Atmosphären-Vorlage — NICHT als Geometrie-"
            "Vorlage; die kommt allein vom Beauty in Bild 1).".format(next_idx)
        )
        next_idx += 1

    if bundle.site_reference is not None:
        site_ref_img = bundle.site_reference
        marker = bundle.meta.site_reference_marker
        marker_hint = ""
        if marker is not None:
            site_ref_img = render_site_reference_marker(
                site_ref_img, marker.x_pct, marker.y_pct,
            )
            marker_hint = (
                " Der rote Pin auf diesem Bild markiert die geplante "
                "Gebaeude-Position auf dem Grundstueck — beschreibe die "
                "Umgebung (Vegetation, Bergsilhouetten, Nachbarbebauung, "
                "Topografie) relativ zu dieser Position."
            )
        blocks.append(to_anthropic_image_block(site_ref_img))
        anhang_lines.append(
            "Bild {} = REAL-WORLD-Standort-Referenz (Drohnen-Foto / Google-"
            "Maps/Earth-Screenshot / Site-Visit-Foto vom Projektstandort). "
            "Nutze es als Wahrheit fuer Vegetation, Umgebung, Bergsilhouetten, "
            "Atmosphaere, regionale Vegetations- und Materialsprache des "
            "Kontexts. Uebernimm KEINE Komposition und KEINE architektonischen "
            "Details aus diesem Bild — der finale Render hat seine eigene "
            "Komposition aus Bild 1 (Beauty). Konkret: wenn das Bild eine "
            "Top-Down-Sicht oder eine andere Perspektive zeigt, ist das fuer "
            "dich Kontext-Information, nicht Bildvorlage.{}".format(
                next_idx, marker_hint,
            )
        )
        next_idx += 1

    id_blocks, id_lines, next_idx = _id_pass_blocks(bundle, next_idx)
    blocks.extend(id_blocks)
    anhang_lines.extend(id_lines)

    text = user_text
    if len(anhang_lines) > 1:  # mehr als nur Beauty → ANHANG-Block prependen
        text = "ANHANG-Bildreihenfolge:\n" + "\n".join(anhang_lines) + "\n\n" + text
    blocks.append({"type": "text", "text": text})
    return blocks


# ---------------------------------------------------------------------------
# Run-Ordner: Namensschema + Persistenz
# ---------------------------------------------------------------------------

def label_from_model(model: str) -> str:
    """Leitet ein kompaktes Backend-Label aus dem Generator-Modellnamen ab.

    Wird beim Anlegen des Run-Ordners genutzt, sodass A/B-
    Vergleiche zwischen Generator-Backends ohne manuelles Umbenennen
    sauber dokumentiert sind:
    - `'gemini-2.5-flash-image'`           → `'NB1'`
    - `'gemini-3.1-flash-image-preview'`   → `'NB2'`
    - `'gpt-image-1'`                      → `'GPT'`

    Heuristik: `'gpt-image'`-Präfix ⇒ GPT, Substring `'gemini-3'` ⇒ NB2,
    sonst NB1. Wenn weitere Backends dazukommen oder feinere Labels
    gewünscht sind (z.B. `'NB2-thinking-high'`), kann der Caller das
    Label-Argument von `make_run_dir` direkt setzen statt diesen Helper
    zu rufen.
    """
    if model.startswith("gpt-image"):
        return "GPT"
    return "NB2" if "gemini-3" in model else "NB1"


def make_run_dir(
    generations_root: Path | str,
    scene_id: str,
    mode: Mode,
    run_index: int,
    now: Optional[datetime] = None,
    label: Optional[str] = None,
    phase_label: Optional[str] = None,
) -> Path:
    """Legt einen Run-Ordner nach dem Schema
    `[<phase_label>/<Scene_XX>/]YYYY-MM-DD_HHMMSS_<scene_id>_mode<M>_run<NN>[_<label>]`
    an und gibt den Pfad zurück.

    `scene_id` wird wie vom Scene-Ordner übernommen (z.B. `scene_01`).

    `label` (optional) hängt einen freitext-Suffix an den Ordnernamen
    (z.B. `'NB1'`, `'NB2'`). Default `None` = ohne Suffix.

    `phase_label` (optional) erzwingt eine zweistufige Subpfad-Struktur:
    `<generations_root>/<phase_label>/<Scene_XX>/<basename>`, z.B. um
    Versuchsreihen getrennt abzulegen. `Scene_XX` ist `scene_id.capitalize()`. Subpfad-
    Parents werden bei Bedarf angelegt. Default `None` = flache
    Struktur direkt unter `generations_root` (non-breaking für ältere
    Phase-0-Runs).
    """
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    name = f"{stamp}_{scene_id}_mode{mode}_run{run_index:02d}"
    if label:
        name = f"{name}_{label}"
    parent = Path(generations_root)
    if phase_label:
        parent = parent / phase_label / scene_id.capitalize()
        parent.mkdir(parents=True, exist_ok=True)
    run_dir = parent / name
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_inputs_json(
    run_dir: Path,
    *,
    scene_id: str,
    scene_path: Path,
    mode: Mode,
    user_request: str,
    director_model: str,
    iteration_type: IterationType = "initial",
    parent_run_id: Optional[str] = None,
    iteration_reason: Optional[str] = None,
    generator_model: Optional[str] = None,
    validator_model: Optional[str] = None,
    extra: Optional[dict] = None,
) -> Path:
    """Schreibt `inputs.json` mit dem Run-Kontext.

    `iteration_type` und `parent_run_id` spannen den Iterations-Baum
    auf. Bei `iteration_type="initial"`
    bleibt `parent_run_id` None.
    """
    if iteration_type == "initial" and parent_run_id is not None:
        raise ValueError("initial-Run darf keine parent_run_id haben")
    if iteration_type != "initial" and parent_run_id is None:
        raise ValueError(f"{iteration_type}-Run braucht eine parent_run_id")

    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "scene_id": scene_id,
        "scene_path": str(scene_path),
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "user_request": user_request,
        "iteration": {
            "type": iteration_type,
            "parent_run_id": parent_run_id,
            "reason": iteration_reason,
        },
        "director_model": director_model,
        "generator_model": generator_model,
        "validator_model": validator_model,
    }
    if extra:
        payload.update(extra)
    path = run_dir / "inputs.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Director-Empfehlung parsen
# ---------------------------------------------------------------------------

def parse_director_recommendation(
    director_reply: str,
) -> Optional[tuple[IterationType, str]]:
    """Extrahiert die `EMPFEHLUNG: ...` + `GRUND: ...`-Zeilen aus der
    Director-Antwort (siehe `prompts/director_system.md`).

    Gibt `None` zurück, wenn keine Empfehlung im Text steht (z.B. bei
    der ersten Generation). Rückgabe sonst:
    `("regenerate" | "refine", "<grund-text>")`.

    DEPRECATED (2026-05-27): Die Iterations-Empfehlung kommt jetzt vom
    Director-Advisor (siehe `parse_advisor_recommendation`), nicht mehr
    vom Director-Iteration-Reply. Diese Funktion bleibt nur fuer
    Backward-Kompatibilitaet (falls alte Director-Replies geparst
    werden muessen).
    """
    import re

    rec_match = re.search(r"^\s*EMPFEHLUNG:\s*(regenerate|refine)\s*$",
                          director_reply, re.MULTILINE | re.IGNORECASE)
    if not rec_match:
        return None
    rec = rec_match.group(1).lower()
    grund_match = re.search(r"^\s*GRUND:\s*(.+?)\s*$",
                            director_reply, re.MULTILINE)
    grund = grund_match.group(1).strip() if grund_match else ""
    return rec, grund  # type: ignore[return-value]


AdvisorAction = Literal["regenerate", "refine", "stop"]
AdvisorConfidence = Literal["high", "medium", "low"]


def parse_advisor_recommendation(
    advisor_reply: str,
) -> Optional[tuple[AdvisorAction, str, AdvisorConfidence]]:
    """Extrahiert ACTION/GRUND/KONFIDENZ aus dem Director-Advisor-Reply.

    Format (siehe `prompts/director_advisor_system.md`):

        ACTION: regenerate | refine | stop
        GRUND: <ein Satz>
        KONFIDENZ: high | medium | low

    Returns:
      Tuple (action, grund, konfidenz) bei erfolgreichem Parse, sonst
      None. None ist defensiv interpretierbar: UI zeigt keine Empfehlung,
      User entscheidet selbst.
    """
    import re

    action_match = re.search(
        r"^\s*ACTION:\s*(regenerate|refine|stop)\s*$",
        advisor_reply, re.MULTILINE | re.IGNORECASE,
    )
    if not action_match:
        return None
    action = action_match.group(1).lower()

    grund_match = re.search(r"^\s*GRUND:\s*(.+?)\s*$",
                            advisor_reply, re.MULTILINE)
    grund = grund_match.group(1).strip() if grund_match else ""

    konf_match = re.search(
        r"^\s*KONFIDENZ:\s*(high|medium|low)\s*$",
        advisor_reply, re.MULTILINE | re.IGNORECASE,
    )
    # Default-Konfidenz medium falls der Advisor's Reply die Zeile vergisst —
    # nicht None, damit der UI-Code nicht null-checken muss.
    konfidenz = konf_match.group(1).lower() if konf_match else "medium"

    return action, grund, konfidenz  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Validator JSON-Score-Block parsen + aggregieren
# ---------------------------------------------------------------------------
#
# Hintergrund: Das 3-stufige ✓/⚠/✗-Schema pro Sektion (Sektion-Header im
# Markdown-Report) ist für menschliche Leser, aber zu grob für maschinelle
# Iterations-Vergleiche — Detail-Issues versinken in der Aggregation. Seit
# der Validator-Prompt-Erweiterung schließt jeder Report mit einem
# JSON-Score-Block (1–5 pro Sub-Dimension, `null` wenn nicht anwendbar).
# Diese Helpers extrahieren + aggregieren den Block.

# Erwartetes Schema (fix, vom Validator-Prompt vorgegeben).
# Bei null darf der Validator setzen, aber keine fremden Keys erfinden.
#
# Zwei parallele Schemas: VALIDATOR_SCORE_SCHEMA für Exterior (Phase 0),
# VALIDATOR_SCORE_SCHEMA_INTERIOR für Interior (Phase 1). Aggregat-Helpers
# unten nehmen ein `schema=`-Argument, Default bleibt das Exterior-Schema.
# Auswahl per `bundle.meta.view_type`.

VALIDATOR_SCORE_SCHEMA: dict[str, list[str]] = {
    "geometrie": [
        "massing", "proportionen", "fensterachsen_tueren",
        "dachform", "elemente_komplett", "komposition_treue",
    ],
    "material": [
        "fassade", "dach", "fenster", "boden_paving",
    ],
    "anforderung": [
        "atmosphaere", "tageszeit_licht", "staffage",
        "modus_passung", "weisse_placeholder_ersetzt",
    ],
}

# Interior-Schema. MATERIAL bauteil-basiert statt
# material-typ-basiert. ANFORDERUNG enthält `default_objekte_ersetzt`
# mit furniture_state-abhängiger Interpretation (Validator-Prompt
# erklärt das). Keys ASCII-transliteriert analog Exterior-Schema.
VALIDATOR_SCORE_SCHEMA_INTERIOR: dict[str, list[str]] = {
    "geometrie": [
        "raumproportionen", "wandflaechen", "deckenhoehe",
        "tueroeffnungen", "einbauten_position",
    ],
    "material": [
        "wandbelag", "deckenfinish", "bodenbelag_innen",
        "moebelmaterial", "fensterleibung",
    ],
    "anforderung": [
        "raumstimmung", "lichtszenario", "moeblierungs_vollstaendig",
        "modus_passung", "default_objekte_ersetzt",
    ],
}


def parse_validator_scores_json(reply: str) -> Optional[dict]:
    """Extrahiert den JSON-Score-Block aus einem Validator-Reply.

    Sucht den **letzten** mit ```json ... ``` markierten Code-Block
    (der Validator soll den Score-Block am Ende der Antwort setzen).
    Liefert das geparste dict zurück oder `None`, wenn kein Block
    gefunden oder JSON malformed.
    """
    import json as _json
    import re as _re

    blocks = _re.findall(
        r"```json\s*(\{.*?\})\s*```", reply, _re.DOTALL | _re.IGNORECASE
    )
    if not blocks:
        return None
    try:
        return _json.loads(blocks[-1])
    except _json.JSONDecodeError:
        return None


def flatten_validator_scores(
    scores: dict,
    schema: Optional[dict[str, list[str]]] = None,
) -> dict[str, Optional[int]]:
    """Macht aus dem geschachtelten Score-Dict ein flaches dict zum
    Vergleich. Beispiel: `{'geometrie': {'massing': 4}}` →
    `{'geometrie.massing': 4}`. Nicht im Schema vorhandene Sub-Keys
    werden mit übernommen (für Robustheit), fehlende mit `None` ergänzt.

    `schema` Default ist `VALIDATOR_SCORE_SCHEMA` (Exterior). Für Interior
    `VALIDATOR_SCORE_SCHEMA_INTERIOR` übergeben.
    """
    if schema is None:
        schema = VALIDATOR_SCORE_SCHEMA
    flat: dict[str, Optional[int]] = {}
    # Erst alle erwarteten Schema-Keys mit None initialisieren
    for section, subs in schema.items():
        for sub in subs:
            flat[f"{section}.{sub}"] = None
    # Dann mit den tatsächlichen Werten überschreiben
    for section, subs in (scores or {}).items():
        if isinstance(subs, dict):
            for sub_key, value in subs.items():
                flat[f"{section}.{sub_key}"] = value
        else:
            flat[section] = subs
    return flat


def mean_validator_score(
    scores: dict,
    *,
    section: Optional[str] = None,
    schema: Optional[dict[str, list[str]]] = None,
) -> Optional[float]:
    """Mittelwert über alle nicht-`null`, numerischen Score-Werte.

    Mit `section=None` Mittelwert über das gesamte Schema, sonst nur
    über die Sub-Dimensionen einer Sektion (`'geometrie'`, `'material'`,
    `'anforderung'`). `schema` Default ist `VALIDATOR_SCORE_SCHEMA`
    (Exterior); für Interior `VALIDATOR_SCORE_SCHEMA_INTERIOR` übergeben.
    """
    flat = flatten_validator_scores(scores, schema=schema)
    if section is not None:
        prefix = f"{section}."
        values = [
            v for k, v in flat.items()
            if k.startswith(prefix) and isinstance(v, (int, float))
        ]
    else:
        values = [v for v in flat.values() if isinstance(v, (int, float))]
    return sum(values) / len(values) if values else None


def add_score_summary_to_inputs(
    run_dir: Path,
    validator_reply: str,
    schema: Optional[dict[str, list[str]]] = None,
) -> Optional[float]:
    """Liest den JSON-Score-Block aus `validator_reply`, berechnet
    `final_score` (Gesamt-Mean) und `section_means` (Geometrie/Material/
    Anforderung) und ergänzt sie in `run_dir/inputs.json`.

    Liefert den `final_score` zurück, oder `None` wenn der JSON-Block
    fehlt oder malformed ist (z.B. weil der Validator-Reply von vor der
    Schema-Erweiterung stammt). Bestehende `inputs.json`-Felder bleiben
    erhalten.

    `schema` Default ist `VALIDATOR_SCORE_SCHEMA` (Exterior); für Interior
    `VALIDATOR_SCORE_SCHEMA_INTERIOR` übergeben.

    Aufrufen direkt nach jedem Validator-Save — pro Run-Ordner einmal.
    """
    import json as _json

    if schema is None:
        schema = VALIDATOR_SCORE_SCHEMA

    inputs_path = run_dir / "inputs.json"
    if not inputs_path.exists():
        return None

    scores = parse_validator_scores_json(validator_reply)
    if scores is None:
        return None

    payload = _json.loads(inputs_path.read_text(encoding="utf-8"))
    payload["final_score"] = mean_validator_score(scores, schema=schema)
    payload["section_means"] = {
        section: mean_validator_score(scores, section=section, schema=schema)
        for section in schema
    }
    inputs_path.write_text(
        _json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return payload["final_score"]


def aggregate_validator_scores(
    scores_list: list[dict],
    schema: Optional[dict[str, list[str]]] = None,
) -> dict:
    """Aggregiert eine Liste von JSON-Scores (z.B. aus N Sampling-Runs)
    in ein Dict pro Sub-Dimension mit `{mean, min, max, n_valid, n_null}`.

    Beispiel-Output:
        {
            'geometrie.dachform': {'mean': 3.33, 'min': 2, 'max': 4,
                                    'n_valid': 3, 'n_null': 0},
            ...
        }

    Nützlich für 5e-Aggregat-Tabellen: Δ-Mean zwischen Varianten pro
    feiner Sub-Dimension statt nur pro Sektion.

    `schema` Default ist `VALIDATOR_SCORE_SCHEMA` (Exterior); für Interior
    `VALIDATOR_SCORE_SCHEMA_INTERIOR` übergeben.
    """
    if not scores_list:
        return {}

    # Alle vorkommenden flat-Keys sammeln (Schema + ggf. Drift)
    all_keys: set[str] = set()
    flats: list[dict[str, Optional[int]]] = []
    for s in scores_list:
        f = flatten_validator_scores(s, schema=schema)
        flats.append(f)
        all_keys.update(f.keys())

    agg: dict[str, dict] = {}
    for key in sorted(all_keys):
        values = [
            f[key] for f in flats
            if isinstance(f.get(key), (int, float))
        ]
        n_null = len(flats) - len(values)
        if values:
            agg[key] = {
                "mean": sum(values) / len(values),
                "min": min(values),
                "max": max(values),
                "n_valid": len(values),
                "n_null": n_null,
            }
        else:
            agg[key] = {
                "mean": None, "min": None, "max": None,
                "n_valid": 0, "n_null": n_null,
            }
    return agg
