"""Site-Analyzer — orchestriert Wikimedia + OSM + Claude-Vision-Analyse.

Workflow:
  1. Wikimedia-Geosearch -> N akzeptierte Bilder (CC0/PD/CC-BY)
  2. Top-K Bilder runterladen
  3. OSM Overpass -> benannte Features als Text
  4. Claude-Vision-Call: "schreibe regionale Charakteristik aus diesen
     Inputs" -> strukturierter Text
  5. Cache als <snapshot>/site_analysis.md (Markdown mit Header)
  6. Subsequente Calls lesen aus Cache statt neu fetchen

Lizenz-Architektur:
  Wikimedia-Bilder werden NUR an Claude (Vision) gegeben. Der Generator
  (Nano Banana) sieht sie nie. Source-IDs + Lizenz werden im Cache-Header
  geloggt für Forensik / Attribution.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from render_director.clients import get_anthropic_client
from render_director.geo.osm_overpass import (
    fetch_nearby_features,
    format_features_as_text,
)
from render_director.geo.wikimedia import (
    WikimediaImage,
    download_image,
    search_nearby,
)
from render_director.geo.wikipedia_leads import fetch_leads_for_features
from render_director.utils import to_anthropic_image_block

CACHE_FILENAME = "site_analysis.md"
DEFAULT_ANALYZER_MODEL = "claude-sonnet-4-6"
ANALYZER_MAX_TOKENS = 1024

# Anzahl Wikimedia-Bilder die wir laden + an Claude geben.
# Mehr = mehr Kontext aber teurer + langsamer. 3 ist ein guter Mittelweg.
WIKIMEDIA_TOP_K = 3


SYSTEM_PROMPT = """Du bist ein Geo-Analyst für eine Architektur-Render-Pipeline.

Aufgabe: Aus Wikimedia-Bildern + OSM-Landmarken-Daten eine **kompakte
Standort-Charakteristik** für einen Architektur-Render-Director schreiben.

**Die OSM-Landmarken sind die Wahrheit** (deterministische Daten,
benannte Gipfel mit Höhe + Distanz + Himmelsrichtung). Wikimedia-
Bilder sind nur Inspiration für Stimmung — wenn sie der OSM-Wahrheit
widersprechen, vertraue OSM.

Output-Format (max 220 Wörter, deutsch, strukturiert):

## VEGETATION
1-2 Sätze: typische Pflanzen-/Wald-Mischung der Region. Konkret machen
(z.B. „Lärchen-Fichten-Mischwald" statt „alpine Bäume").

## TOPOGRAFIE & BERGHINTERGRUND
**Nenne EXPLIZIT die OSM-Berge mit Name + Höhe + Himmelsrichtung**, sofern
in den Daten enthalten. Beispiel: *„Im Süden dominieren Gipfel A (2810 m)
und Gipfel B (2839 m), beide felsig-hellgrau. Nördlich sanftere bewaldete
Mittelgebirgsrücken."*  Falls keine Berg-OSM-Daten: generisch beschreiben.
Auch Seen/Flüsse benennen wenn nahe.

## LICHT- & ATMOSPHÄRE-CHARAKTERISTIK
1-2 Sätze: typisches Licht für Höhe + Breitengrad. Hochalpine Lagen haben
klareres, härteres Licht. Tallagen weicher/dunstiger.

## ARCHITEKTUR-KONTEXT (Umfeld)
1 Satz: typische Bauweise/Materialien der Nachbar-Bauten. Vorsicht bei
Wikimedia-Bildern die offensichtlich nicht zur Projektlage gehören
(z.B. Bahnhöfe, urbane Plätze) — die NICHT als Vorbild nehmen.

## ANTI-PATTERNS (was NICHT in den Render gehört)
Stichpunkte. 3-5 max. Konkrete Region-Klischees rausfiltern (z.B.
„keine Palmen" reicht nicht — schreibe was stattdessen *typisch ist*).

WICHTIG:
- OSM-Namen sind verbindlich. Erfinde keine eigenen Gipfel/Seen.
- Wenn Wikimedia-Bilder weit vom Projekt sind (>2 km laut Distance-Tag) oder
  thematisch fremd (Bahnhof, urbanes Detail, Innenraum-Detail), behandle
  sie als nicht-repräsentativ und ignoriere visuell — OSM gewinnt dann.
- Beschreibe Atmosphäre + Region-DNA, nicht Komposition des Renders.
- Keine Marken/Hotelnamen erwähnen.
"""


@dataclass
class SiteAnalysisResult:
    """Was der Analyzer schreibt + woher die Daten kamen (für Cache-Header)."""
    analysis_text: str
    wikimedia_sources: list[dict]  # Liste {title, page_url, license, artist, distance_m}
    osm_features_count: int
    generated_at: str


def _build_user_text(lat: float, lon: float,
                     osm_text: str,
                     wm_images: list[WikimediaImage]) -> str:
    parts = [
        "STANDORT-KOORDINATEN: {:.4f}° N, {:.4f}° E".format(lat, lon),
        "",
        "OSM-LANDMARKEN IM 5-KM-RADIUS:",
        osm_text,
        "",
        "WIKIMEDIA-BILDER (in Reihenfolge ihrer Anhänge):",
    ]
    for i, img in enumerate(wm_images, start=1):
        parts.append(
            "  Bild {}: {} (Distanz {:.1f} km, Lizenz {})".format(
                i, img.title, img.distance_m / 1000.0, img.license_short,
            )
        )
    parts.append(
        "\nSchreibe basierend auf diesen Inputs die Standort-Charakteristik "
        "im vorgegebenen Format."
    )
    return "\n".join(parts)


def _save_wikimedia_locally(
    scene_dir: Path,
    images: list[WikimediaImage],
) -> dict[str, str]:
    """Speichert NUR die `redistributable`-Bilder unter
    <scene_dir>/site_references/wm_NN_<safe-title>.jpg. Bilder mit
    `usage='analysis_only'` (CC-BY-SA / GFDL) werden absichtlich
    NICHT lokal gespeichert — sie laufen nur durch Claude-Vision für
    die Text-Analyse und werden danach verworfen.

    Returns dict {title -> local_filename} nur für gespeicherte Bilder.
    """
    import re
    out_dir = scene_dir / "site_references"
    out_dir.mkdir(exist_ok=True)
    mapping: dict[str, str] = {}
    for i, img in enumerate(images, start=1):
        if img.image is None:
            continue
        if img.usage != "redistributable":
            # analysis_only — bewusst nicht speichern
            continue
        # Title slugifizieren: "File:Foo bar.jpg" -> "foo-bar"
        slug = img.title
        if slug.lower().startswith("file:"):
            slug = slug[5:]
        slug = re.sub(r"\.(jpg|jpeg|png|tif|tiff)$", "", slug, flags=re.I)
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", slug).strip("-").lower()[:60]
        fname = "wm_{:02d}_{}.jpg".format(i, slug or "untitled")
        try:
            img.image.save(out_dir / fname, format="JPEG", quality=85)
            mapping[img.title] = fname
        except Exception:
            pass
    return mapping


def _run_analysis(
    scene_dir: Path,
    lat: float, lon: float,
    radius_m: int = 5000,
    model: str = DEFAULT_ANALYZER_MODEL,
) -> Optional[SiteAnalysisResult]:
    """Frisch fetchen + analysieren. Returns None wenn keine Datenbasis
    übrig bleibt (Analyse wäre dann Halluzination)."""
    # 1. OSM-Features (Text) — bilden die Grundlage, fast immer reichhaltig
    osm_features = fetch_nearby_features(lat, lon, radius_m=radius_m)

    # 2. Wikipedia-Article-Leads für die wichtigsten OSM-Places (KURIERT,
    # bevorzugt). Holt aus DE/IT/EN-Wikipedia das Hauptbild der Artikel zu
    # Bergen, Seen, Dörfern in der Umgebung.
    wm_images: list[WikimediaImage] = []
    if osm_features:
        wm_images.extend(
            fetch_leads_for_features(
                osm_features, lat, lon,
                overall_limit=WIKIMEDIA_TOP_K,
            )
        )

    # 3. Fallback: wenn Wikipedia-Leads dünn waren, mit Geosearch auffüllen
    # (gefiltert, blacklisted) bis WIKIMEDIA_TOP_K erreicht.
    if len(wm_images) < WIKIMEDIA_TOP_K:
        existing_titles = {img.title for img in wm_images}
        geosearch_candidates = search_nearby(
            lat, lon, radius_m=radius_m, limit=20,
        )
        for cand in geosearch_candidates:
            if len(wm_images) >= WIKIMEDIA_TOP_K:
                break
            if cand.title in existing_titles:
                continue
            wm_images.append(cand)

    # 4. Bilder runterladen
    for img in wm_images:
        download_image(img, max_dim=1024)
    wm_images = [img for img in wm_images if img.image is not None]

    if not wm_images and not osm_features:
        return None

    osm_text = format_features_as_text(osm_features, lat, lon)
    user_text = _build_user_text(lat, lon, osm_text, wm_images)

    # 2. Claude-Vision-Call
    content: list = []
    for img in wm_images:
        content.append(to_anthropic_image_block(img.image))
    content.append({"type": "text", "text": user_text})

    response = get_anthropic_client().messages.create(
        model=model,
        max_tokens=ANALYZER_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    analysis_text = response.content[0].text

    # Bilder lokal speichern für User-Inspektion (site_references/-Subfolder)
    local_map = _save_wikimedia_locally(scene_dir, wm_images)

    sources = [{
        "title": img.title,
        "page_url": img.page_url,
        "license": img.license_short,
        "usage": img.usage,  # "redistributable" | "analysis_only"
        "artist": img.artist,
        "distance_km": round(img.distance_m / 1000.0, 2),
        "local_file": local_map.get(img.title),  # None bei analysis_only
    } for img in wm_images]

    return SiteAnalysisResult(
        analysis_text=analysis_text,
        wikimedia_sources=sources,
        osm_features_count=len(osm_features),
        generated_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    )


def _write_cache(scene_dir: Path, result: SiteAnalysisResult) -> None:
    """Schreibt Cache als Markdown mit YAML-Header (Sources/Lizenzen
    sind im Header forensisch zugreifbar, der Analyse-Text bleibt
    lesbar im Body)."""
    cache_path = scene_dir / CACHE_FILENAME
    lines = [
        "---",
        "generated_at: {}".format(result.generated_at),
        "osm_features: {}".format(result.osm_features_count),
        "osm_attribution: '© OpenStreetMap contributors (ODbL)'",
        "wikimedia_attribution: 'Wikimedia Commons — siehe wikimedia_sources unten'",
        "wikimedia_sources:",
    ]
    for src in result.wikimedia_sources:
        lines.append("  - title: {!r}".format(src["title"]))
        lines.append("    page_url: {!r}".format(src["page_url"]))
        lines.append("    license: {!r}".format(src["license"]))
        lines.append("    usage: {!r}".format(src.get("usage", "redistributable")))
        if src.get("artist"):
            lines.append("    artist: {!r}".format(src["artist"]))
        lines.append("    distance_km: {}".format(src["distance_km"]))
        if src.get("local_file"):
            lines.append("    local_file: {!r}".format(
                "site_references/" + src["local_file"]))
    lines.append("---")
    lines.append("")
    lines.append(result.analysis_text)
    cache_path.write_text("\n".join(lines), encoding="utf-8")


def _read_cache(scene_dir: Path) -> Optional[str]:
    """Liest nur den Analyse-Text-Body aus dem Cache (YAML-Header skippen)."""
    cache_path = scene_dir / CACHE_FILENAME
    if not cache_path.is_file():
        return None
    raw = cache_path.read_text(encoding="utf-8")
    # YAML-Frontmatter zwischen --- entfernen
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\n")
    return raw.strip()


def get_site_analysis(
    scene_dir: Path,
    lat: float, lon: float,
    *, force_refresh: bool = False,
) -> Optional[str]:
    """Liefert die Standort-Charakteristik als Text, mit File-Cache pro
    Snapshot. Bei force_refresh=True wird neu gefetcht.

    Returns:
      Text (Markdown-strukturierter Analyse-Block) oder None wenn weder
      Cache noch frische Analyse möglich war.
    """
    if not force_refresh:
        cached = _read_cache(scene_dir)
        if cached:
            return cached

    from render_director.clients import use_mock_providers
    if use_mock_providers():
        # Demo/Tests: keine Netzwerk-Calls und kein Cache im Scene-Ordner.
        from render_director.mock_providers import MOCK_SITE_ANALYSIS
        return MOCK_SITE_ANALYSIS

    # Defensive: alles in einem try-except, damit Pipeline nicht stirbt
    try:
        result = _run_analysis(scene_dir, lat, lon)
    except Exception as exc:
        import sys
        print(
            "[site_analyzer] WARN: Analyse fehlgeschlagen ({!r}) — "
            "Render läuft ohne SITE-ANALYSE weiter.".format(exc),
            file=sys.stderr,
        )
        return None

    if result is None:
        return None

    try:
        _write_cache(scene_dir, result)
    except Exception:
        pass  # Cache-Fail ist nicht-fatal — Text geben wir trotzdem zurück

    return result.analysis_text
