"""Smoke-Tests der FastAPI-Webapp (Rendering der Seiten, keine Provider-Calls)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Ad-hoc-Seite legt ihren Zielordner an — nicht im Home-Verzeichnis.
    monkeypatch.setenv("RENDER_ADHOC_ROOT", str(tmp_path / "adhoc"))
    return TestClient(app)


def test_health(client):
    body = client.get("/health").json()
    assert body["ok"] is True


@pytest.mark.parametrize("path", ["/", "/adhoc", "/cost"])
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "Render Director" in response.text


def test_mock_badge_only_in_mock_mode(client, mock_providers):
    assert ">Mock</span>" in client.get("/").text


def test_no_mock_badge_by_default(client):
    assert ">Mock</span>" not in client.get("/").text
