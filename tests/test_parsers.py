"""Parser und Score-Aggregation: die Stellen, an denen LLM-Text zu Daten wird."""
from __future__ import annotations

import json

import pytest

from render_director.pipeline import extract_final_prompt, looks_truncated
from render_director.utils import (
    VALIDATOR_SCORE_SCHEMA,
    VALIDATOR_SCORE_SCHEMA_INTERIOR,
    aggregate_validator_scores,
    flatten_validator_scores,
    mean_validator_score,
    parse_advisor_recommendation,
    parse_forward_attachments,
    parse_validator_scores_json,
)


def test_extract_final_prompt_takes_first_code_block():
    reply = "Vorspann.\n\n```\nPROMPT ONE\n```\n\nVariante:\n```text\nPROMPT TWO\n```"
    assert extract_final_prompt(reply) == "PROMPT ONE"


def test_extract_final_prompt_none_for_follow_up_question():
    assert extract_final_prompt("Was ist die Big Idea des Entwurfs?") is None


@pytest.mark.parametrize("reply, truncated", [
    ("```\nfertig\n```", False),
    ("Intro\n```\nabgeschnitten mitten im", True),
    ("keine Code-Blöcke", False),
])
def test_looks_truncated(reply, truncated):
    assert looks_truncated(reply) is truncated


@pytest.mark.parametrize("reply, expected", [
    ("...\nFORWARD_ATTACHMENTS: [1, 3]\n", [1, 3]),
    ("FORWARD_ATTACHMENTS: []", []),
    ("kein Block", []),
    ("FORWARD_ATTACHMENTS: [eins]", []),
])
def test_parse_forward_attachments(reply, expected):
    assert parse_forward_attachments(reply) == expected


def test_parse_advisor_recommendation_full_block():
    reply = "ACTION: Refine\nGRUND: Nur Details schief.\nKONFIDENZ: HIGH\n"
    assert parse_advisor_recommendation(reply) == ("refine", "Nur Details schief.", "high")


def test_parse_advisor_recommendation_defaults_confidence_to_medium():
    assert parse_advisor_recommendation("ACTION: stop\nGRUND: gut genug") == ("stop", "gut genug", "medium")


def test_parse_advisor_recommendation_rejects_unknown_action():
    assert parse_advisor_recommendation("ACTION: maybe\nGRUND: ?") is None


def test_parse_validator_scores_uses_last_json_block():
    first = {"geometrie": {"massing": 1}}
    last = {"geometrie": {"massing": 5}}
    reply = f"```json\n{json.dumps(first)}\n```\ntext\n```json\n{json.dumps(last)}\n```"
    assert parse_validator_scores_json(reply) == last


def test_parse_validator_scores_malformed_json_returns_none():
    assert parse_validator_scores_json("```json\n{ nope }\n```") is None


def test_mean_ignores_null_and_supports_sections():
    scores = {
        "geometrie": {"massing": 5, "proportionen": 3, "dachform": None},
        "material": {"fassade": 2},
    }
    assert mean_validator_score(scores) == pytest.approx(10 / 3)
    assert mean_validator_score(scores, section="geometrie") == pytest.approx(4.0)
    assert mean_validator_score({"material": {"fassade": None}}) is None


def test_flatten_fills_schema_and_keeps_drift_keys():
    flat = flatten_validator_scores({"geometrie": {"massing": 4, "erfunden": 2}})
    assert flat["geometrie.massing"] == 4
    assert flat["geometrie.erfunden"] == 2
    assert flat["material.fassade"] is None
    expected = sum(len(v) for v in VALIDATOR_SCORE_SCHEMA.values()) + 1
    assert len(flat) == expected


def test_flatten_interior_schema():
    flat = flatten_validator_scores({}, schema=VALIDATOR_SCORE_SCHEMA_INTERIOR)
    assert "material.bodenbelag_innen" in flat
    assert "material.fassade" not in flat


def test_aggregate_over_samples():
    samples = [
        {"geometrie": {"dachform": 2}},
        {"geometrie": {"dachform": 4}},
        {"geometrie": {"dachform": None}},
    ]
    agg = aggregate_validator_scores(samples)
    assert agg["geometrie.dachform"] == {"mean": 3.0, "min": 2, "max": 4, "n_valid": 2, "n_null": 1}
