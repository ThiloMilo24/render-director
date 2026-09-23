"""Mock-Provider für Demo und Tests: die Pipeline läuft ohne API-Keys.

Aktiv mit `RENDER_MOCK_PROVIDERS=1` (auch `true`/`yes`/`on`). `clients.py`
gibt dann statt der echten SDK-Clients diese Attrappen zurück. Sie bilden
genau die Teile der SDK-Oberfläche nach, die die Pipeline nutzt:

- Anthropic `messages.create` → Director-, Validator-, Advisor- und
  Standortanalyse-Antworten in dem Format, das die Parser erwarten
- Google GenAI `models.generate_content` → Bild als `inline_data`
- OpenAI `images.edit` → Bild als `b64_json`, inkl. Maske für Inpaint

Die Antworten sind statisch und deterministisch. Das Ergebnisbild ist das
Eingangsbild mit Farbkorrektur, Vignette und einem „MOCK"-Stempel, damit
ein Mock-Ergebnis nie mit einem echten Render verwechselt wird. Die
Rollen-Erkennung läuft über den Anfang des System-Prompts — ändert sich dort
die erste Zeile, muss `_role_from_system` mitziehen.
"""
from __future__ import annotations

import base64
import json
from io import BytesIO
from types import SimpleNamespace
from typing import Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from render_director.utils import (
    VALIDATOR_SCORE_SCHEMA,
    VALIDATOR_SCORE_SCHEMA_INTERIOR,
    mean_validator_score,
    parse_validator_scores_json,
)

# Advisor-Schwelle analog `prompts/director_advisor_system.md` (Exterior).
_STOP_THRESHOLD = 4.3

MOCK_SITE_ANALYSIS = """## VEGETATION
Lärchen-Fichten-Mischwald an den Hängen, Wiesen und Obstgärten im Talboden.

## TOPOGRAFIE & BERGHINTERGRUND
Im Süden zwei felsige Gipfel um 2800 m, nördlich sanftere bewaldete
Mittelgebirgsrücken. (Mock-Analyse ohne OSM-Daten.)

## LICHT- & ATMOSPHÄRE-CHARAKTERISTIK
Klares, gerichtetes Licht am Vormittag, leichter Dunst in der Tiefe.

## ARCHITEKTUR-KONTEXT (Umfeld)
Zwei- bis dreigeschossige Bauten mit Putz- und Holzfassaden, Satteldächer.

## ANTI-PATTERNS (was NICHT in den Render gehört)
- keine mediterrane Vegetation, stattdessen Lärchen und Wiesen
- keine urbane Hochhauskulisse, stattdessen kleinteilige Bebauung
"""


# ---------------------------------------------------------------------------
# Gemeinsame Helfer
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Grobe Token-Schätzung (≈ 4 Zeichen pro Token) für die Usage-Records."""
    return max(1, len(text) // 4)


def _text_of(content) -> str:
    """Konkateniert alle Text-Blöcke einer Anthropic-Message-Content-Liste."""
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _count_images(content) -> int:
    if isinstance(content, str):
        return 0
    return sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")


def mock_render(image: Image.Image) -> Image.Image:
    """Macht aus dem Eingangsbild ein erkennbares Mock-Ergebnis.

    Wärmere Farben, mehr Kontrast, Vignette und ein „MOCK"-Stempel unten
    rechts. Geometrie bleibt pixelgleich — so lässt sich die Pipeline
    (Refine, Inpaint, Validator-Anker) sichtbar nachvollziehen.
    """
    base = image.convert("RGB")
    graded = ImageEnhance.Color(base).enhance(1.25)
    graded = ImageEnhance.Contrast(graded).enhance(1.08)
    r, g, b = graded.split()
    graded = Image.merge("RGB", (
        r.point(lambda v: min(255, int(v * 1.06))),
        g,
        b.point(lambda v: int(v * 0.92)),
    ))

    w, h = graded.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse(
        [int(-0.15 * w), int(-0.2 * h), int(1.15 * w), int(1.2 * h)], fill=255,
    )
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(w, h) // 12))
    dark = ImageEnhance.Brightness(graded).enhance(0.72)
    out = Image.composite(graded, dark, mask)

    draw = ImageDraw.Draw(out)
    size = max(14, h // 22)
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 kennt kein size-Argument
        font = ImageFont.load_default()
    label = "MOCK"
    left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
    pad = size // 2
    x = w - (right - left) - 2 * pad
    y = h - (bottom - top) - 2 * pad
    draw.rectangle([x - pad, y - pad, w - pad, h - pad], fill=(20, 20, 20))
    draw.text((x, y - top), label, fill=(255, 255, 255), font=font)
    return out


def _png_bytes(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

def _role_from_system(system: str) -> str:
    # Nur den Anfang prüfen: die Prompts verweisen im Fliesstext aufeinander
    # (der Director-Prompt erwähnt z.B. den Director-Advisor).
    head = system.lstrip()[:400]
    if "Geo-Analyst" in head:
        return "site_analysis"
    if head.startswith("# Director-Advisor"):
        return "advisor"
    if "VALIDATION AGENT" in head:
        return "validator_interior" if "Interior-Schiene" in head else "validator"
    if "AD-HOC-MODUS" in head:
        return "director_adhoc"
    if "Interior-Schiene" in head:
        return "director_interior"
    return "director"


_EXTERIOR_PROMPT = """[SCENE TYPE & CAMERA]
Architectural photograph, exterior view, eye-level at 1.7 m, 24 mm lens,
two-point perspective.

[GEOMETRY LOCK — HARD CONSTRAINT]
Preserve exact building geometry, proportions, window positions, roof
angles and facade composition from the reference image. Do not invent or
remove architectural elements.

[MATERIALS]
Facade: vertical larch cladding, naturally weathered. Base: exposed
concrete. Roof: dark grey standing seam zinc. Windows: anthracite frames
with clear glass. Ground: local natural stone paving.

[LIGHTING & ATMOSPHERE]
Late morning, sun from the south-east, soft directional light, clear sky
with a few high clouds, gentle haze in the distance.

[ENVIRONMENT & CONTEXT]
Replace any white placeholder volumes from the reference image with
site-appropriate filled context: neighbouring buildings in regional rural
architecture, distant peaks matching the region, larch and spruce forest
on the slopes. Blend cut terrain edges seamlessly into the meadow.

[STAFFAGE]
Two people walking toward the entrance in casual clothing, no crowds.

[STYLE REFERENCE]
Contemporary architectural photography in the manner of Iwan Baan,
natural and unforced, photorealistic.

[NEGATIVE CONSTRAINTS]
No additional buildings, no changes to the structure, no fantasy
elements, no over-saturated colors, no illustration style, no rendered
look, no over-stylization."""

_INTERIOR_PROMPT = """[SCENE TYPE & CAMERA]
Architectural photograph, interior view of a living room, eye-level at
1.6 m, 28 mm lens, one-point composition toward the window wall.

[GEOMETRY LOCK — HARD CONSTRAINT]
Preserve exact room geometry, proportions, wall positions, ceiling height,
door and window openings from the reference image. Do not invent or
remove architectural elements.

[MATERIALS]
Walls: warm white lime plaster with slight hand-applied texture. Ceiling:
exposed larch boards. Floor: wide-plank oiled oak. Sofa: heavy natural
linen, visible weave, soft asymmetric folds.

[LIGHTING & ATMOSPHERE]
Early evening, golden daylight from the side window, table and floor
lamps already on. Outdoor view kept 1-1.5 stops below interior light
level so the landscape stays readable.

[SPATIAL & FURNISHING CONTEXT]
Replace grey mannequin figures with no people (private living space).
Replace untextured surfaces with the listed materials. Furnish the empty
zone with a low oak side table and a wool rug.

[STAFFAGE]
A stack of books, a ceramic vase with seasonal greenery.

[STYLE REFERENCE]
Contemporary documentary interior photography, natural light, lived-in
warmth, photorealistic.

[NEGATIVE CONSTRAINTS]
No outdoor atmosphere indoors, no HDR bloom, no over-saturated colors, no
illustration style, no rendered look, no fantasy furniture."""


def _director_reply(role: str, user_text: str) -> str:
    prompt = _INTERIOR_PROMPT if role == "director_interior" else _EXTERIOR_PROMPT
    is_iteration = (
        "VORHERIGER BILDGENERIERUNGS-PROMPT" in user_text
        or "VORHERIGER FINAL-PROMPT" in user_text
    )
    if is_iteration:
        intro = (
            "Mock-Director (Iteration): Ich adressiere die zwei wichtigsten "
            "Punkte aus dem Validator-Report und halte den Rest stabil."
        )
        prompt = prompt.replace(
            "[STAFFAGE]",
            "[ITERATION FOCUS]\nSharpen facade material texture; keep the "
            "background calmer and less dramatic.\n\n[STAFFAGE]",
        )
    else:
        intro = (
            "Mock-Director: Geometrie und Materialien aus dem Input übernommen, "
            "Atmosphäre im gewählten Modus gesetzt. (Statische Demo-Antwort.)"
        )
    return "{}\n\n```\n{}\n```\n".format(intro, prompt)


def _validator_reply(role: str) -> str:
    interior = role == "validator_interior"
    schema = VALIDATOR_SCORE_SCHEMA_INTERIOR if interior else VALIDATOR_SCORE_SCHEMA
    scores: dict = {section: {sub: 4 for sub in subs} for section, subs in schema.items()}
    if interior:
        scores["material"]["bodenbelag_innen"] = 3
        scores["geometrie"]["raumproportionen"] = 5
    else:
        scores["material"]["fassade"] = 3
        scores["geometrie"]["massing"] = 5
        scores["anforderung"]["weisse_placeholder_ersetzt"] = 5
    focus = "Bodenbelag" if interior else "Fassadenmaterial"
    return (
        "## 1. GEOMETRIE-CHECK ✓ korrekt\n"
        "Baukörper, Öffnungen und Komposition entsprechen dem Input.\n\n"
        "## 2. MATERIAL-CHECK ⚠ kleine Abweichungen\n"
        f"{focus} wirkt glatter als beschrieben.\n\n"
        "## 3. ANFORDERUNGS-CHECK ✓\n"
        "Atmosphäre und Tageszeit passen zum Modus.\n\n"
        "## 4. KONKRETE VERBESSERUNGSVORSCHLÄGE\n"
        f"- {focus}: Textur und Maserung sichtbarer machen.\n\n"
        "## 5. POSITIVES\n"
        "Lichtstimmung und Geometrie-Treue sind überzeugend.\n\n"
        "_(Mock-Validator, statische Bewertung.)_\n\n"
        "```json\n" + json.dumps(scores, indent=2) + "\n```\n"
    )


def _advisor_reply(user_text: str) -> str:
    scores = parse_validator_scores_json(user_text)
    if scores is None:
        return "ACTION: regenerate\nGRUND: Kein Score-Block lesbar (Mock).\nKONFIDENZ: low"
    schema = (
        VALIDATOR_SCORE_SCHEMA_INTERIOR
        if "raumproportionen" in scores.get("geometrie", {})
        else VALIDATOR_SCORE_SCHEMA
    )
    mean = mean_validator_score(scores, schema=schema) or 0.0
    if mean >= _STOP_THRESHOLD:
        return (
            "ACTION: stop\nGRUND: Mean {:.2f} über der Schwelle, keine ✗-Issues "
            "(Mock).\nKONFIDENZ: high".format(mean)
        )
    return (
        "ACTION: refine\nGRUND: Kein Geometrie-Issue, Mean {:.2f} mit einzelnen "
        "Material-Details — chirurgische Korrektur genügt (Mock).\n"
        "KONFIDENZ: medium".format(mean)
    )


class _MockMessages:
    def create(self, *, model: str, max_tokens: int, system: str = "",
               messages: Optional[list] = None, **_ignored):
        messages = messages or []
        content = messages[-1]["content"] if messages else ""
        user_text = _text_of(content)
        role = _role_from_system(system or "")

        if role == "site_analysis":
            text = MOCK_SITE_ANALYSIS
        elif role == "advisor":
            text = _advisor_reply(user_text)
        elif role.startswith("validator"):
            text = _validator_reply(role)
        else:
            text = _director_reply(role, user_text)

        usage = SimpleNamespace(
            input_tokens=_estimate_tokens(system + user_text) + 1000 * _count_images(content),
            output_tokens=_estimate_tokens(text),
        )
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            model=model,
            usage=usage,
        )


class MockAnthropicClient:
    """Attrappe für `anthropic.Anthropic` (nur `messages.create`)."""

    def __init__(self) -> None:
        self.messages = _MockMessages()


# ---------------------------------------------------------------------------
# Google GenAI
# ---------------------------------------------------------------------------

class _MockModels:
    def generate_content(self, *, model: str, contents: list, config=None, **_ignored):
        images = [c for c in contents if isinstance(c, Image.Image)]
        prompt = " ".join(c for c in contents if isinstance(c, str))
        if not images:
            raise ValueError("Mock-Generator braucht mindestens ein Eingangsbild.")
        data = _png_bytes(mock_render(images[0]))
        part = SimpleNamespace(inline_data=SimpleNamespace(data=data, mime_type="image/png"))
        return SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))],
            usage_metadata=SimpleNamespace(
                prompt_token_count=_estimate_tokens(prompt) + 258 * len(images),
                candidates_token_count=1290,
            ),
        )


class MockGoogleClient:
    """Attrappe für `google.genai.Client` (nur `models.generate_content`)."""

    def __init__(self) -> None:
        self.models = _MockModels()


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

def _image_from_upload(upload) -> Image.Image:
    """OpenAI-Upload-Tuple `(name, bytes, mime)` → PIL-Bild."""
    data = upload[1] if isinstance(upload, tuple) else upload
    return Image.open(BytesIO(data))


class _MockImages:
    def edit(self, *, model: str, image, prompt: str, mask=None, **_ignored):
        uploads = image if isinstance(image, list) else [image]
        primary = _image_from_upload(uploads[0]).convert("RGB")
        rendered = mock_render(primary)
        if mask is not None:
            # Wie die echte API: transparente Masken-Bereiche werden neu
            # gemalt, opake bleiben unverändert.
            alpha = _image_from_upload(mask).convert("RGBA").split()[-1]
            repaint = alpha.point(lambda a: 255 if a == 0 else 0)
            rendered = Image.composite(rendered, primary, repaint)
        b64 = base64.b64encode(_png_bytes(rendered)).decode("ascii")
        return SimpleNamespace(
            data=[SimpleNamespace(b64_json=b64)],
            usage=SimpleNamespace(
                input_tokens=_estimate_tokens(prompt) + 323 * len(uploads),
                output_tokens=4160,
            ),
        )


class MockOpenAIClient:
    """Attrappe für `openai.OpenAI` (nur `images.edit`)."""

    def __init__(self) -> None:
        self.images = _MockImages()
