"""Prompt-Laden und User-Text-Templates (ohne API-Calls)."""
from __future__ import annotations

import pytest

from render_director.prompts import (
    DEFAULT_REGION_PLACEHOLDER,
    build_director_user_text,
    build_iteration_user_text,
    build_validator_user_text,
    load_advisor_system,
    load_director_adhoc_system,
    load_director_system,
    load_validator_adhoc_system,
    load_validator_system,
)
from render_director.utils import load_scene_bundle

from conftest import DEMO_EXTERIOR, DEMO_INTERIOR


ALL_LOADERS = [
    lambda: load_director_system("exterior"),
    lambda: load_director_system("interior"),
    lambda: load_validator_system("exterior"),
    lambda: load_validator_system("interior"),
    load_director_adhoc_system,
    load_validator_adhoc_system,
    load_advisor_system,
]


@pytest.mark.parametrize("load", ALL_LOADERS)
def test_no_unresolved_placeholders(load):
    assert DEFAULT_REGION_PLACEHOLDER not in load()


def test_default_region_is_configurable(monkeypatch):
    assert "DER REGIONALE DEFAULT-KONTEXT IST: Alpenraum." in load_director_system("exterior")
    monkeypatch.setenv("RENDER_DEFAULT_REGION", "Mittelmeerküste")
    assert "DER REGIONALE DEFAULT-KONTEXT IST: Mittelmeerküste." in load_director_system("exterior")
    assert "Default-Annahme: Mittelmeerküste" in load_director_adhoc_system()


def test_view_type_selects_prompt_rail():
    assert "Interior-Schiene" in load_director_system("interior")
    assert "Interior-Schiene" not in load_director_system("exterior")


def test_director_text_has_geo_block_for_exterior_with_coordinates():
    bundle = load_scene_bundle(DEMO_EXTERIOR)
    text = build_director_user_text(bundle, "A", "Warmer Vormittag")
    assert "GEO-KONTEXT" in text
    assert "47.2692° N" in text
    assert "MODUS: A (Kundenpräsentation)" in text
    # site_location steht im GEO-Block, nicht zusätzlich im JSON-Dump
    assert '"site_location"' not in text


def test_director_text_mode_d_unlocks_geometry():
    bundle = load_scene_bundle(DEMO_EXTERIOR)
    assert "GEOMETRY EXPLORATION" in build_director_user_text(bundle, "D", "Varianten")
    assert "GEOMETRY EXPLORATION" not in build_director_user_text(bundle, "A", "Varianten")


def test_interior_has_no_geo_block():
    bundle = load_scene_bundle(DEMO_INTERIOR)
    assert "GEO-KONTEXT" not in build_director_user_text(bundle, "A", "Abendstimmung")


def test_validator_text_numbers_attachments_dynamically():
    bundle = load_scene_bundle(DEMO_EXTERIOR)
    text = build_validator_user_text(bundle, "A", "Wunsch", "PROMPT")
    assert "Bild 1 = Original Enscape Beauty Render" in text
    assert "Bild 2 = Depth Pass" in text
    assert "Bild 3 = Material-ID-Pass" in text
    assert "Bild 4 (letztes) = Generierter" in text


def test_italian_directive_keeps_machine_parsed_parts():
    bundle = load_scene_bundle(DEMO_EXTERIOR)
    text = build_validator_user_text(bundle, "A", "Wunsch", "PROMPT", language="it")
    assert text.startswith("LINGUA:")
    assert "chiavi in tedesco" in text


def test_iteration_text_includes_feedback_and_previous_prompt():
    bundle = load_scene_bundle(DEMO_EXTERIOR)
    text = build_iteration_user_text(bundle, "A", "Wunsch", "OLD PROMPT", "REPORT", user_feedback="wärmer")
    assert "OLD PROMPT" in text and "REPORT" in text
    assert "ZUSÄTZLICHES NUTZER-FEEDBACK" in text
    assert "Bild 2 = V1-Render" in text
