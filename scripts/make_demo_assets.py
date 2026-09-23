"""Erzeugt die synthetischen Demo-Daten unter `examples/`.

Alles hier ist prozedural gezeichnet — kein Projektmaterial, keine Fotos:

- `examples/scenes/demo_exterior/`: Wohnhaus mit Satteldach als
  Enscape-artiger Roh-Render (weiße Platzhalter für Nachbarn, Bäume und
  Berge) plus Depth- und Material-ID-Pass und `meta.json`
- `examples/scenes/demo_interior/`: Wohnraum mit Fenster, Sofa, Tisch und
  grauer Platzhalter-Figur, plus Passes und `meta.json`
- `examples/reference_library/`: vier abstrakte Fassaden-„Fotos" mit
  Metadaten im Dateinamen für den Tier-0-Match

Aufruf (idempotent, überschreibt):
    python scripts/make_demo_assets.py

Die Szenen werden mit einer minimalen Lochkamera-Projektion gezeichnet:
konvexe Körper, Rückseiten-Culling, Sortierung nach Tiefe. Depth und
Material-ID nutzen exakt dieselben Polygone wie der Beauty-Render.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
W, H = 1280, 720

Vec = tuple[float, float, float]


# ---------------------------------------------------------------------------
# Mini-Projektion
# ---------------------------------------------------------------------------

def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a: Vec) -> Vec:
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n)


@dataclass
class Camera:
    pos: Vec
    target: Vec
    hfov_deg: float

    def __post_init__(self) -> None:
        self.f = _norm(_sub(self.target, self.pos))
        self.r = _norm(_cross(self.f, (0.0, 0.0, 1.0)))
        self.u = _cross(self.r, self.f)
        self.fpx = (W / 2) / math.tan(math.radians(self.hfov_deg) / 2)

    def depth(self, p: Vec) -> float:
        return _dot(_sub(p, self.pos), self.f)

    def project(self, p: Vec) -> tuple[float, float]:
        d = _sub(p, self.pos)
        z = max(_dot(d, self.f), 0.05)
        return (W / 2 + self.fpx * _dot(d, self.r) / z, H / 2 - self.fpx * _dot(d, self.u) / z)

    def project_poly(self, pts: list[Vec], near: float = 0.1) -> list[tuple[float, float]]:
        """Projiziert ein Polygon, vorher an der Near-Plane abgeschnitten
        (Sutherland-Hodgman) — sonst explodieren Flächen, die hinter die
        Kamera reichen (Innenraum-Wände, Boden)."""
        cam_pts = []
        for p in pts:
            d = _sub(p, self.pos)
            cam_pts.append((_dot(d, self.r), _dot(d, self.u), _dot(d, self.f)))
        clipped = []
        for i, a in enumerate(cam_pts):
            b = cam_pts[(i + 1) % len(cam_pts)]
            a_in, b_in = a[2] >= near, b[2] >= near
            if a_in:
                clipped.append(a)
            if a_in != b_in:
                t = (near - a[2]) / (b[2] - a[2])
                clipped.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), near))
        return [(W / 2 + self.fpx * x / z, H / 2 - self.fpx * y / z) for x, y, z in clipped]

    def horizon_y(self) -> float:
        flat = _norm((self.f[0], self.f[1], 0.0))
        far = (self.pos[0] + flat[0] * 1e5, self.pos[1] + flat[1] * 1e5, self.pos[2])
        return self.project(far)[1]


@dataclass
class Face:
    pts: list[Vec]
    material: str
    outward: bool = True  # False: Normale zeigt nach innen (Raum von innen)


def _normal(face: Face) -> Vec:
    a, b, c = face.pts[0], face.pts[1], face.pts[2]
    n = _norm(_cross(_sub(b, a), _sub(c, a)))
    return n if face.outward else (-n[0], -n[1], -n[2])


def _centroid(pts: list[Vec]) -> Vec:
    k = len(pts)
    return (sum(p[0] for p in pts) / k, sum(p[1] for p in pts) / k, sum(p[2] for p in pts) / k)


def box(x0, y0, z0, x1, y1, z1, material: str, outward: bool = True) -> list[Face]:
    """Quader als sechs Flächen, Punkte gegen den Uhrzeigersinn von außen."""
    p = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    idx = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return [Face([p[i] for i in f], material, outward) for f in idx]


class Renderer:
    """Zeichnet Beauty, Depth und Material-ID aus denselben Polygonen."""

    def __init__(self, cam: Camera, palette: dict, id_colors: dict, sun: Vec,
                 two_sided_light: bool = False) -> None:
        self.cam = cam
        self.two_sided_light = two_sided_light
        self.palette = palette
        self.id_colors = id_colors
        self.sun = _norm(sun)
        self.beauty = Image.new("RGB", (W, H))
        self.depth = Image.new("L", (W, H))
        self.mid = Image.new("RGB", (W, H))
        self.db = ImageDraw.Draw(self.beauty)
        self.dd = ImageDraw.Draw(self.depth)
        self.dm = ImageDraw.Draw(self.mid)

    def _depth_value(self, d: float, near: float, far: float) -> int:
        t = min(1.0, max(0.0, (d - near) / (far - near)))
        return int(255 * (1.0 - t))

    def fill_2d(self, poly, material: str, color, depth_value: int) -> None:
        self.db.polygon(poly, fill=color)
        self.dd.polygon(poly, fill=depth_value)
        self.dm.polygon(poly, fill=self.id_colors[material])

    def faces(self, faces: list[Face], near: float, far: float, cull: bool = True) -> None:
        visible = []
        for face in faces:
            c = _centroid(face.pts)
            if cull and _dot(_normal(face), _sub(self.cam.pos, c)) <= 0:
                continue
            visible.append((self.cam.depth(c), face))
        for d, face in sorted(visible, key=lambda t: -t[0]):
            base = self.palette[face.material]
            ndl = _dot(_normal(face), self.sun)
            if self.two_sided_light:
                light = 0.72 + 0.28 * abs(ndl)
            else:
                light = 0.55 + 0.45 * max(0.0, ndl)
            color = tuple(min(255, int(ch * light)) for ch in base)
            poly = self.cam.project_poly(face.pts)
            if len(poly) < 3:
                continue
            self.fill_2d(poly, face.material, color, self._depth_value(d, near, far))

    def save(self, folder: Path, prefix: str) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.beauty.filter(ImageFilter.SMOOTH).save(folder / f"{prefix}_Beauty.png", optimize=True)
        self.depth.save(folder / f"{prefix}_Depth.png", optimize=True)
        self.mid.save(folder / f"{prefix}_Material_ID.png", optimize=True)


def _sky(draw: ImageDraw.ImageDraw, horizon: int, top=(170, 196, 222), bottom=(226, 234, 240)) -> None:
    for y in range(0, max(1, horizon)):
        t = y / max(1, horizon)
        draw.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))


# ---------------------------------------------------------------------------
# Exterior
# ---------------------------------------------------------------------------

def make_exterior() -> None:
    cam = Camera(pos=(-19.0, -25.0, 1.7), target=(4.0, 4.0, 4.0), hfov_deg=74.0)
    palette = {
        "placeholder": (242, 242, 240), "ground": (138, 150, 118), "path": (214, 210, 202),
        "larch": (150, 112, 82), "concrete": (178, 176, 170), "zinc": (92, 96, 102),
        "glass": (62, 74, 86), "mountain": (236, 238, 240),
    }
    id_colors = {
        "placeholder": (255, 0, 255), "ground": (0, 160, 0), "path": (255, 200, 0),
        "larch": (200, 60, 0), "concrete": (0, 120, 255), "zinc": (120, 0, 200),
        "glass": (0, 220, 220), "mountain": (255, 120, 180), "sky": (0, 0, 0),
    }
    r = Renderer(cam, palette, id_colors, sun=(0.5, -0.7, 0.6))
    hy = int(cam.horizon_y())
    _sky(r.db, hy)
    r.dm.rectangle([0, 0, W, hy], fill=id_colors["sky"])

    # Weiße Berg-Platzhalter hinter dem Horizont (Enscape-Default-Volumen)
    ridge = [(0, hy), (140, hy - 120), (300, hy - 60), (470, hy - 170), (650, hy - 80),
             (820, hy - 150), (1000, hy - 70), (1160, hy - 130), (W, hy - 60), (W, hy)]
    r.fill_2d(ridge, "mountain", palette["mountain"], 8)
    # Gelände unterhalb des Horizonts, Tiefe als Bänder
    for i, y in enumerate(range(hy, H, 12)):
        t = (y - hy) / max(1, H - hy)
        shade = tuple(int(c * (0.9 + 0.15 * t)) for c in palette["ground"])
        r.db.rectangle([0, y, W, y + 12], fill=shade)
        r.dd.rectangle([0, y, W, y + 12], fill=int(40 + 215 * t))
    r.dm.rectangle([0, hy, W, H], fill=id_colors["ground"])

    # Terrasse vor der Südfassade und abgeschnittene helle Geländekante
    terrace = [(-10.0, -9.0, 0.02), (12.0, -9.0, 0.02), (12.0, -4.0, 0.02), (-10.0, -4.0, 0.02)]
    band = [(-14.0, -10.5, 0.03), (16.0, -10.5, 0.03), (16.0, -9.0, 0.03), (-14.0, -9.0, 0.03)]
    r.faces([Face(terrace, "path"), Face(band, "placeholder")], near=5, far=140, cull=False)

    # Weiße Nachbar-Platzhalter (fern, zuerst)
    neighbours = box(-60, 40, 0, -44, 52, 8, "placeholder") + box(34, 48, 0, 50, 58, 10, "placeholder")
    r.faces(neighbours, near=5, far=140)

    # Weiße Baum-Platzhalter (Kugel auf Stamm). Bäume hinter dem Haus
    # werden vor ihm gezeichnet, Bäume davor danach.
    def draw_tree(tx: float, ty: float) -> None:
        d = cam.depth((tx, ty, 3.0))
        x, y = cam.project((tx, ty, 4.2))
        _, base_y = cam.project((tx, ty, 0.0))
        rad = cam.fpx * 2.6 / d
        dv = r._depth_value(d, 5, 140)
        r.fill_2d([(x - rad * 0.12, y), (x + rad * 0.12, y), (x + rad * 0.12, base_y), (x - rad * 0.12, base_y)],
                  "placeholder", (225, 225, 222), dv)
        for draw, fill in ((r.db, palette["placeholder"]), (r.dd, dv), (r.dm, id_colors["placeholder"])):
            draw.ellipse([x - rad, y - rad * 1.2, x + rad, y + rad * 0.6], fill=fill)

    trees = sorted([(-22.0, 2.0), (24.0, 10.0), (30.0, 24.0), (-30.0, 22.0), (18.0, -14.0)],
                   key=lambda t: -cam.depth((t[0], t[1], 3.0)))
    house_depth = cam.depth((1.0, 2.0, 4.0))
    for tx, ty in trees:
        if cam.depth((tx, ty, 3.0)) > house_depth:
            draw_tree(tx, ty)

    # Hauptbaukörper: Betonsockel, Lärchen-Obergeschosse, Satteldach
    r.faces(box(-8, -4, 0, 10, 8, 1.2, "concrete"), near=5, far=140)
    walls = box(-8, -4, 1.2, 10, 8, 7.5, "larch")
    roof_l = Face([(-8, -4, 7.5), (10, -4, 7.5), (10, 2, 11.0), (-8, 2, 11.0)], "zinc")
    roof_r = Face([(10, 8, 7.5), (-8, 8, 7.5), (-8, 2, 11.0), (10, 2, 11.0)], "zinc")
    gable_w = Face([(-8, 8, 7.5), (-8, -4, 7.5), (-8, 2, 11.0)], "larch")
    gable_e = Face([(10, -4, 7.5), (10, 8, 7.5), (10, 2, 11.0)], "larch")
    r.faces(walls + [roof_l, roof_r, gable_w, gable_e], near=5, far=140)

    # Fenster auf den sichtbaren Fassaden (Süd = y -4, West = x -8), leicht vorgesetzt
    windows = []
    for z0, z1 in ((2.0, 4.0), (4.8, 6.8)):
        for x0 in (-6.5, -2.5, 1.5, 5.5):
            windows.append(Face([(x0, -4.02, z0), (x0 + 2.2, -4.02, z0), (x0 + 2.2, -4.02, z1), (x0, -4.02, z1)], "glass"))
        for y0 in (-2.0, 3.0):
            windows.append(Face([(-8.02, y0 + 2.0, z0), (-8.02, y0, z0), (-8.02, y0, z1), (-8.02, y0 + 2.0, z1)], "glass"))
    r.faces(windows, near=5, far=140)

    for tx, ty in trees:
        if cam.depth((tx, ty, 3.0)) <= house_depth:
            draw_tree(tx, ty)

    folder = EXAMPLES / "scenes" / "demo_exterior"
    r.save(folder, "DemoHouse_South")
    meta = {
        "project_name": "Demo House",
        "project_type": "Wohnbau",
        "location": "Innsbruck, Österreich (fiktiver Standort)",
        "facade_orientation": "Süd-Südwest",
        "materials": {
            "Fassade": "Lärchenholz-Schalung vertikal, natur vergraut",
            "Sockel": "Sichtbeton, glatt",
            "Dach": "Zink Stehfalz, dunkelgrau",
            "Fenster": "Holz-Alu, anthrazit, Dreifachverglasung",
            "Außenboden": "Natursteinplatten, hell",
        },
        "camera_focal_length_mm": 24.0,
        "camera_height_m": 1.7,
        "time_of_day": "später Vormittag",
        "site_context": {"active": False, "source": "none", "neighbors_visible": False, "notes": ""},
        "view_type": "exterior",
        "site_location": {
            "latitude_deg": 47.2692,
            "longitude_deg": 11.4041,
            "elevation_m": 574.0,
            "time_zone_utc_offset_h": 1.0,
            "place_name": "Innsbruck",
        },
        "notes": (
            "Synthetische Demo-Szene (scripts/make_demo_assets.py). Weiße Volumen "
            "sind Enscape-Platzhalter für Nachbarbauten, Bäume und Berge."
        ),
    }
    (folder / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Interior
# ---------------------------------------------------------------------------

def make_interior() -> None:
    cam = Camera(pos=(1.0, 0.6, 1.6), target=(3.2, 6.0, 1.35), hfov_deg=78.0)
    palette = {
        "plaster": (232, 230, 224), "larch": (196, 160, 118), "oak": (170, 128, 88),
        "window": (214, 230, 242), "fabric": (205, 198, 184), "placeholder": (150, 150, 150),
    }
    id_colors = {
        "plaster": (255, 220, 0), "larch": (200, 60, 0), "oak": (120, 70, 20),
        "window": (0, 220, 220), "fabric": (0, 120, 255), "placeholder": (255, 0, 255),
    }
    r = Renderer(cam, palette, id_colors, sun=(-0.4, -0.6, 0.7), two_sided_light=True)
    room = box(0, 0, 0, 6, 7, 3, "plaster", outward=False)
    room[0].material = "oak"    # Boden
    room[1].material = "larch"  # Decke
    r.faces(room, near=0.5, far=9)
    window = Face([(1.6, 6.98, 0.6), (4.4, 6.98, 0.6), (4.4, 6.98, 2.5), (1.6, 6.98, 2.5)], "window", outward=False)
    r.faces([window], near=0.5, far=9, cull=False)
    furniture = box(3.4, 3.8, 0, 5.6, 4.8, 0.45, "fabric") + box(3.4, 4.5, 0.45, 5.6, 4.8, 0.85, "fabric")
    table = box(2.1, 3.9, 0, 2.9, 4.6, 0.42, "oak")
    r.faces(table, near=0.5, far=9)
    r.faces(furniture, near=0.5, far=9)

    # Graue Platzhalter-Figur (Enscape-Mannequin)
    d = cam.depth((1.6, 5.2, 0.9))
    x, top = cam.project((1.6, 5.2, 1.75))
    _, foot = cam.project((1.6, 5.2, 0.0))
    body_w = cam.fpx * 0.22 / d
    dv = r._depth_value(d, 0.5, 9)
    r.fill_2d([(x - body_w, top + body_w * 2.4), (x + body_w, top + body_w * 2.4), (x + body_w * 0.8, foot), (x - body_w * 0.8, foot)],
              "placeholder", palette["placeholder"], dv)
    for draw, fill in ((r.db, palette["placeholder"]), (r.dd, dv), (r.dm, id_colors["placeholder"])):
        draw.ellipse([x - body_w * 0.8, top, x + body_w * 0.8, top + body_w * 2.0], fill=fill)

    folder = EXAMPLES / "scenes" / "demo_interior"
    r.save(folder, "DemoHouse_Living")
    meta = {
        "project_name": "Demo House",
        "project_type": "Wohnbau",
        "location": "Innsbruck, Österreich (fiktiver Standort)",
        "facade_orientation": "Nord (Hauptfenster)",
        "materials": {
            "Wandbelag": "Kalkputz warmweiß",
            "Deckenfinish": "Lärchen-Schalung sichtbar, geölt",
            "Bodenbelag": "Eiche breitdielig, geölt",
            "Möbel": "Sofa Naturleinen, Beistelltisch massiv Eiche",
            "Fensterleibung": "Eiche, tief",
        },
        "camera_focal_length_mm": 24.0,
        "camera_height_m": 1.6,
        "time_of_day": "Frühabend",
        "view_type": "interior",
        "program_type": "wohnraum",
        "furniture_state": "placeholder",
        "notes": "Synthetische Demo-Szene (scripts/make_demo_assets.py). Graue Figur = Enscape-Mannequin.",
    }
    (folder / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Referenzbibliothek
# ---------------------------------------------------------------------------

_FACADE_COLORS = {
    "wood": (150, 112, 82), "glass": (70, 88, 104), "plaster": (230, 226, 216),
    "stone": (168, 160, 150), "metal": (110, 114, 120),
}
_SKY = {"day": (160, 196, 228), "dusk": (232, 170, 120), "overcast": (196, 200, 204)}


def _reference_photo(material: str, time: str) -> Image.Image:
    """Abstrakte Fassadenansicht: Himmel, Gelände, Baukörper aus Materialbändern."""
    img = Image.new("RGB", (640, 427), _SKY[time])
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 300, 640, 427], fill=(120, 136, 104))
    parts = [m for m in _FACADE_COLORS if m in material] or ["plaster"]
    x0, x1, y0, y1 = 120, 520, 120, 320
    band = (y1 - y0) // len(parts)
    for i, part in enumerate(parts):
        draw.rectangle([x0, y0 + i * band, x1, y0 + (i + 1) * band], fill=_FACADE_COLORS[part])
    draw.polygon([(x0 - 20, y0), (320, 60), (x1 + 20, y0)], fill=(88, 92, 98))
    for wx in range(150, 500, 80):
        draw.rectangle([wx, 200, wx + 40, 260], fill=(52, 62, 72))
    if time == "dusk":
        img = Image.blend(img, Image.new("RGB", img.size, (60, 40, 70)), 0.25)
    return img.filter(ImageFilter.GaussianBlur(0.6))


def make_reference_library() -> None:
    entries = [
        ("Demo Lodge", "demolodge_001_residential_woodglass_day_front_medium", "Holzfassade mit großen Fenstern, Tageslicht."),
        ("Demo Lodge", "demolodge_002_residential_woodglass_dusk_corner_far", "Holzfassade in der Dämmerung, Innenlicht an."),
        ("Stone House", "stonehouse_001_house_stoneplaster_overcast_front_close", "Natursteinsockel mit Putz, bedeckter Himmel."),
        ("Stone House", "stonehouse_002_house_stoneplaster_day_corner_medium", "Natursteinsockel mit Putz, Tageslicht."),
    ]
    for folder, stem, caption in entries:
        target = EXAMPLES / "reference_library" / folder
        target.mkdir(parents=True, exist_ok=True)
        parts = stem.split("_")
        _reference_photo(parts[3], parts[4]).save(target / f"{stem}.jpg", quality=82, optimize=True)
        (target / f"{stem}.txt").write_text(caption + "\n", encoding="utf-8")


if __name__ == "__main__":
    make_exterior()
    make_interior()
    make_reference_library()
    print("Demo-Daten geschrieben nach", EXAMPLES)
