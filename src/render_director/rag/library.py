"""Tier-0-Referenzbibliothek: Metadaten-Match auf kuratierten Projektfotos.

Kein ML. Die Fotos der Referenzbibliothek tragen ihre Metadaten
strukturiert im Dateinamen:

    [test_|val_]<projekt>_<nnn>[_<region>]_<typ>_<material>_<zeit>_<winkel..>_<distanz>.jpg
    z.B.  projecta_005_hotel_woodglass_day_corner_close.jpg
          projectb_001_residential_plastermetal_day_other_facadeup_close.jpg

`<typ>` ist ein Gebäudetyp aus `BUILDING_TYPE_VOCAB`; der Parser verankert
sich daran. Ein optionales Regions-Token davor wird ignoriert.

`build_index()` parst alle Fotos zu `LibraryImage`-Records (robust gegen
Tippfehler wie `walkwa`/`walkway.`). `find_similar_for_snapshot()` mappt das
deutsche BIM-`meta.json` (Materialien als Freitext, Tageszeit deutsch) aufs
Library-Vokabular und rankt nach Material-Überlappung (Hauptsignal) +
Tageszeit-Match (Nebensignal).

Fehlt der Library-Ordner (z.B. Nutzer ohne lokale Fotos), liefern die
Funktionen defensiv `[]` — der Aufrufer rendert dann einfach ohne Referenzen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# --- Kontrollierte Vokabulare (aus der Filename-Analyse) -------------------
TIME_VOCAB = {"day", "dusk", "night", "overcast", "dawn"}
DIST_VOCAB = {"close", "medium", "far"}
# Gebäudetyp-Token, an dem sich der Dateinamen-Parser verankert.
BUILDING_TYPE_VOCAB = {
    "hotel", "residential", "house", "office", "public", "school",
    "retail", "industrial", "mixed",
}
# Material-Kompositum (woodglass, plastermetal, …) → Komponenten-Keywords.
# Substring-Match, damit metalgreen→{metal,green}, woodstone→{wood,stone} etc.
MATERIAL_KEYS = ("wood", "glass", "metal", "plaster", "stone", "green", "concrete")
# Split-Präfixe (train/test/val), die dem Projektnamen vorangestellt sein können.
_SPLIT_PREFIXES = {"test", "val", "train"}

# --- Deutsch (BIM meta.json) → Library-Vokabular ---------------------------
# Substring-Treffer im (kleingeschriebenen) Material-Freitext → Keyword.
_GERMAN_MATERIAL_MAP: list[tuple[str, str]] = [
    ("holz", "wood"), ("lärche", "wood"), ("laerche", "wood"), ("larche", "wood"),
    ("eiche", "wood"), ("fichte", "wood"), ("wood", "wood"),
    ("glas", "glass"), ("glass", "glass"),
    ("metall", "metal"), ("alu", "metal"), ("stahl", "metal"), ("blech", "metal"),
    ("zink", "metal"), ("kupfer", "metal"), ("metal", "metal"),
    ("putz", "plaster"), ("plaster", "plaster"),
    ("stein", "stone"), ("porphyr", "stone"), ("marmor", "stone"),
    ("granit", "stone"), ("quarzit", "stone"), ("schiefer", "stone"), ("stone", "stone"),
    ("beton", "concrete"), ("concrete", "concrete"),
    ("grün", "green"), ("gruen", "green"), ("begrün", "green"),
    ("bepflanz", "green"), ("efeu", "green"), ("green", "green"),
]
# Substring im deutschen time_of_day → Library-Zeit. Reihenfolge = Priorität
# (spezifisch vor generisch, damit "später nachmittag" nicht an "nacht" hängt).
_GERMAN_TIME_MAP: list[tuple[str, str]] = [
    ("dämmer", "dusk"), ("daemmer", "dusk"), ("abend", "dusk"),
    ("sonnenunter", "dusk"), ("golden", "dusk"), ("blaue stunde", "dusk"),
    ("nacht", "night"),
    ("bewölkt", "overcast"), ("bewoelkt", "overcast"), ("bedeckt", "overcast"),
    ("morgen", "day"), ("vormittag", "day"), ("mittag", "day"),
    ("nachmittag", "day"), ("tag", "day"), ("mittags", "day"),
]


@dataclass(frozen=True)
class LibraryImage:
    """Ein einzelnes Referenzfoto mit geparsten Metadaten."""
    path: Path
    project: str
    material_tokens: frozenset[str]   # z.B. {"wood", "glass"}
    time: Optional[str]               # day/dusk/night/overcast
    angle: Optional[str]              # corner/front/other_walkway/…
    distance: Optional[str]           # close/medium/far
    caption: Optional[str] = None
    project_folder: str = ""          # Top-Ordnername, z.B. "Alpine Lodge"


def _material_keywords(token: str) -> frozenset[str]:
    """Kompositum-Token → Menge der Komponenten-Keywords (Substring-Match)."""
    found = {k for k in MATERIAL_KEYS if k in token}
    return frozenset(found) if found else frozenset({token})


def _clean(tok: str) -> str:
    """Nur a-z behalten (entfernt Tippfehler-Artefakte wie 'walkway.')."""
    return re.sub(r"[^a-z]", "", tok)


def parse_filename(stem: str) -> dict:
    """Zerlegt einen Dateinamen-Stamm in die Metadaten-Felder.

    Robust: verankert am Gebäudetyp-Token (Material = das Token danach),
    Zeit + Distanz per Vokabular, Winkel = der Rest dazwischen. Fällt auf
    `<projekt>_<nnn>_<typ>_<material>` zurück, falls kein bekannter
    Gebäudetyp im Namen steht.
    """
    toks = [t for t in re.split(r"[_\s]+", stem.lower()) if t]
    if toks and toks[0] in _SPLIT_PREFIXES:
        toks = toks[1:]
    project = toks[0] if toks else "unknown"

    type_idx = next(
        (i for i, t in enumerate(toks) if i >= 1 and t in BUILDING_TYPE_VOCAB),
        None,
    )
    if type_idx is not None:
        material_tok = toks[type_idx + 1] if type_idx + 1 < len(toks) else ""
        rest = toks[type_idx + 2:]
    else:
        material_tok = toks[3] if len(toks) > 3 else ""
        rest = toks[4:] if len(toks) > 4 else []

    time = next((t for t in rest if t in TIME_VOCAB), None)
    distance = None
    for t in reversed(rest):
        if _clean(t) in DIST_VOCAB:
            distance = _clean(t)
            break
    angle_toks = [t for t in rest if t != time and _clean(t) not in DIST_VOCAB]
    angle = "_".join(_clean(t) for t in angle_toks if _clean(t)) or None

    return {
        "project": project,
        "material_tokens": _material_keywords(material_tok),
        "time": time,
        "angle": angle,
        "distance": distance,
    }


# Modul-Cache: Index einmal pro Root bauen (per-user-lokal, statische Fotos;
# Server-Neustart refresht). Key = aufgelöster Pfad-String.
_INDEX_CACHE: dict[str, list[LibraryImage]] = {}


def build_index(root: Path | str, *, use_cache: bool = True) -> list[LibraryImage]:
    """Scannt `root` rekursiv nach `*.jpg`, parst Metadaten + Caption.

    Liefert `[]`, wenn der Ordner fehlt (Nutzer ohne lokale Fotos). Ergebnis
    wird pro Root gecacht.
    """
    root = Path(root)
    key = str(root.resolve())
    if use_cache and key in _INDEX_CACHE:
        return _INDEX_CACHE[key]

    images: list[LibraryImage] = []
    if root.is_dir():
        for p in sorted(root.rglob("*.jpg")):
            fields = parse_filename(p.stem)
            caption = None
            txt = p.with_suffix(".txt")
            if txt.is_file():
                try:
                    caption = txt.read_text(encoding="utf-8").strip() or None
                except OSError:
                    caption = None
            # Projekt-Ordner = oberster Ordner unter root (z.B. "Alpine Lodge").
            try:
                project_folder = p.relative_to(root).parts[0]
            except (ValueError, IndexError):
                project_folder = ""
            images.append(LibraryImage(
                path=p, caption=caption, project_folder=project_folder, **fields,
            ))

    if use_cache:
        _INDEX_CACHE[key] = images
    return images


# Kurze Needles, die nur am Wortanfang zählen: sonst trifft "alu" auch
# "Schalung" (Holzschalung würde zu Metall). Lange Needles bleiben reine
# Substrings, damit deutsche Komposita ("Lärchenholz", "Sichtbeton") greifen.
_WORD_START_NEEDLES = {"alu"}


def _contains(low: str, needle: str) -> bool:
    if needle in _WORD_START_NEEDLES:
        return re.search(r"(?<![a-zäöüß])" + re.escape(needle), low) is not None
    return needle in low


def _keywords_from_text(text: str, mapping: list[tuple[str, str]]) -> list[str]:
    """Alle Ziel-Keywords, deren Needle im Text vorkommt (Reihenfolge stabil)."""
    low = (text or "").lower()
    out: list[str] = []
    for needle, target in mapping:
        if _contains(low, needle) and target not in out:
            out.append(target)
    return out


def snapshot_material_keywords(materials: dict, extra_text: str = "") -> frozenset[str]:
    """BIM-Material-Freitext (+ optional User-Prompt) → Library-Material-Keywords."""
    blob = " ".join(str(v) for v in (materials or {}).values())
    if extra_text:
        blob = blob + " " + extra_text
    return frozenset(_keywords_from_text(blob, _GERMAN_MATERIAL_MAP))


def snapshot_time(time_of_day: str) -> Optional[str]:
    """Deutsche Tageszeit → Library-Zeit (day/dusk/night/overcast) oder None."""
    hits = _keywords_from_text(time_of_day or "", _GERMAN_TIME_MAP)
    return hits[0] if hits else None


# Scoring-Gewichte: Material ist das dominante Stil-Signal, Zeit sekundär.
_W_MATERIAL = 2.0
_W_TIME = 1.0


def find_similar(
    index: list[LibraryImage],
    *,
    material_keywords: frozenset[str],
    time: Optional[str],
    k: int = 2,
) -> list[LibraryImage]:
    """Rankt die Bibliothek nach Material-Überlappung + Zeit-Match.

    Nur Kandidaten mit ≥1 Material-Überlappung qualifizieren (reines Zeit-Match
    wäre stilistisch beliebig). Ergebnis: top-`k`, mit Projekt-Diversität
    (verschiedene Projekte bevorzugt, damit K=2 nicht 2× dasselbe Gebäude ist).
    """
    if k <= 0 or not index:
        return []

    scored: list[tuple[float, str, LibraryImage]] = []
    for img in index:
        overlap = len(material_keywords & img.material_tokens)
        if overlap == 0:
            continue
        score = _W_MATERIAL * overlap
        if time and img.time == time:
            score += _W_TIME
        # Tie-Break-Key = Dateiname (stabil/reproduzierbar).
        scored.append((score, img.path.name, img))

    scored.sort(key=lambda t: (-t[0], t[1]))

    picked: list[LibraryImage] = []
    seen_projects: set[str] = set()
    # 1. Durchgang: Projekt-Diversität.
    for _score, _name, img in scored:
        if len(picked) >= k:
            break
        if img.project in seen_projects:
            continue
        picked.append(img)
        seen_projects.add(img.project)
    # 2. Durchgang: auffüllen, falls zu wenige verschiedene Projekte.
    if len(picked) < k:
        for _score, _name, img in scored:
            if len(picked) >= k:
                break
            if img in picked:
                continue
            picked.append(img)
    return picked


def find_similar_for_snapshot(
    materials: dict,
    time_of_day: str,
    *,
    root: Path | str,
    k: int = 2,
    extra_text: str = "",
) -> list[LibraryImage]:
    """Bequemer Wrapper: baut/holt den Index + mappt das deutsche meta.json.

    `extra_text` (optional) = z.B. der User-Prompt, als zusätzliches Material-
    Signal. Liefert `[]`, wenn der Ordner fehlt oder kein Material erkennbar.
    """
    index = build_index(root)
    if not index:
        return []
    mats = snapshot_material_keywords(materials, extra_text=extra_text)
    if not mats:
        return []
    return find_similar(index, material_keywords=mats, time=snapshot_time(time_of_day), k=k)


def _norm_name(s: str) -> str:
    """Namen für den Match normalisieren (klein, nur a-z0-9)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_by_project(
    index: list[LibraryImage], project_query: str, *, k: int = 2,
) -> list[LibraryImage]:
    """Fotos eines Projekts per Namens-Match. `project_query` (z.B. der Ad-hoc-
    Projekt-Tag oder meta.project_name) matcht gegen den Projekt-Codenamen
    (Filename) ODER den Projekt-Ordnernamen, jeweils normalisiert.

    Beispiele: „projecta"→projecta_*.jpg, „lodge"→Ordner „Alpine Lodge".
    Liefert bis zu `k` Treffer, gleichmäßig über die Projekt-Fotos verteilt
    (Winkel/Distanz-Varianz statt 2× fast identische Aufnahmen).
    """
    q = _norm_name(project_query)
    if k <= 0 or not index or len(q) < 3:
        return []
    hits = [
        img for img in index
        if q == _norm_name(img.project) or q in _norm_name(img.project_folder)
    ]
    if not hits:
        return []
    hits.sort(key=lambda i: i.path.name)
    step = max(1, len(hits) // k)  # gleichmäßig verteilen für Varianz
    return hits[::step][:k]


def select_reference_images(
    *,
    root: Path | str,
    k: int = 2,
    project_query: Optional[str] = None,
    materials: Optional[dict] = None,
    time_of_day: Optional[str] = None,
    extra_text: str = "",
) -> list[LibraryImage]:
    """Referenzfotos wählen — Projektname zuerst, sonst Material+Tageszeit.

    - Ad-hoc: nur `project_query` (der Projekt-Tag) gesetzt → reines Projekt-Match.
    - Snapshot: `project_query` (meta.project_name) + `materials`/`time_of_day`
      → passt der Projektname zu einem Ordner, gewinnt das (spezifischer);
      sonst Fallback auf Material+Tageszeit.

    Liefert `[]`, wenn die Bibliothek fehlt oder nichts passt.
    """
    index = build_index(root)
    if not index:
        return []
    if project_query:
        by_proj = find_by_project(index, project_query, k=k)
        if by_proj:
            return by_proj
    if materials:
        mats = snapshot_material_keywords(materials, extra_text=extra_text)
        if mats:
            return find_similar(index, material_keywords=mats, time=snapshot_time(time_of_day), k=k)
    return []
