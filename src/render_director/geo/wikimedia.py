"""Wikimedia Commons Geosearch — Bilder im Radius um Lat/Lon abrufen.

API-Doku: https://www.mediawiki.org/wiki/Geosearch
Wir nutzen die MediaWiki-API ohne Auth (öffentlich), nur User-Agent.
Lizenz pro Bild kommt aus den ImageInfo-Extmetadata.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import requests
from PIL import Image

from render_director.geo.http import user_agent

WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"

# Lizenzen die wir akzeptieren (=keine ShareAlike-Pflicht beim Output).
# CC-BY-SA bewusst NICHT in der Liste — würde Render-Output unter SA stellen.
ACCEPTED_LICENSES = {
    "cc0", "cc 0", "public domain", "pd", "pd-", "cc-pd",
    "cc-by", "cc by", "cc-by-4.0", "cc-by-3.0", "cc-by-2.5", "cc-by-2.0",
}

# Title-Blacklist: substrings die offensichtlich uninteressant sind als
# regionale Referenz. Iterativ erweitert: in Tallagen mit Bahnlinie
# dominieren Eisenbahn-Fotos die geo-getaggten Wikimedia-Bilder, dazu
# kamen Satelliten- und NASA-Earth-Photos.
TITLE_BLACKLIST_SUBSTRINGS = {
    # Weltraum / Satellit
    "iss0", "iss-0",       # ISS-Astronauten-Fotos
    "nasa",                # NASA-allgemein
    "satellite",           # Satellitenbilder
    "earth observation",   # Earth Observation
    "from space",          # Spaceview
    "sentinel",            # ESA-Sentinel-Bilder
    "landsat",             # USGS-Landsat
    # Bahnverkehr (in Tallagen mit Bahnlinie extrem viel Crowdsource-
    # Material — Lokomotiven, Bahnhöfe, Gleisanlagen — alles nicht-
    # repräsentativ für Architektur-Render).
    "train", "treno", "railway", "rail line", "bahn ", " bahn",
    "bahnhof", "stazione", "locomotive", "lokomotive",
    "ferrovia", "ferroviaria", "gleis", "platform",
    "fs ", "öbb",
    # Sport- / Freizeitanlagen (Motorikpark, Spielplätze, Fitness-Geräte)
    "motorikpark", "fitness", "playground", "spielplatz",
    # Wappen / Karten / abstrakt
    "logo", "icon", "diagram", "map of",
    "coat of arms", "wappen", "stemma",
}


def _title_blacklisted(title: str) -> bool:
    """True wenn der Titel offensichtlich nicht-ground-level-Inhalt ist."""
    low = title.lower()
    return any(token in low for token in TITLE_BLACKLIST_SUBSTRINGS)


@dataclass
class WikimediaImage:
    """Ein Wikimedia-Commons-Bild + Metadaten + Lizenz-Info.

    `usage` (Phase-5-Lizenz-Policy):
      - "redistributable": CC0/PD/CC-BY → darf lokal gespeichert + an
        Generator weitergegeben werden (theoretisch — aktuell tun wir
        letzteres trotzdem nicht, zum Generator gehen nur Director-
        produzierte Texte)
      - "analysis_only": CC-BY-SA → nur an Claude-Vision für Text-
        Output zulässig. Bild wird NICHT lokal gespeichert, NICHT in
        site_references/ exportiert, NICHT zum Generator weitergegeben.
    """
    title: str           # z.B. "File:Mountain_lake_2018.jpg"
    page_url: str        # Commons-Beschreibungs-URL (für Attribution-Log)
    image_url: str       # Direkt-URL zur Bild-Datei (für Download)
    lat: float
    lon: float
    distance_m: float    # Distanz zum Query-Punkt in Metern
    license_short: str   # z.B. "cc-by-4.0", "cc0", "public domain"
    artist: Optional[str] = None  # Foto-Urheber (für Log)
    image: Optional[Image.Image] = None  # erst nach download_image() befüllt
    usage: str = "redistributable"   # "redistributable" | "analysis_only"


def _license_acceptable(license_short: Optional[str]) -> bool:
    """True wenn Lizenz **redistributable** ist (CC0/PD/CC-BY ohne SA).
    Wird vom Backward-Kompat-Code genutzt. Strenger Filter."""
    return _classify_license(license_short) == "redistributable"


def _classify_license(license_short: Optional[str]) -> Optional[str]:
    """Klassifiziert eine Lizenz-Kurzform:

      "redistributable" — frei (CC0/PD/CC-BY ohne SA-Klausel).
                           Bild darf lokal gespeichert + weitergereicht
                           werden.
      "analysis_only"   — CC-BY-SA / GFDL / ShareAlike. Darf an Claude-
                           Vision für Text-Output gegeben werden
                           (transformatives Derivat, kein Re-Publish),
                           aber NICHT lokal gespeichert/weitergereicht.
      None              — komplett unbrauchbar (Fair-Use-Only, kein CC,
                           kommerziell-NC, leere Lizenz).
    """
    if not license_short:
        return None
    norm = license_short.strip().lower().replace(" ", "-")

    # Hard-Reject: alle NC- (NonCommercial-) und ND- (NoDerivatives-) Varianten,
    # auch wenn sie zusätzlich CC-BY oder SA tragen.
    if "-nc" in norm or "noncommercial" in norm:
        return None
    if "-nd" in norm or "noderivatives" in norm:
        return None

    # Erst die freie Variante prüfen (CC-BY / CC0 / PD)
    if "-sa" not in norm and "sharealike" not in norm and "gfdl" not in norm:
        for accept in ACCEPTED_LICENSES:
            accept_norm = accept.strip().lower().replace(" ", "-")
            if norm == accept_norm or norm.startswith(accept_norm):
                return "redistributable"

    # Dann die SA-Variante (analysis-only)
    if "-sa" in norm or "sharealike" in norm:
        # Nur CC-BY-SA und GFDL zulassen, nicht NC-SA
        if "-nc" in norm or "noncommercial" in norm:
            return None
        return "analysis_only"
    if "gfdl" in norm:
        return "analysis_only"

    # NC oder unbekannt → ablehnen
    return None


def search_nearby(
    lat: float, lon: float,
    radius_m: int = 5000,
    limit: int = 20,
    timeout_s: float = 10.0,
) -> list[WikimediaImage]:
    """Wikimedia-Geosearch: Bilder im Radius. Filtert direkt auf akzeptable
    Lizenzen — CC-BY-SA-Bilder werden NICHT zurückgegeben (würden den
    Render-Output unter SA stellen).

    Returns leere Liste bei Netzwerk-Fehler — bewusst defensiv, der
    Aufrufer darf annehmen "nichts gefunden" als legitimen Zustand.
    """
    # Schritt 1: Geosearch -> PageIDs im Radius
    params = {
        "action": "query",
        "list": "geosearch",
        "gscoord": "{}|{}".format(lat, lon),
        "gsradius": str(radius_m),  # max 10000
        "gslimit": str(min(limit, 50)),
        "gsnamespace": "6",  # File-Namespace
        "format": "json",
    }
    try:
        r = requests.get(
            WIKIMEDIA_API, params=params,
            headers={"User-Agent": user_agent()},
            timeout=timeout_s,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    pages = data.get("query", {}).get("geosearch", [])
    if not pages:
        return []

    # Schritt 2: ImageInfo + Extmetadata für alle PageIDs (Lizenz + URL)
    pageids = "|".join(str(p["pageid"]) for p in pages)
    params2 = {
        "action": "query",
        "pageids": pageids,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|size",
        "format": "json",
    }
    try:
        r2 = requests.get(
            WIKIMEDIA_API, params=params2,
            headers={"User-Agent": user_agent()},
            timeout=timeout_s,
        )
        r2.raise_for_status()
        data2 = r2.json()
    except Exception:
        return []

    # Geo-Daten nach PageID indexiert für späteren Lookup
    geo_by_pageid = {p["pageid"]: p for p in pages}

    out: list[WikimediaImage] = []
    pages_info = data2.get("query", {}).get("pages", {})
    for pageid_str, page in pages_info.items():
        title = page.get("title", "")
        # Title-Blacklist (NASA/ISS-Fotos, Wappen, Karten, etc.)
        if _title_blacklisted(title):
            continue
        iinfo_list = page.get("imageinfo") or []
        if not iinfo_list:
            continue
        ii = iinfo_list[0]
        ext = ii.get("extmetadata", {}) or {}
        license_short = (ext.get("LicenseShortName") or {}).get("value", "")
        usage = _classify_license(license_short)
        if usage is None:
            continue
        artist_raw = (ext.get("Artist") or {}).get("value", "")
        # Artist kommt teils mit HTML-Tags — grob strippen
        import re
        artist = re.sub(r"<[^>]+>", "", artist_raw).strip() or None

        geo = geo_by_pageid.get(int(pageid_str), {})
        out.append(WikimediaImage(
            title=page.get("title", ""),
            page_url="https://commons.wikimedia.org/?curid={}".format(pageid_str),
            image_url=ii.get("url", ""),
            lat=geo.get("lat", 0.0),
            lon=geo.get("lon", 0.0),
            distance_m=float(geo.get("dist", 0.0)),
            license_short=license_short,
            artist=artist,
            usage=usage,
        ))

    # Nach Distanz sortieren (nächste zuerst)
    out.sort(key=lambda x: x.distance_m)
    return out


def download_image(
    img: WikimediaImage,
    max_dim: int = 1024,
    timeout_s: float = 15.0,
) -> Optional[Image.Image]:
    """Lädt das Bild herunter und resized auf max_dim (Langkante).
    Mutiert auch `img.image`. Returns None bei Fehler."""
    if not img.image_url:
        return None
    try:
        r = requests.get(
            img.image_url,
            headers={"User-Agent": user_agent()},
            timeout=timeout_s,
        )
        r.raise_for_status()
        pil = Image.open(io.BytesIO(r.content)).convert("RGB")
        if max(pil.size) > max_dim:
            pil.thumbnail((max_dim, max_dim), Image.LANCZOS)
        img.image = pil
        return pil
    except Exception:
        return None
