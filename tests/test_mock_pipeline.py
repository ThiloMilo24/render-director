"""End-to-End mit Mock-Providern: Director → Generator → Validator → Advisor.

Prüft den kompletten Datenfluss inkl. Run-Artefakten, Score-Parsing,
Usage-Erfassung, Iterationen und Inpaint — ohne Netzwerk und ohne Keys.
"""
from __future__ import annotations

import json

import pytest
from PIL import Image, ImageDraw

from render_director.clients import get_anthropic_client, use_mock_providers
from render_director.mock_providers import MockAnthropicClient
from render_director.pipeline import (
    finalize_iteration,
    run_iteration,
    run_iteration_adhoc,
)

from conftest import DEMO_EXTERIOR, DEMO_INTERIOR, DEMO_LIBRARY

# Mock-Validator Exterior: 15 Sub-Dims mit 4, fassade 3, massing 5,
# weisse_placeholder_ersetzt 5 → Mean 61/15 (< 4.3 → Advisor empfiehlt refine).
MOCK_EXTERIOR_MEAN = 61 / 15


def test_mock_flag_switches_clients(mock_providers):
    assert use_mock_providers()
    assert isinstance(get_anthropic_client(), MockAnthropicClient)


def test_mock_flag_off_by_default():
    assert not use_mock_providers()


def test_exterior_iteration_writes_all_artifacts(mock_providers, generations_root):
    result = run_iteration(
        DEMO_EXTERIOR, "Warmer Vormittag, zwei Personen am Eingang",
        mode="A", generations_root=generations_root,
    )
    for name in ("result.png", "final_prompt.txt", "director_reply.md",
                 "validator_reply.md", "advisor_reply.md", "inputs.json"):
        assert (result.run_dir / name).is_file(), name

    assert "[GEOMETRY LOCK — HARD CONSTRAINT]" in result.final_prompt
    assert result.final_score == pytest.approx(MOCK_EXTERIOR_MEAN)
    assert set(result.section_means) == {"geometrie", "material", "anforderung"}
    assert result.advisor_action == "refine"
    assert result.run_dir.name.endswith("_NB1")

    inputs = json.loads((result.run_dir / "inputs.json").read_text(encoding="utf-8"))
    roles = [r["role"] for r in inputs["usage"]["records"]]
    assert roles == ["director", "generator", "validator", "advisor"]
    assert inputs["usage"]["estimated_cost_usd"] > 0

    with Image.open(result.generated_image_path) as img:
        assert img.size == (1280, 720)


def test_interior_uses_interior_schema(mock_providers, generations_root):
    result = run_iteration(DEMO_INTERIOR, "Abendstimmung", generations_root=generations_root)
    assert "bodenbelag_innen" in result.validator_reply
    assert set(result.section_means) == {"geometrie", "material", "anforderung"}


def test_refine_iteration_builds_on_previous_run(mock_providers, generations_root):
    first = run_iteration(DEMO_EXTERIOR, "Vormittag", generations_root=generations_root)
    second = run_iteration(
        DEMO_EXTERIOR, "Vormittag",
        iteration_type="refine",
        parent_run_id=first.run_id,
        previous_image_path=first.generated_image_path,
        previous_final_prompt=first.final_prompt,
        previous_validator_reply=first.validator_reply,
        user_feedback="Fassade stärker strukturiert",
        run_index=2,
        generations_root=generations_root,
    )
    assert "[ITERATION FOCUS]" in second.final_prompt
    inputs = json.loads((second.run_dir / "inputs.json").read_text(encoding="utf-8"))
    assert inputs["iteration"] == {"type": "refine", "parent_run_id": first.run_id, "reason": None}
    assert inputs["user_feedback"] == "Fassade stärker strukturiert"


def test_deferred_validation_then_finalize(mock_providers, generations_root):
    result = run_iteration(DEMO_EXTERIOR, "Vormittag", generations_root=generations_root,
                           defer_validation=True)
    assert result.validator_reply is None
    assert not (result.run_dir / "validator_reply.md").exists()

    outcome = finalize_iteration(result.run_dir)
    assert outcome.final_score == pytest.approx(MOCK_EXTERIOR_MEAN)
    inputs = json.loads((result.run_dir / "inputs.json").read_text(encoding="utf-8"))
    assert [r["role"] for r in inputs["usage"]["records"]] == ["director", "generator", "validator", "advisor"]


def test_reference_library_augments_generator_input(mock_providers, generations_root):
    result = run_iteration(
        DEMO_EXTERIOR, "Vormittag", generations_root=generations_root,
        use_reference_library=True, reference_library_root=DEMO_LIBRARY,
    )
    # Projektname passt zu keinem Ordner → Material-Match (Holz, Beton, Zink,
    # Naturstein) trifft beide Demo-Projekte, Diversität nimmt je eines
    refs = result.reference_library_refs
    assert len(refs) == 2
    assert {name.split("_")[0] for name in refs} == {"demolodge", "stonehouse"}
    inputs = json.loads((result.run_dir / "inputs.json").read_text(encoding="utf-8"))
    assert inputs["reference_library_refs"] == refs


def test_adhoc_gpt_backend_and_inpaint(mock_providers, tmp_path):
    source = Image.open(DEMO_EXTERIOR / "DemoHouse_South_Beauty.png").convert("RGB")
    first = run_iteration_adhoc(
        source, "Fotorealistisch, goldene Stunde",
        generator_model="gpt-image-1", generations_root=tmp_path,
    )
    assert first.run_dir.name.endswith("_GPT")
    assert first.advisor_action in {"refine", "stop", "regenerate"}

    previous = Image.open(first.generated_image_path).convert("RGB")
    mask = Image.new("RGBA", previous.size, (0, 0, 0, 255))
    ImageDraw.Draw(mask).rectangle([0, 600, 400, 720], fill=(0, 0, 0, 0))
    inpaint = run_iteration_adhoc(
        source, "Fotorealistisch, goldene Stunde",
        iteration_type="inpaint", parent_run_id=first.run_id,
        previous_image=previous, previous_final_prompt=first.final_prompt,
        user_feedback="Wiese statt Kante", mask=mask,
        generator_model="gpt-image-1", session_dir=first.run_dir.parent,
    )
    assert inpaint.final_prompt == "Wiese statt Kante"
    assert (inpaint.run_dir / "mask.png").is_file()

    after = Image.open(inpaint.generated_image_path).convert("RGB")
    # Opake Masken-Bereiche bleiben pixelgleich, transparente werden neu gemalt
    assert after.getpixel((900, 100)) == previous.getpixel((900, 100))
    assert after.getpixel((100, 650)) != previous.getpixel((100, 650))


def test_inpaint_requires_gpt_backend(mock_providers, tmp_path):
    img = Image.new("RGB", (64, 64))
    with pytest.raises(ValueError, match="gpt-image"):
        run_iteration_adhoc(
            img, "x", iteration_type="inpaint", parent_run_id="run",
            previous_image=img, previous_final_prompt="p", mask=Image.new("RGBA", (64, 64)),
            generations_root=tmp_path,
        )


def test_cli_generations_root_precedence(monkeypatch, tmp_path):
    """CLI-Ziel: Argument vor Env vor Default (None = data/generations)."""
    from render_director.pipeline import _cli_generations_root

    assert _cli_generations_root(None) is None
    monkeypatch.setenv("RENDER_GENERATIONS_ROOT", str(tmp_path))
    assert _cli_generations_root(None) == tmp_path
    assert _cli_generations_root(str(tmp_path / "explizit")) == tmp_path / "explizit"
