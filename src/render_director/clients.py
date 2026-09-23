"""SDK-Clients für Anthropic + Google-GenAI + OpenAI.

Lazy + cached: erstes `get_anthropic_client()`/`get_google_client()`/
`get_openai_client()` lädt `.env` und instanziiert den Client; weitere
Aufrufe geben denselben Client zurück. Mit `RENDER_MOCK_PROVIDERS=1`
kommen stattdessen die Attrappen aus `mock_providers` (Demo und Tests
ohne API-Keys). So bezahlt ein bloßes
`import render_director` keinen SDK-Boot — Code, der nur Helper aus
`utils` nutzt (z.B. Tests), braucht die SDKs nicht.
"""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

from render_director.paths import PROJECT_ROOT

_DOTENV_LOADED = False


def _ensure_dotenv() -> None:
    global _DOTENV_LOADED
    if not _DOTENV_LOADED:
        # override=True, weil ANTHROPIC_API_KEY/GOOGLE_API_KEY auf manchen
        # Windows-Systemen als leerer String systemweit registriert sind
        # und sonst die .env-Werte nicht durchkommen.
        load_dotenv(PROJECT_ROOT / ".env", override=True)
        _DOTENV_LOADED = True


def use_mock_providers() -> bool:
    """True, wenn `RENDER_MOCK_PROVIDERS` aktiv ist (Demo/Tests ohne Keys)."""
    _ensure_dotenv()
    flag = os.environ.get("RENDER_MOCK_PROVIDERS", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_anthropic_client():
    """Anthropic-Client (Director + Validator).

    Liest `ANTHROPIC_API_KEY` aus `.env`. Lazy importiert das SDK, damit
    der Import-Kostenfaktor erst beim ersten Aufruf bezahlt wird.
    """
    if use_mock_providers():
        from render_director.mock_providers import MockAnthropicClient
        return MockAnthropicClient()
    import anthropic

    return anthropic.Anthropic()


@lru_cache(maxsize=1)
def get_google_client():
    """Google-GenAI-Client (Nano Banana / Gemini-Image).

    Liest `GOOGLE_API_KEY` aus `.env`. Lazy importiert das SDK aus
    demselben Grund wie oben.
    """
    if use_mock_providers():
        from render_director.mock_providers import MockGoogleClient
        return MockGoogleClient()
    from google import genai

    return genai.Client(api_key=os.environ["GOOGLE_API_KEY"])


@lru_cache(maxsize=1)
def get_openai_client():
    """OpenAI-Client (Generator-Backend „GPT" / gpt-image-1).

    Liest `OPENAI_API_KEY` aus `.env`. Lazy importiert das SDK, damit der
    Import-Kostenfaktor erst beim ersten Aufruf bezahlt wird — analog zu
    Anthropic/Google. Nur nötig, wenn der User das GPT-Backend wählt;
    Director/Validator bleiben Claude, die Standard-Generatoren Gemini.
    """
    if use_mock_providers():
        from render_director.mock_providers import MockOpenAIClient
        return MockOpenAIClient()
    from openai import OpenAI

    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])
