"""Wikidata-basierter Lead-Image-Fetcher für Geo-Places.

WORKAROUND-HISTORIE (2026-05-27): Ursprünglich als Wikipedia-PageImages-
Fetcher gebaut, aber `*.wikipedia.org` war im Entwicklungsnetz blockiert.
Pivot auf **Wikidata SPARQL** — selber Datenpool (Wikidata-Editoren
pflegen die P18-Image-Property auf jeder Place-Entity), aber andere
Subdomain (`query.wikidata.org`) die nicht blockiert wird.

Vorteile gegenüber Wikimedia-Commons-Geosearch:
  - **Kuratiert**: P18 ist redaktionell als Hauptbild markiert, nicht
    crowdsourced Schnappschuss. Fast immer Landschaft/Architektur.
  - **Entity-bezogen**: ein Bild pro Place (z.B. ein Bergsee), nicht
    50 Bilder eines Bahnhofs.
  - **Native Distanz-Filterung** via `wikibase:around` SPARQL-Service.

Lizenz-Check + Bild-Download laufen weiter über `commons.wikimedia.org`
(die ist offen).
"""
from __future__ import annotations

import re
from typing import Optional

import requests

from render_director.geo.http import user_agent
from render_director.geo.osm_overpass import OsmFeature
from render_director.geo.wikimedia import (
    WIKIMEDIA_API,
    WikimediaImage,
    _classify_license,
    _title_blacklisted,
)

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"


def _sparql_nearby_with_images(
    lat: float, lon: float,
    radius_km: float = 5.0,
    limit: int = 25,
    timeout_s: float = 20.0,
) -> list[dict]:
    """SPARQL-Query: Wikidata-Entities im Radius mit P18-Image.

    Returns Liste von {qid, label, image_url, distance_km, instance_label}.
    Sortiert nach Distanz aufsteigend.
    """
    # SPARQL als Plain-String (kein .format mit Lat/Lon weil SPARQL
    # eigene {}-Syntax hat). Statt dessen %s-Subst.
    query = (
        'SELECT ?item ?itemLabel ?image ?distance ?instanceLabel WHERE {\n'
        '  SERVICE wikibase:around {\n'
        '    ?item wdt:P625 ?coord .\n'
        '    bd:serviceParam wikibase:center '
        '"Point(' + str(lon) + ' ' + str(lat) + ')"^^geo:wktLiteral .\n'
        '    bd:serviceParam wikibase:radius "' + str(radius_km) + '" .\n'
        '    bd:serviceParam wikibase:distance ?distance .\n'
        '  }\n'
        '  ?item wdt:P18 ?image .\n'
        '  OPTIONAL { ?item wdt:P31 ?instance . }\n'
        '  SERVICE wikibase:label { '
        'bd:serviceParam wikibase:language "de,it,en,[AUTO_LANGUAGE]" }\n'
        '}\n'
        'ORDER BY ?distance\n'
        'LIMIT ' + str(limit)
    )
    try:
        r = requests.get(
            WIKIDATA_SPARQL,
            params={"query": query, "format": "json"},
            headers={
                "User-Agent": user_agent(),
                "Accept": "application/sparql-results+json",
            },
            timeout=timeout_s,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    out = []
    for binding in data.get("results", {}).get("bindings", []):
        qid_uri = binding.get("item", {}).get("value", "")
        qid = qid_uri.rsplit("/", 1)[-1] if qid_uri else None
        out.append({
            "qid": qid,
            "label": binding.get("itemLabel", {}).get("value", ""),
            "image_url": binding.get("image", {}).get("value", ""),
            "distance_km": float(binding.get("distance", {}).get("value", 0)),
            "instance_label": binding.get("instanceLabel", {}).get("value", ""),
        })
    return out


def _commons_filename_from_url(url: str) -> Optional[str]:
    """Wandelt Wikidata-P18-URL (Special:FilePath/<name>) in den File:Titel
    für die Commons-API um. URL ist URL-encoded — also dekodieren + 'File:' prefixen.
    """
    if not url:
        return None
    # P18-URL hat typischerweise Form:
    #   http://commons.wikimedia.org/wiki/Special:FilePath/<URL-encoded-name>
    m = re.search(r"Special:FilePath/(.+)$", url)
    if not m:
        return None
    from urllib.parse import unquote
    name = unquote(m.group(1))
    # Underscores in Wikidata-Encoded-Form bleiben drin, das ist OK für
    # die Commons-API (akzeptiert beides).
    return "File:" + name


def _fetch_commons_imageinfo(file_title: str,
                             timeout_s: float = 8.0) -> Optional[dict]:
    """Lizenz + URL über Commons-API."""
    params = {
        "action": "query",
        "titles": file_title,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|size",
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
        return None

    pages = data.get("query", {}).get("pages", {})
    for _, page in pages.items():
        iinfo_list = page.get("imageinfo") or []
        if iinfo_list:
            return iinfo_list[0]
    return None


def fetch_leads_for_features(
    features: list[OsmFeature],
    origin_lat: float, origin_lon: float,
    max_per_type: int = 2,        # ignoriert in Wikidata-Variante (SPARQL macht eigene Sortierung)
    overall_limit: int = 5,
) -> list[WikimediaImage]:
    """Wikidata-SPARQL-Variante: holt Entities im Radius mit kuratiertem
    P18-Image, filtert über Lizenz + Title-Blacklist.

    `features` wird nicht direkt genutzt (SPARQL deckt das ab), bleibt
    aber im Interface damit der Aufrufer austauschbar bleibt.
    """
    candidates = _sparql_nearby_with_images(
        origin_lat, origin_lon, radius_km=5.0, limit=25,
    )

    out: list[WikimediaImage] = []
    seen: set[str] = set()
    for c in candidates:
        if len(out) >= overall_limit:
            break
        file_title = _commons_filename_from_url(c["image_url"])
        if not file_title or file_title in seen:
            continue
        if _title_blacklisted(file_title):
            continue
        seen.add(file_title)

        ii = _fetch_commons_imageinfo(file_title)
        if not ii:
            continue
        ext = ii.get("extmetadata", {}) or {}
        license_short = (ext.get("LicenseShortName") or {}).get("value", "")
        usage = _classify_license(license_short)
        if usage is None:
            continue
        artist_raw = (ext.get("Artist") or {}).get("value", "")
        artist = re.sub(r"<[^>]+>", "", artist_raw).strip() or None

        # Page-URL für Attribution: Wikidata-Entity-Page (statt Wikipedia-
        # Article weil wikipedia.org-Subdomain im Netzwerk blockiert ist).
        page_url = "https://www.wikidata.org/wiki/" + (c["qid"] or "")
        out.append(WikimediaImage(
            title=file_title,
            page_url=page_url,
            image_url=ii.get("url", ""),
            lat=origin_lat,  # exakte Entity-Coords haben wir nicht direkt zur Hand
            lon=origin_lon,
            distance_m=c["distance_km"] * 1000.0,
            license_short=license_short,
            artist=artist,
            usage=usage,
        ))
    return out
