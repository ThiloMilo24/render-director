# -*- coding: utf-8 -*-
"""Render Director — Start-Render (Render-First-1-Klick-Workflow).

Phase-4.5-Erweiterung 2026-05-26 (revidiert): Render-First-Architektur.

User-Flow:
  1. In Enscape rendern (Screenshot) und in den konfigurierten
     Enscape-Output-Ordner speichern.
  2. Zu Revit zurück, auf die View navigieren, diesen Button klicken.

Was der Klick macht:
  1. Aktive View + Projekt-Pfad aus Revit ableiten.
  2. Jüngsten Render der letzten 24 Stunden im Enscape-Output-Ordner
     suchen. Wenn nichts gefunden -> File-Picker als Fallback.
  3. Bestätigungs-Dialog: Quelle + Ziel + Pass-Geschwister.
     User-Optionen: Weiter / Andere Render-Datei waehlen / Abbrechen.
  4. materials_legend.json + meta.json in <Projekt>/Revit Render/<View>/
     schreiben.
  5. Beauty + Pass-Files (MaterialID, ObjectID, Depth) per
     shutil.move() vom Enscape-Ordner ins Snapshot-Folder verschieben.
     Original-Dateien sind danach weg.
  6. Webapp pingen, Chrome --app auf rechter Bildschirmhaelfte starten
     mit ?snapshot=<View>&project_dir=<Revit Render-Root>.

Kein State zwischen Klicks. Kein pending_render. Eine config.json mit
nur `enscape_output_dir` als persistentem Wert.
"""

import codecs
import io
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

from Autodesk.Revit.DB import (
    BuiltInParameter,
    FilteredElementCollector,
    Material,
)
from pyrevit import forms, revit, script

doc = revit.doc
output = script.get_output()


# ----------------------------------------------------------------------
# Konstanten

INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\r\n\t]')
FEET_TO_M = 0.3048

APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
CONFIG_DIR = os.path.join(APPDATA, "render_director")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# Folder-Name unter dem Projekt-Verzeichnis. Hat seit 2026-06-19 ein
# Leerzeichen statt Underscore (User-Wunsch — sauberer im File-Explorer).
# Alte 'Revit Render'-Folder bleiben lesbar via `?project_dir=…`, neue
# Exports landen hier.
REVIT_RENDER_FOLDER = "Revit Render"

# Sequenzielle View-Nummerierung pro Projekt: das Mapping
# `<revit_view_name_raw> -> <view_number>` liegt in dieser JSON-Datei
# direkt unter `<Projekt>/Revit Render/`. Beim wiederholten Export einer
# bekannten View wird dieselbe Nummer + derselbe Folder genutzt
# (Refresh-Workflow); bei einer neuen View wird die naechste freie
# Nummer vergeben.
VIEW_INDEX_FILENAME = "_view_index.json"

IMAGE_EXTS = (".png", ".jpg", ".jpeg")

TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

# Frische-Fenster fuer Auto-Detection: wenn juengste PNG im Enscape-
# Ordner aelter ist als das, faellt der Code auf File-Picker zurueck.
# 24h ist absichtlich grosszuegig - der Confirm-Dialog zeigt das Alter
# der gewaehlten Datei und schuetzt vor "aus Versehen den Vortags-
# Render mitgenommen"-Fehlern.
MAX_RENDER_AGE_S = 86400  # 24 Stunden

# (Kein Sibling-mtime-Fenster mehr - die Pass-Detection laeuft jetzt
# rein ueber den Filename-Prefix, siehe _build_render_set_for_prefix.)

# Enscape-Pass-Suffix-Mapping. Wird im _split_render_filename gegen das
# letzte Underscore-Segment matched (case-insensitiv, "-" toleriert).
# Bekannte Enscape-Suffixe: _Depth, _MaterialID, _ObjectID, _AlphaMask,
# _Normal, _AmbientOcclusion. Wert "_ignore" = Pass existiert, soll aber
# nicht in den Snapshot-Folder uebernommen werden.
# Wert "beauty" = explizit als Beauty markierte Datei (falls User
# manuell so benannt hat); ohne erkanntes Suffix wird ein File ebenfalls
# als Beauty interpretiert.
_PASS_SUFFIX_MAP = {
    "beauty": "beauty",
    "materialid": "material_id",
    "objectid": "object_id",
    "depth": "depth",
    "alpha": "_ignore",
    "alphamask": "_ignore",
    "normal": "_ignore",
    "ambientocclusion": "_ignore",
    "ao": "_ignore",
}

PASS_TARGET_SUFFIX = {
    "beauty": "_Beauty",
    "material_id": "_MaterialID",
    "object_id": "_ObjectID",
    "depth": "_Depth",
}


# ----------------------------------------------------------------------
# Helpers - Pfade & IO

def sanitize_path_segment(name):
    cleaned = INVALID_PATH_CHARS.sub("_", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "unnamed"


def write_json(path, payload_dict):
    payload = json.dumps(payload_dict, indent=2, ensure_ascii=False, sort_keys=False)
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)


def load_config():
    if not os.path.isfile(CONFIG_FILE):
        return {}
    try:
        with io.open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.loads(f.read())
    except Exception:
        return {}


def save_config(cfg):
    if not os.path.isdir(CONFIG_DIR):
        os.makedirs(CONFIG_DIR)
    payload = json.dumps(cfg, indent=2, ensure_ascii=False, sort_keys=True)
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    with io.open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(payload)


def remember_picked_folder(cfg, picked_path):
    """Selbst-Lern-Mechanismus: nach manueller File-Wahl den Parent-
    Ordner als neuen enscape_output_dir merken. So lernt das Tool sich
    selbst auf den Pfad ein, wo Enscape de facto speichert - beim
    naechsten Klick funktioniert die Auto-Suche dann ohne Picker."""
    if not picked_path:
        return
    folder = os.path.dirname(picked_path)
    if not folder or not os.path.isdir(folder):
        return
    if cfg.get("enscape_output_dir") == folder:
        return  # bereits gesetzt
    cfg["enscape_output_dir"] = folder
    save_config(cfg)


def format_age(seconds):
    """Mensch-lesbare Alters-Angabe: '34 s', '4 min 12 s', '1 h 23 min'."""
    seconds = int(seconds)
    if seconds < 60:
        return "{} s".format(seconds)
    if seconds < 3600:
        return "{} min {} s".format(seconds // 60, seconds % 60)
    return "{} h {} min".format(seconds // 3600, (seconds % 3600) // 60)


# ----------------------------------------------------------------------
# Helpers - Material-Export

def safe_get_param_string(element, builtin_param):
    try:
        param = element.get_Parameter(builtin_param)
        if param is None:
            return None
        value = param.AsString()
        if value is None or value == "":
            return None
        return value
    except Exception:
        return None


def get_shading_rgb(material):
    try:
        color = material.Color
        if color is None:
            return None
        return [int(color.Red), int(color.Green), int(color.Blue)]
    except Exception:
        return None


def get_appearance_asset_name(material, doc):
    try:
        asset_id = material.AppearanceAssetId
        if asset_id is None or asset_id.IntegerValue <= 0:
            return None
        asset_element = doc.GetElement(asset_id)
        if asset_element is None:
            return None
        return asset_element.Name
    except Exception:
        return None


def get_material_klasse(material):
    try:
        value = material.MaterialClass
        return value if value else None
    except Exception:
        return None


def material_to_dict(material, doc):
    return {
        "name": material.Name,
        "shading_rgb": get_shading_rgb(material),
        "klasse": get_material_klasse(material),
        "beschreibung": safe_get_param_string(
            material, BuiltInParameter.ALL_MODEL_DESCRIPTION
        ),
        "appearance_asset_name": get_appearance_asset_name(material, doc),
    }


def feet_to_m(value):
    try:
        return round(float(value) * FEET_TO_M, 4)
    except Exception:
        return None


def safe_attr(obj, attr_name, default=None):
    try:
        value = getattr(obj, attr_name, default)
        if isinstance(value, str) and value.strip() == "":
            return None
        return value
    except Exception:
        return default


def get_project_info(doc):
    pi = doc.ProjectInformation
    info = {}
    for attr in [
        "Name", "Number", "ClientName", "Address", "BuildingName",
        "OrganizationName", "OrganizationDescription", "IssueDate",
        "Status", "Author",
    ]:
        info[attr] = safe_attr(pi, attr)
    return info


def get_site_location_info(doc):
    """Liest doc.SiteLocation: Breitengrad/Laengengrad (Radiant->Grad),
    Hoehe (Feet->Meter), Zeitzone (Float-Stunden UTC-Offset).

    Defensive: wenn die Location nicht gesetzt ist (neues Projekt) oder
    die API failed, alle Felder bleiben None. Lat/Lon als (0.0, 0.0)
    sind ein Indikator fuer "nicht gesetzt" - geben wir explizit None
    zurueck, damit der Director-Block sauber unterdrueckt werden kann.
    """
    import math
    info = {
        "latitude_deg": None,
        "longitude_deg": None,
        "elevation_m": None,
        "time_zone_utc_offset_h": None,
        "place_name": None,
    }
    try:
        sl = doc.SiteLocation
        if sl is None:
            return info
        lat_rad = safe_attr(sl, "Latitude")
        lon_rad = safe_attr(sl, "Longitude")
        if lat_rad is not None:
            lat_deg = math.degrees(float(lat_rad))
            if abs(lat_deg) > 0.001:  # 0/0 = nicht gesetzt
                info["latitude_deg"] = round(lat_deg, 6)
        if lon_rad is not None:
            lon_deg = math.degrees(float(lon_rad))
            if abs(lon_deg) > 0.001:
                info["longitude_deg"] = round(lon_deg, 6)
        elev_ft = safe_attr(sl, "Elevation")
        if elev_ft is not None:
            info["elevation_m"] = feet_to_m(elev_ft)
        tz = safe_attr(sl, "TimeZone")
        if tz is not None:
            info["time_zone_utc_offset_h"] = round(float(tz), 2)
        # `PlaceName` ist in neueren Revit-Versionen verfuegbar
        # (Site-Location-Name aus Bing-Map-Search). Defensive read.
        place = safe_attr(sl, "PlaceName")
        if place:
            info["place_name"] = place
    except Exception:
        pass
    return info


def get_view_info(view):
    info = {
        "view_name": safe_attr(view, "Name"),
        "view_type": str(safe_attr(view, "ViewType")),
        "is_perspective": None,
        "camera_position_m": None,
        "camera_height_m": None,
        "forward_direction": None,
        "up_direction": None,
        "crop_box_active": None,
    }
    try:
        info["is_perspective"] = bool(view.IsPerspective)
    except Exception:
        pass
    try:
        orientation = view.GetOrientation()
        eye = orientation.EyePosition
        fwd = orientation.ForwardDirection
        up = orientation.UpDirection
        info["camera_position_m"] = [feet_to_m(eye.X), feet_to_m(eye.Y), feet_to_m(eye.Z)]
        info["camera_height_m"] = feet_to_m(eye.Z)
        info["forward_direction"] = [round(fwd.X, 4), round(fwd.Y, 4), round(fwd.Z, 4)]
        info["up_direction"] = [round(up.X, 4), round(up.Y, 4), round(up.Z, 4)]
    except Exception:
        pass
    try:
        info["crop_box_active"] = bool(view.CropBoxActive)
    except Exception:
        pass
    return info


def get_used_material_ids_in_view(doc, view):
    used = set()
    try:
        collector = FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType()
    except Exception:
        return used
    for element in collector:
        for include_paint in (False, True):
            try:
                mat_ids = element.GetMaterialIds(include_paint)
            except Exception:
                continue
            if not mat_ids:
                continue
            for mid in mat_ids:
                try:
                    if mid is not None and mid.IntegerValue > 0:
                        used.add(mid.IntegerValue)
                except Exception:
                    continue
    return used


# ----------------------------------------------------------------------
# Helpers - Enscape-Output-Suche

def get_enscape_output_dir(cfg):
    """Lese gespeicherten Enscape-Pfad oder frag den User einmalig.

    Persistenz: einmal via Folder-Picker eingegeben, in config.json
    gespeichert. Bei spaeteren Klicks wird daraus gelesen. Wenn der
    gespeicherte Pfad nicht mehr existiert -> Re-Prompt.
    """
    cached = cfg.get("enscape_output_dir")
    if cached and os.path.isdir(cached):
        return cached
    forms.alert(
        "Bitte wähle im nächsten Dialog dein Enscape-Output-"
        "Verzeichnis.\n\n"
        "Das ist der Ordner, in den du in Enscape die Renderings "
        "(Screenshot) ablegst. Der Pfad wird einmalig in "
        "%APPDATA%\\render_director\\config.json gespeichert.",
        title="Render Director - Einmalige Konfiguration",
    )
    selected = forms.pick_folder(title="Enscape-Output-Verzeichnis waehlen")
    if not selected:
        return None
    cfg["enscape_output_dir"] = selected
    save_config(cfg)
    return selected


def _split_render_filename(filename):
    """Parst Render-Filename in (prefix, pass_type, ext).

    Enscape-Convention: `Enscape_<timestamp>.png` (Beauty) +
    `Enscape_<timestamp>_<pass>.png` (Passes). User-eigene Prefixe
    funktionieren genauso (`Lobby01.png` + `Lobby01_materialId.png`).

    Returns:
        prefix:     Dateiname-Stem mit Pass-Suffix entfernt
        pass_type:  'beauty' | 'material_id' | 'object_id' | 'depth'
                    | '_ignore' (Pass den wir nicht uebernehmen)
                    | None  (sollte nicht vorkommen, Fallback = Beauty)
        ext:        Lowercase-Endung inkl. Punkt
    """
    base, ext = os.path.splitext(filename)
    ext = ext.lower()

    # Letztes Underscore-Segment isolieren und gegen Map matchen.
    parts = base.rsplit("_", 1)
    if len(parts) == 2:
        prefix_candidate, last = parts
        last_norm = last.lower().replace("-", "")
        if last_norm in _PASS_SUFFIX_MAP:
            return prefix_candidate, _PASS_SUFFIX_MAP[last_norm], ext

    # Kein erkanntes Suffix -> ganzer Stem ist Prefix, Datei ist Beauty.
    return base, "beauty", ext


def _categorize_pass(filename):
    """Kompatibilitaets-Wrapper: liefert nur den pass_type fuer Code,
    der die alte Single-Return-Funktion erwartet."""
    _, pass_type, _ = _split_render_filename(filename)
    if pass_type == "beauty":
        return None  # alte Semantik: cat=None bedeutet Beauty
    return pass_type


def _build_render_set_for_prefix(folder, prefix):
    """Sammelt alle Files in `folder` deren Prefix mit `prefix` matched.
    Liefert ein render_set-Dict mit Beauty + Pass-Slots."""
    result = {
        "beauty": None,
        "material_id": None,
        "object_id": None,
        "depth": None,
        "_newest_mtime": 0.0,
        "_prefix": prefix,
    }
    if not os.path.isdir(folder):
        return result
    for entry in os.listdir(folder):
        p = os.path.join(folder, entry)
        if not (os.path.isfile(p) and entry.lower().endswith(IMAGE_EXTS)):
            continue
        entry_prefix, pass_type, _ = _split_render_filename(entry)
        if entry_prefix != prefix:
            continue
        if pass_type == "_ignore":
            continue
        # Beauty + andere Passes ihrem Slot zuweisen, ersten Treffer behalten.
        slot = "beauty" if pass_type == "beauty" else pass_type
        if slot in result and result[slot] is None:
            result[slot] = p
            try:
                mtime = os.path.getmtime(p)
                if mtime > result["_newest_mtime"]:
                    result["_newest_mtime"] = mtime
            except Exception:
                pass
    return result


def find_render_set(folder, max_age_s=MAX_RENDER_AGE_S):
    """Findet das juengste Render-Set im Ordner (Prefix-basiert).

    Algorithmus:
      1. Juengste PNG/JPG im Ordner finden.
      2. Aus dem Filename den Prefix ableiten (z.B. `Enscape_2026-05-26-
         17-20-35`).
      3. Alle Files mit gleichem Prefix einsammeln und nach Pass-Typ
         (Beauty / MaterialID / ObjectID / Depth) zuordnen.

    Vorteil gegenueber mtime-Window: Enscape schreibt Beauty oft Minuten
    vor / nach den Passes - das Prefix-Match ist deterministisch.

    Returns:
      render_set-Dict oder None wenn keine PNG/JPG im Ordner ODER
      juengste Datei aelter als `max_age_s`.
    """
    if not os.path.isdir(folder):
        return None
    candidates = []
    for entry in os.listdir(folder):
        p = os.path.join(folder, entry)
        if os.path.isfile(p) and entry.lower().endswith(IMAGE_EXTS):
            try:
                candidates.append((os.path.getmtime(p), p))
            except Exception:
                continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    newest_mtime, newest_path = candidates[0]
    if (time.time() - newest_mtime) > max_age_s:
        return None  # Kein frischer Render

    prefix, _, _ = _split_render_filename(os.path.basename(newest_path))
    return _build_render_set_for_prefix(folder, prefix)


def find_pass_siblings(picked_path):
    """Wenn der User manuell eine Datei picked (Beauty ODER Pass):
    Prefix ableiten und ALLE Files mit gleichem Prefix im selben Ordner
    einsammeln. Robust gegen "User pickt aus Versehen den MaterialID-
    Pass" - Beauty wird unter dem gemeinsamen Prefix gefunden.
    """
    if not picked_path or not os.path.isfile(picked_path):
        return None
    folder = os.path.dirname(picked_path)
    prefix, _, _ = _split_render_filename(os.path.basename(picked_path))
    return _build_render_set_for_prefix(folder, prefix)


# ----------------------------------------------------------------------
# Launcher-Bruecke
#
# Statt hier Chrome + Webapp-Health selbst zu machen, ruft der Button den
# gemeinsamen CPython-Launcher auf (tools/launcher/launch.py). Der
# startet die Webapp bei Bedarf selbst (kein manuelles uvicorn mehr) und
# oeffnet Chrome — EINE Quelle fuer den Start-Ablauf, gemeinsam mit der
# Desktop-Verknuepfung.

def find_repo_root_from_script():
    """Vom Skript-Pfad aufwaerts zur ersten pyproject.toml (= Repo-Root).
    Die Extension liegt in <repo>/tools/pyrevit_export/..., also findet der
    Walk-up das Repo, solange sie in-place registriert ist. Fallback:
    'repo_dir' aus config.json."""
    node = None
    try:
        node = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        node = None
    while node:
        if os.path.isfile(os.path.join(node, "pyproject.toml")):
            return node
        parent = os.path.dirname(node)
        if parent == node:
            break
        node = parent
    cfg = load_config()
    repo = cfg.get("repo_dir")
    if repo and os.path.isfile(os.path.join(repo, "pyproject.toml")):
        return repo
    return None


def open_via_launcher(page, snapshot=None, project_dir=None):
    """Startet den gemeinsamen Launcher: venv-pythonw launch.py --page ...
    Gibt (ok, error_message_or_None) zurueck."""
    repo = find_repo_root_from_script()
    if not repo:
        return False, (
            "Projekt-Root nicht gefunden (keine pyproject.toml oberhalb des "
            "Skripts, kein 'repo_dir' in config.json)."
        )
    pyw = os.path.join(repo, ".venv", "Scripts", "pythonw.exe")
    launcher = os.path.join(repo, "tools", "launcher", "launch.py")
    if not os.path.isfile(pyw):
        return False, "venv-Python nicht gefunden:\n  {}".format(pyw)
    if not os.path.isfile(launcher):
        return False, "Launcher nicht gefunden:\n  {}".format(launcher)
    args = [pyw, launcher, "--page", page]
    if snapshot:
        args += ["--snapshot", snapshot]
    if project_dir:
        args += ["--project-dir", project_dir]
    try:
        subprocess.Popen(args)
        return True, None
    except Exception as e:
        return False, "Launcher-Start fehlgeschlagen:\n  {}".format(e)


# ----------------------------------------------------------------------
# Helpers - Sub-Schritte

def write_legend_and_meta(view, view_name_raw, view_name, project_dir,
                          project_file, snapshot_dir):
    """Phase-1-Aequivalent: materials_legend.json + meta.json schreiben.
    Returns (materials_count, total_in_project, skipped_count)."""
    used_material_ids = get_used_material_ids_in_view(doc, view)
    collector = FilteredElementCollector(doc).OfClass(Material)
    total_materials_in_project = 0
    materials_list = []
    skipped = []
    for material in collector:
        total_materials_in_project += 1
        try:
            if material.Id.IntegerValue not in used_material_ids:
                continue
            materials_list.append(material_to_dict(material, doc))
        except Exception as exc:
            skipped.append((material.Name if material else "?", str(exc)))
    materials_list.sort(key=lambda m: m["name"] or "")

    revit_app = doc.Application
    timestamp = datetime.now().strftime(TS_FORMAT)

    legend = {
        "scene_id": view_name,
        "exported_at": timestamp,
        "revit_project_file": project_file,
        "revit_version": revit_app.VersionNumber,
        "revit_build": revit_app.VersionBuild,
        "pyrevit_engine": "IronPython2",
        "enscape_id_pass_anchor": "not_applicable_falsified_2026_05_18",
        "view_name_raw": view_name_raw,
        "filter": "elements_visible_in_active_view",
        "stats": {
            "total_materials_in_project": total_materials_in_project,
            "materials_used_in_view": len(materials_list),
            "skipped": len(skipped),
        },
        "materials_count": len(materials_list),
        "materials": materials_list,
    }
    write_json(os.path.join(snapshot_dir, "materials_legend.json"), legend)

    meta = {
        "scene_id": view_name,
        "exported_at": timestamp,
        "revit_project_file": project_file,
        "revit_version": revit_app.VersionNumber,
        "revit_build": revit_app.VersionBuild,
        "view_name_raw": view_name_raw,
        "project_info": get_project_info(doc),
        "site_location": get_site_location_info(doc),
        "view": get_view_info(view),
        "program_type": None,
        "furniture_state": None,
        "materials_count": len(materials_list),
        "materials": [m["name"] for m in materials_list],
    }
    write_json(os.path.join(snapshot_dir, "meta.json"), meta)

    return len(materials_list), total_materials_in_project, len(skipped)


def move_render_set(render_set, view_name, snapshot_dir):
    """Verschiebt Beauty + verfuegbare Passes ins snapshot_dir per
    shutil.move. Beauty-Move ist kritisch (raised); Pass-Moves sind
    best-effort (logged in failed-Liste).

    Returns (moved_list, failed_list).
    """
    moved = []
    failed = []
    for key in ["beauty", "material_id", "object_id", "depth"]:
        source = render_set.get(key)
        if not source:
            continue
        ext = os.path.splitext(source)[1].lower()
        target_name = "{}{}{}".format(view_name, PASS_TARGET_SUFFIX[key], ext)
        target_path = os.path.join(snapshot_dir, target_name)
        # Defensive: wenn der User die Datei aus dem Snapshot-Folder selbst
        # gepickt hat (typisches Re-Render-Szenario), waere Source==Target
        # und shutil.move wirft "are the same file". Skip dann — die Datei
        # ist ja schon am Ziel.
        try:
            if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(target_path)):
                moved.append((key, source, target_path))
                continue
        except Exception:
            pass
        # Falls Target bereits existiert (z.B. Wiederholungs-Klick fuer
        # gleiche View), erst loeschen - shutil.move ueberschreibt sonst
        # nicht zuverlaessig auf Windows.
        if os.path.isfile(target_path):
            try:
                os.remove(target_path)
            except Exception:
                pass
        try:
            shutil.move(source, target_path)
            moved.append((key, source, target_path))
        except Exception as e:
            if key == "beauty":
                raise
            failed.append((key, source, str(e)))
    return moved, failed


def render_set_summary(render_set):
    """Eine Zeile pro Datei fuer den Confirm-Dialog."""
    lines = []
    for key in ["beauty", "material_id", "object_id", "depth"]:
        path = render_set.get(key)
        label = PASS_TARGET_SUFFIX[key].lstrip("_")
        if path:
            lines.append("  {}: {}".format(label, os.path.basename(path)))
        else:
            lines.append("  {}: (kein Pass-File gefunden)".format(label))
    return "\n".join(lines)


def existing_snapshot_summary(snapshot_dir, view_name):
    """Beschreibt was sich aktuell im Snapshot-Folder befindet, damit der
    User vor dem Move weiss ob da unrelatierte Files liegen.

    Returns (summary_str, num_existing).
    """
    if not os.path.isdir(snapshot_dir):
        return "  (Snapshot-Folder existiert noch nicht, wird angelegt)", 0
    entries = [
        e for e in sorted(os.listdir(snapshot_dir))
        if os.path.isfile(os.path.join(snapshot_dir, e))
    ]
    if not entries:
        return "  (leer)", 0
    # Welche Files werden vom Run ueberschrieben/angefasst?
    managed_prefixes = [
        view_name + suffix for suffix in PASS_TARGET_SUFFIX.values()
    ]
    overwrites = []
    unrelated = []
    for entry in entries:
        stem = os.path.splitext(entry)[0]
        if entry in ("materials_legend.json", "meta.json"):
            overwrites.append(entry)
        elif any(stem == p for p in managed_prefixes):
            overwrites.append(entry)
        else:
            unrelated.append(entry)
    lines = []
    if overwrites:
        lines.append("  Wird ueberschrieben ({}):".format(len(overwrites)))
        for n in overwrites[:5]:
            lines.append("    - {}".format(n))
        if len(overwrites) > 5:
            lines.append("    ... +{} weitere".format(len(overwrites) - 5))
    if unrelated:
        lines.append("  Bleibt unangetastet ({}):".format(len(unrelated)))
        for n in unrelated[:5]:
            lines.append("    - {}".format(n))
        if len(unrelated) > 5:
            lines.append("    ... +{} weitere".format(len(unrelated) - 5))
    return "\n".join(lines), len(entries)


# ----------------------------------------------------------------------
# Snapshot-Identitaet: Projekt-Name + sequentielle View-Nummer

def derive_project_short_name(project_file):
    """Projekt-Kurzname aus dem Dateinamen der Revit-Datei.

    `Wohnanlage_Nord.rvt` → `Wohnanlage_Nord`. Bewusst unabhängig von
    Laufwerken oder Ablage-Konventionen eines bestimmten Büros.

    Returns None wenn kein Dateiname vorliegt — Caller faellt dann auf das
    View-Name-Naming zurueck.
    """
    if not project_file:
        return None
    stem = os.path.splitext(os.path.basename(project_file))[0]
    return sanitize_path_segment(stem) or None


def get_or_assign_view_number(revit_render_root, view_name_raw):
    """Liefert die View-Nummer fuer `view_name_raw` aus _view_index.json.

    Wenn die View dort schon eingetragen ist: Bestandsnummer zurueck.
    Sonst: naechste freie Nummer vergeben, Datei updaten.

    revit_render_root muss existieren (= os.makedirs vorher).
    """
    index_path = os.path.join(revit_render_root, VIEW_INDEX_FILENAME)
    index = {}
    if os.path.isfile(index_path):
        try:
            with codecs.open(index_path, "r", "utf-8") as f:
                index = json.load(f)
        except Exception:
            index = {}
    if view_name_raw in index:
        return int(index[view_name_raw]), index
    next_n = (max(index.values()) if index else 0) + 1
    index[view_name_raw] = next_n
    try:
        with codecs.open(index_path, "w", "utf-8") as f:
            f.write(json.dumps(index, indent=2, ensure_ascii=False))
    except Exception:
        pass
    return next_n, index


def compute_snapshot_id(project_dir, project_file, view_name_raw, view_name_sanitized):
    """Bestimmt den Snapshot-Folder-Name unter `<Projekt>/Revit Render/`.

    Schema (User-Wunsch 2026-06-19): `<projekt_short>_view<N>`, wobei N die
    laufende Nummer fuer diese View in diesem Projekt ist. Wenn der
    Projekt-Name nicht aus dem Pfad ableitbar ist: Fallback auf das alte
    View-Name-Naming (= view_name_sanitized).

    Returns (snapshot_id, project_short_or_None).
    """
    project_short = derive_project_short_name(project_file)
    if not project_short:
        return view_name_sanitized, None

    # Sicherstellen dass Revit Render-Root existiert, bevor wir das
    # Index-File anlegen wollen.
    revit_render_root = os.path.join(project_dir, REVIT_RENDER_FOLDER)
    if not os.path.isdir(revit_render_root):
        os.makedirs(revit_render_root)

    view_n, _ = get_or_assign_view_number(revit_render_root, view_name_raw)
    snapshot_id = "{}_view{}".format(project_short, view_n)
    return snapshot_id, project_short


def is_snapshot_complete(snapshot_dir, snapshot_id):
    """True wenn der Snapshot-Folder bereits einen kompletten Stand hat
    (meta.json + materials_legend.json + irgendein Beauty-File).

    Wird vom Idempotenz-Check in main() genutzt: bei `True` wird der
    Move-Workflow komplett geskipped, der User bekommt einen Dialog mit
    'Existierende View oeffnen' vs 'Neu rendern'.
    """
    if not os.path.isdir(snapshot_dir):
        return False
    if not os.path.isfile(os.path.join(snapshot_dir, "meta.json")):
        return False
    if not os.path.isfile(os.path.join(snapshot_dir, "materials_legend.json")):
        return False
    # Beauty kann unterschiedliche Extensions haben — Suffix-Match
    for fname in os.listdir(snapshot_dir):
        stem, ext = os.path.splitext(fname)
        if ext.lower() in IMAGE_EXTS and (
            stem.lower() == "beauty"
            or stem.endswith("_Beauty")
            or stem.endswith("-Beauty")
            or stem.lower().endswith("_beauty")
        ):
            return True
    return False


# ----------------------------------------------------------------------
# Webapp-Launch (geteilt zwischen Idempotenz-Pfad + Normal-Flow)

def launch_webapp_for_snapshot(snapshot_id, revit_render_root, view_name_raw,
                               project_file, snapshot_dir,
                               mats_count=None, total_count=None,
                               skipped_count=None, moved=None, failed=None):
    """Oeffnet die Webapp fuer diesen Snapshot ueber den gemeinsamen
    Launcher (tools/launcher/launch.py). Der Launcher startet die
    Webapp bei Bedarf selbst (kein manuelles uvicorn mehr) und oeffnet
    Chrome --app rechts. Danach folgt das Detail-Log.

    Bei `moved/failed/mats_count=None` (= Idempotenz-Pfad: kein Move
    stattgefunden) wird der Log-Output entsprechend reduziert.
    """
    ok, launch_err = open_via_launcher(
        "home", snapshot=snapshot_id, project_dir=revit_render_root,
    )
    if not ok:
        forms.alert(
            "Snapshot ist bereit:\n  {}\n\nAber der Launcher lief nicht:\n"
            "  {}\n\nDu kannst das Tool ueber die Desktop-Verknuepfung "
            "'Render Director' starten und dort den Snapshot '{}' oeffnen.".format(
                snapshot_dir, launch_err, snapshot_id,
            ),
            title="Render Director - Launcher-Fehler",
        )
        # Kein return: Log unten trotzdem schreiben, damit die Pfade sichtbar sind.

    # --- Detail-Log
    output.print_md("### Render Director - {} ({})".format(snapshot_id, view_name_raw))
    output.print_md("- **Projekt:** `{}`".format(project_file))
    output.print_md("- **Snapshot-Folder:** `{}`".format(snapshot_dir))
    if mats_count is not None:
        output.print_md(
            "- **JSONs:** {} Materialien (von {} im Projekt), "
            "{} skipped".format(mats_count, total_count, skipped_count)
        )
    if moved:
        output.print_md("- **Verschoben ({}):**".format(len(moved)))
        for key, source, target in moved:
            output.print_md(
                "  - `{}`: `{}` -> `{}`".format(
                    key, os.path.basename(source), os.path.basename(target)
                )
            )
    if failed:
        output.print_md("- **Pass-Moves fehlgeschlagen ({}):**".format(len(failed)))
        for key, source, err_msg in failed:
            output.print_md(
                "  - `{}` (`{}`): {}".format(key, os.path.basename(source), err_msg)
            )
    if moved is None and mats_count is None:
        output.print_md("- **Modus:** Existierende View geoeffnet (kein Move).")
    if ok:
        output.print_md(
            "- **Webapp:** via Launcher geoeffnet (Snapshot `{}`).".format(snapshot_id)
        )


# ----------------------------------------------------------------------
# Main

def main():
    # --- 0. Modus-Wahl: EIN Button, zwei Wege (User-Wunsch 2026-07-02).
    #   A = Enscape-Render / BIM (dieser Workflow unten).
    #   B = Ad-hoc ohne Revit -> nur Launcher auf /adhoc.
    mode = forms.alert(
        u"Wie möchtest du arbeiten?\n\n"
        "- Enscape-Render (BIM): mit dem gerenderten Beauty + den Revit-"
        "Daten der aktiven View arbeiten.\n"
        "- Ad-hoc (ohne Revit): direkt ein Bild hochladen und bearbeiten.",
        title="Render Director",
        options=[
            "Enscape-Render (BIM)",
            "Ad-hoc (Bild hochladen)",
            "Abbrechen",
        ],
    )
    if mode == "Ad-hoc (Bild hochladen)":
        ok, launch_err = open_via_launcher("adhoc")
        if not ok:
            forms.alert(
                "Ad-hoc-Modus konnte nicht gestartet werden:\n  {}\n\n"
                "Alternativ die Desktop-Verknuepfung 'Render Director' nutzen."
                .format(launch_err),
                title="Render Director - Ad-hoc",
            )
        return
    if mode != "Enscape-Render (BIM)":
        # Abbrechen, Fenster-X, Esc oder unerwarteter Rueckgabewert -> sicherer
        # Default = nichts tun. (Frueher exakter "Abbrechen"-Match; bei jedem
        # abweichenden Rueckgabewert fiel der Klick trotzdem in den BIM-Export.)
        return

    # --- 1. Revit-Kontext validieren (Modus A ab hier)
    project_path = doc.PathName
    if not project_path:
        forms.alert(
            "Das Revit-Projekt wurde noch nie gespeichert.\n\n"
            "Bitte speichere das Projekt zuerst - der Export braucht "
            "einen Projekt-Ordner.",
            title="Render Director",
            exitscript=True,
        )

    project_dir = os.path.dirname(project_path)
    project_file = os.path.basename(project_path)

    view = doc.ActiveView
    view_name_raw = view.Name if view else "no_active_view"
    view_name_sanitized = sanitize_path_segment(view_name_raw)

    # Snapshot-Identitaet (Folder-Name): Projekt-Name aus der Revit-Datei +
    # sequentielle View-Nummer (z.B. 'Wohnanlage_Nord_view1'). Ohne
    # Dateinamen: Fallback auf den sanitisierten View-Namen.
    snapshot_id, project_short = compute_snapshot_id(
        project_dir, project_file, view_name_raw, view_name_sanitized,
    )
    snapshot_dir = os.path.join(project_dir, REVIT_RENDER_FOLDER, snapshot_id)
    revit_render_root = os.path.join(project_dir, REVIT_RENDER_FOLDER)

    # --- 1b. Idempotenz-Check: wenn die Snapshot-Daten schon komplett sind
    # (zweiter Klick auf dieselbe View, ohne dass sich der Enscape-Render
    # geaendert hat), Move-Workflow ueberspringen und direkt zur Webapp.
    if is_snapshot_complete(snapshot_dir, snapshot_id):
        choice = forms.alert(
            u"Snapshot für '{view}' existiert bereits:\n  {dir}\n\n"
            u"Was möchtest du tun?".format(
                view=view_name_raw, dir=snapshot_dir,
            ),
            title="Render Director - View bereits exportiert",
            options=[
                "Existierende View oeffnen",
                "Neu rendern (Enscape-Datei waehlen + ueberschreiben)",
                "Abbrechen",
            ],
        )
        if choice == "Existierende View oeffnen":
            launch_webapp_for_snapshot(
                snapshot_id, revit_render_root, view_name_raw,
                project_file, snapshot_dir,
                mats_count=None, total_count=None, skipped_count=None,
                moved=None, failed=None,
            )
            return
        if choice != "Neu rendern (Enscape-Datei waehlen + ueberschreiben)":
            # Abbrechen / Fenster-X / Esc -> nichts tun.
            return
        # "Neu rendern" -> Fall-through in den normalen Workflow

    # Alias: ab hier ist `view_name` = der Snapshot-Folder-Name. Wird in
    # write_legend_and_meta / move_render_set / existing_snapshot_summary
    # als Filename-Prefix + scene_id genutzt.
    view_name = snapshot_id

    # --- 2. Enscape-Output-Verzeichnis (cached oder frisch abgefragt)
    cfg = load_config()
    enscape_dir = get_enscape_output_dir(cfg)
    if not enscape_dir:
        forms.alert(
            "Kein Enscape-Verzeichnis ausgewaehlt - Abbruch.",
            title="Render Director",
            exitscript=True,
        )

    # --- 3. Render-Set finden (Auto, mit File-Picker-Fallback)
    render_set = find_render_set(enscape_dir)
    if render_set is None or not render_set.get("beauty"):
        # Kein Render automatisch gefunden -> File-Picker
        forms.alert(
            "Im konfigurierten Enscape-Ordner konnte kein Render "
            "automatisch gefunden werden:\n  {}\n\n"
            "Bitte waehle die Render-Datei (Beauty oder ein Pass-"
            "File reicht) manuell aus.".format(enscape_dir),
            title="Render Director - Kein Render automatisch gefunden",
        )
        picked = forms.pick_file(
            files_filter="Bild-Dateien (*.png;*.jpg;*.jpeg)|*.png;*.jpg;*.jpeg|"
                         "Alle Dateien (*.*)|*.*",
            init_dir=enscape_dir,
            title="Enscape-Render auswaehlen",
        )
        if not picked:
            return
        render_set = find_pass_siblings(picked)
        if render_set is None or not render_set.get("beauty"):
            forms.alert(
                "Datei nicht lesbar oder keine Beauty erkannt.\n\n"
                "Abbruch.",
                title="Render Director",
                exitscript=True,
            )
        remember_picked_folder(cfg, picked)

    # --- 4. Confirm-Dialog (User kann nochmal andere Datei waehlen)
    while True:
        age_s = (time.time() - render_set["_newest_mtime"]
                 if render_set.get("_newest_mtime") else 0)
        existing_summary, num_existing = existing_snapshot_summary(
            snapshot_dir, view_name,
        )
        beauty_path = render_set.get("beauty")
        if not beauty_path:
            forms.alert(
                "Im aktuellen Render-Set wurde keine Beauty-Datei "
                "gefunden (nur Pass-Files). Bitte andere Datei waehlen.",
                title="Render Director",
            )
        confirm_msg = (
            "Speichern als View '{view}'\n"
            "Projekt: {project}\n"
            "Ziel-Ordner: {snapshot}\n\n"
            "Render-Prefix: {prefix}\n"
            "Render-Quelle: {beauty_path}\n"
            "  (juengste Datei vor {age})\n\n"
            "Render-Set:\n{set_summary}\n\n"
            "Snapshot-Folder Status:\n{existing}\n\n"
            "Aktion: Beauty + Pass-Files werden vom Source-Ordner "
            "ins Ziel verschoben (Move, nicht Copy)."
        ).format(
            view=view_name_raw,
            project=project_file,
            snapshot=snapshot_dir,
            prefix=render_set.get("_prefix", "(unbekannt)"),
            beauty_path=beauty_path or "(noch keine Beauty)",
            age=format_age(age_s),
            set_summary=render_set_summary(render_set),
            existing=existing_summary,
        )
        choice = forms.alert(
            confirm_msg,
            title="Render Director - Bestaetigung",
            options=["Weiter", "Andere Render-Datei waehlen", "Abbrechen"],
        )
        if choice == "Weiter":
            break
        if choice != "Andere Render-Datei waehlen":
            # Abbrechen / Fenster-X / Esc -> nichts tun.
            return
        # "Andere Render-Datei waehlen"
        picked = forms.pick_file(
            files_filter="Bild-Dateien (*.png;*.jpg;*.jpeg)|*.png;*.jpg;*.jpeg|"
                         "Alle Dateien (*.*)|*.*",
            init_dir=enscape_dir,
            title="Enscape-Render auswaehlen",
        )
        if not picked:
            continue  # Picker-Cancel -> zurueck zum Confirm
        new_set = find_pass_siblings(picked)
        if new_set is None or not new_set.get("beauty"):
            forms.alert(
                "Datei nicht lesbar - bitte andere auswaehlen.",
                title="Render Director",
            )
            continue
        render_set = new_set
        remember_picked_folder(cfg, picked)

    # --- 5. Snapshot-Folder anlegen, JSONs schreiben
    if not os.path.exists(snapshot_dir):
        os.makedirs(snapshot_dir)

    try:
        mats_count, total_count, skipped_count = write_legend_and_meta(
            view, view_name_raw, view_name, project_dir, project_file,
            snapshot_dir,
        )
    except Exception as e:
        forms.alert(
            "Fehler beim Schreiben der JSONs:\n  {}\n\n"
            "Render-Files wurden NICHT verschoben.".format(e),
            title="Render Director",
            exitscript=True,
        )

    # --- 6. Render-Set verschieben
    try:
        moved, failed = move_render_set(render_set, view_name, snapshot_dir)
    except Exception as e:
        forms.alert(
            "Fehler beim Verschieben der Beauty:\n  {}\n\n"
            "Snapshot-Folder enthaelt zwar die JSONs, aber kein Bild. "
            "Beauty liegt ggf. noch im Enscape-Ordner.".format(e),
            title="Render Director",
            exitscript=True,
        )

    # --- 7. Webapp-Check + Chrome-Launch + Detail-Log
    launch_webapp_for_snapshot(
        snapshot_id, revit_render_root, view_name_raw,
        project_file, snapshot_dir,
        mats_count=mats_count, total_count=total_count, skipped_count=skipped_count,
        moved=moved, failed=failed,
    )


if __name__ == "__main__":
    main()
