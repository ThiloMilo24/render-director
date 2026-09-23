"""Tier-0-Referenzbibliothek: Dateinamen-Parser und Auswahl."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from render_director.rag.library import (
    build_index,
    find_by_project,
    find_similar,
    parse_filename,
    select_reference_images,
    snapshot_material_keywords,
    snapshot_time,
)

from conftest import DEMO_LIBRARY


@pytest.mark.parametrize("stem, material, time, angle, distance", [
    ("projecta_005_hotel_woodglass_day_corner_close", {"wood", "glass"}, "day", "corner", "close"),
    # optionales Regions-Token vor dem Gebäudetyp, Split-Präfix, Tippfehler-Punkt
    ("test_projectb_001_xy_residential_plastermetal_dusk_other_walkway._far",
     {"plaster", "metal"}, "dusk", "other_walkway", "far"),
    # unbekannter Gebäudetyp → positionaler Fallback
    ("projectc_002_barn_stone_overcast_front_medium", {"stone"}, "overcast", "front", "medium"),
])
def test_parse_filename(stem, material, time, angle, distance):
    parsed = parse_filename(stem)
    assert parsed["material_tokens"] == frozenset(material)
    assert parsed["time"] == time
    assert parsed["angle"] == angle
    assert parsed["distance"] == distance


def test_german_bim_text_maps_to_library_vocabulary():
    materials = {"Fassade": "Lärchenholz-Schalung", "Sockel": "Sichtbeton"}
    assert snapshot_material_keywords(materials) == frozenset({"wood", "concrete"})
    assert snapshot_time("später Nachmittag") == "day"
    assert snapshot_time("Dämmerung") == "dusk"
    assert snapshot_time("unbekannt") is None


def test_missing_library_is_not_an_error(tmp_path: Path):
    assert build_index(tmp_path / "fehlt", use_cache=False) == []
    assert select_reference_images(root=tmp_path / "fehlt", project_query="x") == []


def test_demo_library_index():
    index = build_index(DEMO_LIBRARY, use_cache=False)
    assert len(index) == 4
    assert {img.project_folder for img in index} == {"Demo Lodge", "Stone House"}
    assert all(img.caption for img in index)


def test_find_similar_prefers_material_and_project_diversity():
    index = build_index(DEMO_LIBRARY, use_cache=False)
    picked = find_similar(index, material_keywords=frozenset({"wood", "stone"}), time="day", k=2)
    assert len(picked) == 2
    assert len({img.project for img in picked}) == 2
    assert all(img.time == "day" for img in picked)


def test_find_similar_requires_material_overlap():
    index = build_index(DEMO_LIBRARY, use_cache=False)
    assert find_similar(index, material_keywords=frozenset({"concrete"}), time="day", k=2) == []


def test_find_by_project_matches_code_name_and_folder():
    index = build_index(DEMO_LIBRARY, use_cache=False)
    assert {img.project for img in find_by_project(index, "demolodge", k=2)} == {"demolodge"}
    assert {img.project_folder for img in find_by_project(index, "stone", k=2)} == {"Stone House"}
    assert find_by_project(index, "xy", k=2) == []  # zu kurz für einen sinnvollen Match


def test_select_prefers_project_then_falls_back_to_materials(tmp_path: Path):
    by_project = select_reference_images(root=DEMO_LIBRARY, k=2, project_query="Stone House",
                                         materials={"Fassade": "Holz"}, time_of_day="Vormittag")
    assert {img.project for img in by_project} == {"stonehouse"}

    fallback = select_reference_images(root=DEMO_LIBRARY, k=1, project_query="Unbekannt",
                                       materials={"Fassade": "Holz, Glas"}, time_of_day="Dämmerung")
    assert [img.path.name for img in fallback] == ["demolodge_002_residential_woodglass_dusk_corner_far.jpg"]


def test_build_index_reads_nested_folders(tmp_path: Path):
    folder = tmp_path / "Project Z"
    folder.mkdir()
    Image.new("RGB", (8, 8)).save(folder / "projectz_001_office_metalglass_night_front_far.jpg")
    index = build_index(tmp_path, use_cache=False)
    assert index[0].project_folder == "Project Z"
    assert index[0].time == "night"


def test_short_needle_only_matches_word_start():
    # "alu" steckt in "Schalung" — darf nicht als Metall zählen
    assert snapshot_material_keywords({"Fassade": "Holzschalung"}) == frozenset({"wood"})
    assert snapshot_material_keywords({"Fenster": "Holz-Alu"}) == frozenset({"wood", "metal"})
