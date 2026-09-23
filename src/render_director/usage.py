"""Token-/Kosten-Erfassung pro API-Call.

Jeder Generator/Director/Validator/Advisor-Call liefert in seiner SDK-
Antwort ein Usage-Objekt. `record_usage()` extrahiert die Token-Zahlen
(defensiv, per getattr — Feldnamen variieren zwischen SDK-Versionen) und
hängt einen normalisierten Record an eine Sink-Liste. `run_iteration` /
`run_iteration_adhoc` sammeln diese Records und schreiben sie als
`usage`-Block in die `inputs.json` (Token = Ground Truth, Kosten =
Schätzung aus der Preistabelle unten).

Bewusst getrennt vom eigentlichen Pipeline-Code, damit die Preistabelle
an einer Stelle gepflegt werden kann.
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Preistabelle — $/1M Tokens, (input, output). GESCHÄTZT, Stand ~2026.
# Preise ändern sich; hier zentral anpassen. Für Bild-Modelle sind die
# Output-Tokens die Bild-Tokens (so bepreisen OpenAI/Google die Generation).
# Tokens in inputs.json sind IMMER exakt (aus response.usage); nur die
# abgeleiteten Kosten sind eine Schätzung.
# ---------------------------------------------------------------------------
PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "gemini-2.5-flash-image": (0.30, 30.0),          # NB1
    "gemini-3.1-flash-image-preview": (0.30, 30.0),  # NB2
    "gpt-image-1": (5.0, 40.0),                       # Text-Input / Bild-Output
}
DEFAULT_PRICING: tuple[float, float] = (3.0, 15.0)


def price_for(model: str) -> tuple[float, float]:
    """(input_$per1M, output_$per1M) für ein Modell — Prefix-Match, damit
    Varianten (z.B. datierte Modell-IDs) auf den Basispreis fallen."""
    if model in PRICING:
        return PRICING[model]
    for key, val in PRICING.items():
        if model.startswith(key):
            return val
    return DEFAULT_PRICING


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Geschätzte Kosten eines Calls aus Token-Zahlen + Preistabelle."""
    pin, pout = price_for(model)
    return (input_tokens / 1_000_000) * pin + (output_tokens / 1_000_000) * pout


def _int(value) -> int:
    """getattr-Ergebnis robust zu int (None/fehlend → 0)."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def record_usage(
    sink: Optional[list],
    *,
    role: str,
    provider: str,
    model: str,
    response,
) -> None:
    """Extrahiert Tokens aus einer SDK-Antwort und hängt einen Record an
    `sink`. No-op, wenn `sink is None` (Default-Pfad ohne Cost-Logging).

    provider ∈ {'anthropic', 'google', 'openai_image'}. Defensiv: fehlt
    das Usage-Objekt oder ein Feld, wird 0 gezählt (kein Crash).
    """
    if sink is None:
        return

    input_tokens = 0
    output_tokens = 0
    if provider == "anthropic":
        u = getattr(response, "usage", None)
        input_tokens = _int(getattr(u, "input_tokens", 0))
        output_tokens = _int(getattr(u, "output_tokens", 0))
    elif provider == "google":
        u = getattr(response, "usage_metadata", None)
        input_tokens = _int(getattr(u, "prompt_token_count", 0))
        output_tokens = _int(getattr(u, "candidates_token_count", 0))
    elif provider == "openai_image":
        u = getattr(response, "usage", None)
        input_tokens = _int(getattr(u, "input_tokens", 0))
        output_tokens = _int(getattr(u, "output_tokens", 0))

    sink.append({
        "role": role,
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost_usd(model, input_tokens, output_tokens), 6),
    })


def summarize(records: list) -> dict:
    """Aggregiert eine Record-Liste zu Summen (für den inputs.json-Block)."""
    total_in = sum(r.get("input_tokens", 0) for r in records)
    total_out = sum(r.get("output_tokens", 0) for r in records)
    total_cost = sum(r.get("cost_usd", 0.0) for r in records)
    return {
        "records": records,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "total_tokens": total_in + total_out,
        "estimated_cost_usd": round(total_cost, 6),
    }
