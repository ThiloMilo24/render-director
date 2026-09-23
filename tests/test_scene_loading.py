"""Scene-Bundle-Loader: Pass-Erkennung und pyRevit-Meta-Adapter."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from render_director.utils import _adapt_pyrevit_meta, _find_pass, load_scene_bundle

from conftest import DEMO_EXTERIOR, DEMO_INTERIOR


def _minimal_meta(**overrides) -> dict:
    meta = {
        "project_name": "Test",
        "project_type": "Wohnbau",
        "location": "Testort",
        "facade_orientation": "Süd",
        "camera_focal_length_mm": 24.0,
        "camera_height_m": 1.6,
        "time_of_day": "Vormittag",
    }
    meta.update(overrides)
    return meta


def test_demo_exterior_bundle():
    bundle = load_scene_bundle(DEMO_EXTERIOR)
    assert bundle.meta.view_type == "exterior"
    assert bundle.depth is not None and bundle.material_id is not None
    assert bundle.object_id is None
    assert bundle.beauty.size == (1280, 720)
    assert bundle.meta.site_location.latitude_deg == pytest.approx(47.2692)


def test_demo_interior_bundle():
    bundle = load_scene_bundle(DEMO_INTERIOR)
    assert bundle.meta.view_type == "interior"
    assert bundle.meta.program_type == "wohnraum"
    assert bundle.meta.furniture_state == "placeholder"


@pytest.mark.parametrize("name", ["View_MaterialID.png", "View-material-id.jpg", "material_id.png"])
def test_pass_suffix_is_tolerant(tmp_path: Path, name: str):
    Image.new("RGB", (4, 4)).save(tmp_path / name)
    assert _find_pass(tmp_path, "material_id").name == name


def test_exact_name_wins_over_prefixed(tmp_path: Path):
    Image.new("RGB", (4, 4)).save(tmp_path / "beauty.jpg")
    Image.new("RGB", (4, 4)).save(tmp_path / "View_Beauty.jpg")
    assert _find_pass(tmp_path, "beauty").name == "beauty.jpg"


def test_ambiguous_passes_fail_loudly(tmp_path: Path):
    Image.new("RGB", (4, 4)).save(tmp_path / "ViewA_Beauty.png")
    Image.new("RGB", (4, 4)).save(tmp_path / "ViewB_Beauty.png")
    with pytest.raises(RuntimeError, match="Mehrdeutig"):
        _find_pass(tmp_path, "beauty")


def test_missing_meta_json(tmp_path: Path):
    Image.new("RGB", (4, 4)).save(tmp_path / "beauty.png")
    with pytest.raises(FileNotFoundError, match="meta.json"):
        load_scene_bundle(tmp_path)


def test_manual_meta_passes_through_unchanged():
    meta = _minimal_meta()
    assert _adapt_pyrevit_meta(meta) is meta


def test_pyrevit_meta_is_adapted(tmp_path: Path):
    raw = {
        "scene_id": "Wohnanlage_view1",
        "view_name_raw": "3D Innen Lobby",
        "project_info": {"Name": "Wohnanlage", "Address": "Musterstraße 1"},
        "view": {"view_name": "3D Innen Lobby", "camera_height_m": 1.5},
        "site_location": {"latitude_deg": 47.0, "longitude_deg": 11.0},
        "materials": ["Putz weiß", "Eiche geölt"],
    }
    Image.new("RGB", (4, 4)).save(tmp_path / "Wohnanlage_view1_Beauty.png")
    (tmp_path / "meta.json").write_text(json.dumps(raw), encoding="utf-8")

    meta = load_scene_bundle(tmp_path).meta
    assert meta.project_name == "Wohnanlage"
    assert meta.view_type == "interior"  # Trigger "innen"/"lobby" im View-Namen
    assert meta.materials == {"Material_01": "Putz weiß", "Material_02": "Eiche geölt"}
    assert meta.camera_height_m == 1.5
    assert meta.site_location.longitude_deg == 11.0
