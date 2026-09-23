"""Gemeinsame Fixtures.

Tests laufen ohne `.env` und ohne Netzwerk: `_ensure_dotenv` wird als schon
geladen markiert (sonst würde eine lokale `.env` mit `override=True` die
Test-Umgebung überschreiben), und Provider-Calls gehen an die Mock-Provider.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from render_director import clients
from render_director.paths import PROJECT_ROOT

EXAMPLES = PROJECT_ROOT / "examples"
DEMO_EXTERIOR = EXAMPLES / "scenes" / "demo_exterior"
DEMO_INTERIOR = EXAMPLES / "scenes" / "demo_interior"
DEMO_LIBRARY = EXAMPLES / "reference_library"


def _clear_client_caches() -> None:
    clients.get_anthropic_client.cache_clear()
    clients.get_google_client.cache_clear()
    clients.get_openai_client.cache_clear()


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch):
    """Keine `.env`, keine echten Keys, keine Client-Caches zwischen Tests."""
    monkeypatch.setattr(clients, "_DOTENV_LOADED", True)
    for var in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
                "RENDER_MOCK_PROVIDERS", "RENDER_DEFAULT_REGION", "RENDER_HTTP_CONTACT",
                "RENDER_GENERATIONS_ROOT"):
        monkeypatch.delenv(var, raising=False)
    _clear_client_caches()
    yield
    _clear_client_caches()


@pytest.fixture
def mock_providers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RENDER_MOCK_PROVIDERS", "1")
    _clear_client_caches()


@pytest.fixture
def generations_root(tmp_path: Path) -> Path:
    root = tmp_path / "generations"
    root.mkdir()
    return root
