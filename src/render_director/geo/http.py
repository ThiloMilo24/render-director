"""Gemeinsamer HTTP-User-Agent für die öffentlichen Geo-APIs.

OSM Overpass, Wikimedia Commons und Wikidata verlangen einen
aussagekräftigen User-Agent mit Kontaktmöglichkeit. Der Kontakt kommt aus
der Umgebung (`RENDER_HTTP_CONTACT` in `.env`), damit keine persönliche
Adresse im Code steht. Ohne Kontakt geht ein neutraler Hinweis mit.
"""
from __future__ import annotations

import os

from render_director.clients import _ensure_dotenv

APP_NAME = "render-director/0.1"


def user_agent() -> str:
    """User-Agent-String, zur Request-Zeit gebaut (`.env` ist dann geladen)."""
    _ensure_dotenv()
    contact = os.environ.get("RENDER_HTTP_CONTACT", "").strip()
    return "{} ({})".format(APP_NAME, contact or "contact not configured")
