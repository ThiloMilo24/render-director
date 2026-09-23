"""OpenStreetMap Overpass-API — strukturierte Standort-Features als Text.

Liefert benannte Landmarken (Berge, Seen, Siedlungen, Almen, Flüsse, Wälder)
in einem Radius um Lat/Lon. Daten unter ODbL (frei nutzbar mit Attribution).

API-Doku: https://wiki.openstreetmap.org/wiki/Overpass_API
Offizieller Endpoint: https://overpass-api.de/api/interpreter
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from render_director.geo.http import user_agent

OVERPASS_API = "https://overpass-api.de/api/interpreter"


@dataclass
class OsmFeature:
    """Ein benanntes OSM-Feature: Berg, See, Siedlung, etc."""
    name: str
    feature_type: str   # z.B. "peak", "water", "village", "alpine_hut"
    lat: float
    lon: float
    distance_m: float
    elevation_m: Optional[float] = None  # nur bei peaks befüllt


def _bearing_label(from_lat: float, from_lon: float,
                   to_lat: float, to_lon: float) -> str:
    """Liefert grobe Himmelsrichtung als String ('N', 'NO', 'O', ...).
    Approximation, reicht für Director-Briefing."""
    import math
    d_lon = math.radians(to_lon - from_lon)
    lat1 = math.radians(from_lat)
    lat2 = math.radians(to_lat)
    y = math.sin(d_lon) * math.cos(lat2)
    x = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(d_lon))
    bearing_deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    labels = ["N", "NO", "O", "SO", "S", "SW", "W", "NW"]
    idx = int((bearing_deg + 22.5) // 45) % 8
    return labels[idx]


def _haversine_m(lat1: float, lon1: float,
                 lat2: float, lon2: float) -> float:
    """Distanz in Metern zwischen zwei Lat/Lon-Punkten."""
    import math
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def fetch_nearby_features(
    lat: float, lon: float,
    radius_m: int = 5000,
    timeout_s: float = 25.0,
) -> list[OsmFeature]:
    """Holt benannte Geo-Features im Radius. Returns leere Liste bei Fehler.

    Welche Feature-Klassen wir abfragen — kuratiert für Architektur-Kontext:
    - natural=peak (Berge mit Namen + Höhe)
    - natural=water (Seen)
    - place=village/hamlet/town (Siedlungen, gewichtet nach Größe)
    - tourism=alpine_hut (Almen, Schutzhütten)
    - waterway=river (benannte Flüsse)
    - natural=wood + name (benannte Wälder)
    """
    # Overpass-QL — kompakt, ein Query mit mehreren around-Filtern
    q = """
    [out:json][timeout:{timeout}];
    (
      node(around:{r},{lat},{lon})[natural=peak][name];
      node(around:{r},{lat},{lon})[natural=water][name];
      way(around:{r},{lat},{lon})[natural=water][name];
      node(around:{r},{lat},{lon})[place~"^(village|hamlet|town)$"][name];
      node(around:{r},{lat},{lon})[tourism=alpine_hut][name];
      way(around:{r},{lat},{lon})[waterway=river][name];
      way(around:{r},{lat},{lon})[natural=wood][name];
    );
    out center tags;
    """.format(r=radius_m, lat=lat, lon=lon, timeout=int(timeout_s - 2))

    try:
        r = requests.post(
            OVERPASS_API, data={"data": q},
            headers={"User-Agent": user_agent()},
            timeout=timeout_s,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    out: list[OsmFeature] = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue

        # Position: Node hat lat/lon direkt; Way hat center.lat/lon
        f_lat = el.get("lat") or (el.get("center") or {}).get("lat")
        f_lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if f_lat is None or f_lon is None:
            continue

        # Feature-Type vereinheitlichen
        ftype = "unknown"
        if tags.get("natural") == "peak":
            ftype = "peak"
        elif tags.get("natural") == "water":
            ftype = "water"
        elif tags.get("place") in ("village", "hamlet", "town"):
            ftype = tags["place"]
        elif tags.get("tourism") == "alpine_hut":
            ftype = "alpine_hut"
        elif tags.get("waterway") == "river":
            ftype = "river"
        elif tags.get("natural") == "wood":
            ftype = "wood"

        # Höhe nur bei Peaks (ele-Tag)
        elev = None
        ele_raw = tags.get("ele")
        if ele_raw:
            try:
                elev = float(ele_raw)
            except ValueError:
                pass

        out.append(OsmFeature(
            name=name,
            feature_type=ftype,
            lat=f_lat,
            lon=f_lon,
            distance_m=_haversine_m(lat, lon, f_lat, f_lon),
            elevation_m=elev,
        ))

    # Nach Distanz sortieren — nähere Features zuerst
    out.sort(key=lambda f: f.distance_m)
    return out


def format_features_as_text(features: list[OsmFeature],
                            origin_lat: float, origin_lon: float,
                            max_per_type: int = 5) -> str:
    """Formatiert die Features als kompakten Text-Block für den Director.
    Pro Typ max_per_type Einträge, sortiert nach Distanz."""
    if not features:
        return "(keine OSM-Features in der Umgebung gefunden)"

    by_type: dict[str, list[OsmFeature]] = {}
    for f in features:
        by_type.setdefault(f.feature_type, []).append(f)

    # Anzeige-Reihenfolge mit Labels
    type_labels = [
        ("peak", "Berge"),
        ("water", "Gewässer"),
        ("town", "Städte"),
        ("village", "Dörfer"),
        ("hamlet", "Weiler"),
        ("alpine_hut", "Almen / Hütten"),
        ("river", "Flüsse"),
        ("wood", "benannte Wälder"),
    ]

    lines = []
    for key, label in type_labels:
        items = by_type.get(key, [])[:max_per_type]
        if not items:
            continue
        lines.append("  {}:".format(label))
        for f in items:
            bearing = _bearing_label(origin_lat, origin_lon, f.lat, f.lon)
            dist_km = f.distance_m / 1000.0
            extras = []
            if f.elevation_m is not None:
                extras.append("{:.0f} m".format(f.elevation_m))
            extras.append("{:.1f} km {}".format(dist_km, bearing))
            lines.append("    - {} ({})".format(f.name, ", ".join(extras)))

    return "\n".join(lines) if lines else "(keine relevanten Features)"
