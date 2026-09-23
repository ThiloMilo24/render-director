"""Orchestrierungs-Kern: Director → Generator → Validator → Score.

Webapp, CLI und pyRevit-Button teilen sich diesen Kern. Zentralisiert als:

- `call_director_v1` / `call_director_iteration` (Anthropic, Sonnet 4.6)
- `call_generator` (Google GenAI, Nano Banana / Gemini-Image)
- `call_validator` (Anthropic, Sonnet 4.6 Vision)
- `run_iteration` — die eine Funktion, die die Webapp/CLI/pyRevit ruft

Schreibreihenfolge der Run-Artefakte:
`result.png` → `final_prompt.txt` → `director_reply.md` → `inputs.json`
→ `validator_reply.md` → Score-Update in `inputs.json`.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image

from render_director.clients import (
    get_anthropic_client,
    get_google_client,
    get_openai_client,
)
from render_director.paths import GENERATIONS_DIR, REFERENCE_LIBRARY_DIR
from render_director.usage import record_usage, summarize
from render_director.prompts import (
    Language,
    _MODE_D_INSTRUCTION,
    advisor_lang_directive,
    build_director_user_text,
    build_iteration_user_text,
    build_validator_user_text,
    director_lang_directive,
    load_advisor_system,
    load_director_adhoc_system,
    load_director_system,
    load_validator_adhoc_system,
    load_validator_system,
)
from render_director.utils import (
    AdvisorAction,
    AdvisorConfidence,
    IterationType,
    MODE_LABELS,
    Mode,
    SceneBundle,
    VALIDATOR_SCORE_SCHEMA,
    VALIDATOR_SCORE_SCHEMA_INTERIOR,
    _FORWARD_ATTACHMENTS_INSTRUCTION,
    _attachment_blocks,
    _id_pass_blocks,
    add_score_summary_to_inputs,
    director_user_content_v1,
    label_from_model,
    load_scene_bundle,
    make_run_dir,
    parse_advisor_recommendation,
    parse_director_recommendation,
    parse_forward_attachments,
    to_anthropic_image_block,
    write_inputs_json,
)

MAX_ATTACHMENTS_PER_TURN = 3

# Default-Modelle. Webapp/CLI übergeben sie als Parameter.
DEFAULT_DIRECTOR_MODEL = "claude-sonnet-4-6"
DEFAULT_GENERATOR_MODEL = "gemini-2.5-flash-image"
DEFAULT_VALIDATOR_MODEL = "claude-sonnet-4-6"
# Ursprünglich 2048 — bei verbose Director-Replies
# (ausführliche Annahmen-Block + 7-Segment-Prompt + Negativ-Constraints)
# reicht das nicht mehr und der Prompt wird mid-stream abgeschnitten.
# Sonnet 4.6 kann deutlich mehr Output liefern, also großzügig setzen.
DIRECTOR_MAX_TOKENS = 8192
VALIDATOR_MAX_TOKENS = 4096
# Advisor outputt nur einen strukturierten 3-Zeilen-Block (ACTION/GRUND/
# KONFIDENZ). 512 ist mehr als genug; eng halten beschneidet auch das
# Modell von "ich schreib mal einen Aufsatz dazu"-Tendenz.
ADVISOR_MAX_TOKENS = 512


# ---------------------------------------------------------------------------
# Director
# ---------------------------------------------------------------------------

def call_director_v1(
    bundle: SceneBundle,
    user_request: str,
    mode: Mode,
    *,
    attachments: Optional[list[Image.Image]] = None,
    attachment_description: Optional[str] = None,
    language: Language = "de",
    model: str = DEFAULT_DIRECTOR_MODEL,
    usage_sink: Optional[list] = None,
) -> str:
    """Initialer Director-Call (V1). Liefert den Markdown-Reply zurück.

    System-Prompt wird passend zu `bundle.meta.view_type` geladen
    (Exterior- oder Interior-Schiene). Beauty geht als Image-Block mit,
    plus Referenz-Bild und Material-/Object-ID-Passes falls vorhanden.

    `language` steuert die Sprache des Nutzer-Vorspanns (de/it); der EN-
    Prompt im Code-Block bleibt sprachunabhängig Englisch.

    `attachments` (optional) sind User-Per-Turn-Bilder. Der Director sieht
    sie, beschreibt sie verbal im EN-Prompt — oder kann via
    `FORWARD_ATTACHMENTS:`-Block dem Generator-Call mitteilen, welche
    Attachments er weitergeben soll (siehe `parse_forward_attachments`).
    """
    system = load_director_system(bundle.meta.view_type)
    user_text = build_director_user_text(bundle, mode, user_request, language)

    # Attachment-Erweiterung: ID-Passes haben Vorrang im Image-Index,
    # Attachments kommen danach (damit deren Nummerierung 1..N stabil
    # im FORWARD_ATTACHMENTS-Block referenziert werden kann).
    attachments = (attachments or [])[:MAX_ATTACHMENTS_PER_TURN]
    if attachments:
        # director_user_content_v1 baut Beauty + Reference + ID-Passes
        # und liefert die Liste; wir hängen die Attachments danach an
        # und schieben das Text-Block ans Ende neu auf.
        content = director_user_content_v1(bundle, user_text)
        # extract existing text block (immer das letzte Element)
        text_block = content.pop()
        existing_text = text_block["text"]

        # ANHANG-Block des Original-Texts könnte schon Bild-Indizes 1..K
        # haben; wir zählen die Image-Blocks vor uns, um den richtigen
        # Start-Index für die Attachments zu bekommen.
        n_existing_images = sum(1 for c in content if c.get("type") == "image")
        att_blocks, att_lines, _ = _attachment_blocks(
            attachments, start_index=n_existing_images + 1,
            description=attachment_description,
        )
        content.extend(att_blocks)

        # Description-Block + Forward-Instruction zum Text hinzufügen
        suffix_parts = []
        if attachment_description:
            suffix_parts.append(
                "\nUSER-ATTACHMENT-BESCHREIBUNG:\n{}\n".format(attachment_description)
            )
        suffix_parts.append("\nATTACHMENT-BILDER:\n  " + "\n  ".join(att_lines) + "\n")
        suffix_parts.append(_FORWARD_ATTACHMENTS_INSTRUCTION.format(n_att=len(attachments)))
        new_text = existing_text + "".join(suffix_parts)
        content.append({"type": "text", "text": new_text})
    else:
        content = director_user_content_v1(bundle, user_text)

    response = get_anthropic_client().messages.create(
        model=model,
        max_tokens=DIRECTOR_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    record_usage(usage_sink, role="director", provider="anthropic", model=model, response=response)
    return response.content[0].text


def call_director_iteration(
    bundle: SceneBundle,
    user_request: str,
    mode: Mode,
    final_prompt_v1: str,
    validator_reply_v1: str,
    previous_image: Image.Image,
    *,
    user_feedback: Optional[str] = None,
    attachments: Optional[list[Image.Image]] = None,
    attachment_description: Optional[str] = None,
    language: Language = "de",
    model: str = DEFAULT_DIRECTOR_MODEL,
    usage_sink: Optional[list] = None,
) -> str:
    """Iterations-Director-Call (V2-Planung).

    Bekommt Beauty + V1-Render als Bilder + V1-Prompt + V1-Validator-
    Report (+ optional User-Feedback) als Text. Plus ggf. Material-ID +
    Object-ID-Passes als Spatial-Anker. Plus ggf. User-Attachments.
    Antwortet mit EMPFEHLUNG-Block (regenerate|refine) + neuem EN-Prompt
    + ggf. FORWARD_ATTACHMENTS-Block. `language` lokalisiert den Vorspann.
    """
    system = load_director_system(bundle.meta.view_type)
    user_text = build_iteration_user_text(
        bundle, mode, user_request, final_prompt_v1, validator_reply_v1,
        user_feedback, language,
    )
    # Bilder: Beauty (1) + V1-Render (2) + ggf. Site-Reference + Material-ID + Object-ID
    images = [
        to_anthropic_image_block(bundle.beauty),
        to_anthropic_image_block(previous_image),
    ]
    next_idx = 3
    if bundle.site_reference is not None:
        site_ref_img = bundle.site_reference
        marker = bundle.meta.site_reference_marker
        if marker is not None:
            from render_director.utils import render_site_reference_marker
            site_ref_img = render_site_reference_marker(
                site_ref_img, marker.x_pct, marker.y_pct,
            )
        images.append(to_anthropic_image_block(site_ref_img))
        next_idx += 1
    id_blocks, _, next_idx = _id_pass_blocks(bundle, start_index=next_idx)
    images.extend(id_blocks)

    # User-Per-Turn-Attachments anhängen
    attachments = (attachments or [])[:MAX_ATTACHMENTS_PER_TURN]
    if attachments:
        att_blocks, att_lines, _ = _attachment_blocks(
            attachments, start_index=next_idx,
            description=attachment_description,
        )
        images.extend(att_blocks)

        suffix_parts = []
        if attachment_description:
            suffix_parts.append(
                "\nUSER-ATTACHMENT-BESCHREIBUNG:\n{}\n".format(attachment_description)
            )
        suffix_parts.append("\nATTACHMENT-BILDER:\n  " + "\n  ".join(att_lines) + "\n")
        suffix_parts.append(_FORWARD_ATTACHMENTS_INSTRUCTION.format(n_att=len(attachments)))
        user_text = user_text + "".join(suffix_parts)

    response = get_anthropic_client().messages.create(
        model=model,
        max_tokens=DIRECTOR_MAX_TOKENS,
        system=system,
        messages=[{
            "role": "user",
            "content": [*images, {"type": "text", "text": user_text}],
        }],
    )
    record_usage(usage_sink, role="director", provider="anthropic", model=model, response=response)
    return response.content[0].text


class DirectorNoPromptError(RuntimeError):
    """Director hat keinen Code-Block geliefert.

    Zwei mögliche Ursachen, unterschieden via `truncated`:
    - `truncated=True` → Director hat den Prompt angefangen aber von einem
      Token-Limit abgeschnitten worden. Fix: max_tokens hochsetzen.
    - `truncated=False` → Director hat tatsächlich eine Rückfrage gestellt
      (typischerweise in Modus B oder bei vagen Prompts).

    Trägt den vollen `director_reply` mit, damit die UI ihn als Error-
    Bubble zeigen kann.
    """
    def __init__(self, director_reply: str):
        self.truncated = looks_truncated(director_reply)
        msg = (
            "Director-Reply scheint vom Token-Limit abgeschnitten — Prompt-Block geöffnet, nicht geschlossen."
            if self.truncated
            else "Director hat keinen Prompt-Code-Block geliefert — vermutlich eine Rückfrage."
        )
        super().__init__(msg)
        self.director_reply = director_reply


def extract_final_prompt(director_reply: str) -> Optional[str]:
    """Extrahiert den ersten Code-Block aus der Director-Antwort.

    Der Director legt den finalen EN-Prompt in einem ```-Block ab. In
    Modus C können mehrere drin sein — wir nehmen den ersten. Liefert `None`, wenn kein Block vorhanden (Director
    hat eine Rückfrage gestellt — in Modus B erwartbar).
    """
    blocks = re.findall(r"```(?:\w+)?\n(.*?)```", director_reply, re.DOTALL)
    return blocks[0].strip() if blocks else None


def looks_truncated(director_reply: str) -> bool:
    """Heuristik: Director hat den Prompt angefangen aber nicht geschlossen.

    Wenn ein ```-Block geöffnet aber nicht geschlossen wurde, war der Reply
    vermutlich von Token-Limit abgeschnitten — wichtig für Error-UX, damit
    der User nicht denkt, der Director hätte eine Rückfrage gestellt.
    """
    opens = director_reply.count("```")
    return opens > 0 and opens % 2 == 1


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def call_generator(
    prompt: str,
    *,
    primary_image: Image.Image,
    depth: Optional[Image.Image] = None,
    forwarded_attachments: Optional[list[Image.Image]] = None,
    mask: Optional[Image.Image] = None,
    model: str = DEFAULT_GENERATOR_MODEL,
    thinking_budget: Optional[int] = None,
    usage_sink: Optional[list] = None,
) -> Image.Image:
    """Generator-Call (Gemini-Image / Nano Banana).

    `primary_image` ist je nach Iterations-Typ verschieden:
    - **initial / regenerate** → Beauty (Geometrie-Anker), optional + Depth.
    - **refine** → der vorherige V1/V2-Render (Edit-Anweisung statt Re-Gen).
      Depth wird beim Refine ignoriert.
    - **inpaint** → der vorherige Render + `mask` (nur gpt-image-Backend).

    `thinking_budget` (optional, nur sinnvoll für Gemini-3-Modelle):
    - `None` (Default) → kein Thinking-Config, klassisches Verhalten
    - `0` → Thinking abgeschaltet (schneller, vermutlich schlechter)
    - `-1` → Dynamic-Budget (Modell entscheidet selbst, „High" in der UI)
    - positive Zahl → expliziter Token-Cap fürs Thinking

    `mask` (optional): Inpaint-Maske. **Nur das gpt-image-Backend hat native
    Masken-Unterstützung** — für Gemini gibt es keine; ein Maske+Gemini-Aufruf
    wirft `ValueError` (die UI erzwingt gpt-image für Inpaint). Semantik der
    Maske siehe `_call_openai_generator`.

    Wirft `RuntimeError`, wenn die Gemini-Antwort kein Bild enthält.

    Backend-Wahl über den Model-String: `"gpt-image"`-Modelle laufen über
    OpenAI (`_call_openai_generator`), alles andere über Google-GenAI.
    """
    if mask is not None and not model.startswith("gpt-image"):
        raise ValueError(
            f"Inpaint-Maske wird nur vom gpt-image-Backend unterstützt, "
            f"nicht von {model!r}. Bitte Generator 'GPT' (gpt-image-1) wählen."
        )

    if model.startswith("gpt-image"):
        # OpenAI-Zweig. Depth wird bewusst NICHT weitergereicht — gpt-image-1
        # hat keine Depth-Konditionierung; ein roher Depth-Map als weiteres
        # Referenzbild würde die Komposition eher stören als helfen. Der
        # Geometrie-Anker steckt in `primary_image` + `input_fidelity="high"`.
        # `thinking_budget` ist Gemini-spezifisch und hier ohne Wirkung.
        return _call_openai_generator(
            prompt,
            primary_image=primary_image,
            forwarded_attachments=forwarded_attachments,
            mask=mask,
            model=model,
            usage_sink=usage_sink,
        )

    contents: list = [primary_image]
    if depth is not None:
        contents.append(depth)
    if forwarded_attachments:
        contents.extend(forwarded_attachments)
    contents.append(prompt)

    kwargs: dict = {"model": model, "contents": contents}
    if thinking_budget is not None and "gemini-3" in model:
        # ThinkingConfig nur für Gemini-3-Modelle anwenden — ältere Modelle
        # (gemini-2.5-flash-image / NB1) kennen das Feld nicht.
        from google.genai import types as _genai_types

        kwargs["config"] = _genai_types.GenerateContentConfig(
            thinking_config=_genai_types.ThinkingConfig(thinking_budget=thinking_budget),
        )

    response = get_google_client().models.generate_content(**kwargs)
    record_usage(usage_sink, role="generator", provider="google", model=model, response=response)
    result_img: Optional[Image.Image] = None
    for part in response.candidates[0].content.parts:
        if getattr(part, "inline_data", None) is not None:
            # WICHTIG: convert("RGB") erzwingt sofortiges Decoding + normalisiert
            # den Modus. Ohne das bleibt das Image lazy-loaded an die BytesIO
            # gebunden; nachfolgende Operationen (insb. .copy() in
            # to_anthropic_image_block fuer den Validator-Call) lesen dann
            # aus einem evtl. schon erschoepften BytesIO-Stream und liefern
            # ein schwarzes Bild zurueck — selbst wenn der erste save() auf
            # Disk noch korrekt war (siehe Phase-5-Bug 2026-06-19, NB2-Runs
            # in archive_phase4/{3d}/: result.png ok, Validator-Score 1.0
            # mit "Bild ist vollstaendig schwarz").
            result_img = Image.open(BytesIO(part.inline_data.data)).convert("RGB")
            break
    if result_img is None:
        raise RuntimeError(
            f"Generator {model!r} hat kein Bild zurückgegeben. "
            f"Vermutlich Safety-Filter oder leere Antwort."
        )
    return result_img


# OpenAI-Image-Qualität. `high` = beste Detail-/Materialtreue (teurer, aber
# der Beauty-Anspruch rechtfertigt es). Als Konstante gehalten, damit ein
# Kosten-Downgrade später ein Einzeiler ist.
OPENAI_IMAGE_QUALITY = "high"


def _openai_size_for(img: Image.Image) -> str:
    """Wählt die gpt-image-Zielgröße nächst dem Seitenverhältnis der Vorlage.

    gpt-image-1 kennt nur `1024x1024`, `1536x1024`, `1024x1536`. Wir mappen
    aufs Beauty-Seitenverhältnis, damit die Framing-Absicht des Ankers
    erhalten bleibt (statt `auto`, das das Modell frei wählen ließe).
    """
    w, h = img.size
    if w > h:
        return "1536x1024"
    if h > w:
        return "1024x1536"
    return "1024x1024"


def _pil_to_png_upload(img: Image.Image, name: str, *, keep_alpha: bool = False) -> tuple:
    """PIL-Bild → OpenAI-File-Tuple `(name, bytes, mime)` für `images.edit`.

    In-Memory-Bilder brauchen für den Multipart-Upload einen Dateinamen mit
    korrekter Endung + MIME, sonst lehnt die API sie als „invalid file
    format" ab. Das Tuple-Format ist der dokumentierte sichere Weg.

    `keep_alpha=True` erhält den Alpha-Kanal (RGBA statt RGB) — nötig für
    die Inpaint-Maske: OpenAI liest den Alpha-Kanal (transparent = „hier
    neu malen"). Für die normalen Bild-Uploads bleibt RGB der Default,
    damit ein evtl. Alpha-Kanal die Geometrie-Treue nicht stört.
    """
    buf = BytesIO()
    img.convert("RGBA" if keep_alpha else "RGB").save(buf, format="PNG")
    return (name, buf.getvalue(), "image/png")


def _call_openai_generator(
    prompt: str,
    *,
    primary_image: Image.Image,
    forwarded_attachments: Optional[list[Image.Image]] = None,
    mask: Optional[Image.Image] = None,
    model: str = "gpt-image-1",
    usage_sink: Optional[list] = None,
) -> Image.Image:
    """Generator-Call über OpenAI `images.edit` (Backend „GPT").

    Bild-Reihenfolge entspricht dem Gemini-Zweig ohne Depth: `primary_image`
    (Geometrie-Anker: Beauty bei initial/regenerate, Vorgänger-Render bei
    refine) zuerst, dann `forwarded_attachments` als weitere Referenzbilder
    (Director-gated). gpt-image-1 nimmt bis zu 16 Bilder — wir liegen mit
    max. 1 + 3 weit darunter.

    `input_fidelity="high"` hält Detail/Struktur des ersten Bildes maximal —
    genau das, was der Beauty-Anker braucht. Wird nur für die gpt-image-1-
    Familie gesetzt (gpt-image-2 verarbeitet ohnehin high-fidelity und
    verbietet den Parameter).

    `mask` (optional, nur Inpaint): PNG mit Alpha-Kanal. **Transparente
    Bereiche (alpha=0) werden neu gemalt**, opake Bereiche bleiben. Die
    Maske MUSS exakt die Größe des `primary_image` haben — wir resizen
    defensiv, falls die vom Canvas exportierte Auflösung leicht abweicht.
    Die Maske wird auf das erste Bild (`primary.png`) angewandt.

    Wirft `RuntimeError`, wenn die Antwort kein Bild enthält.
    """
    uploads = [_pil_to_png_upload(primary_image, "primary.png")]
    for i, att in enumerate(forwarded_attachments or []):
        uploads.append(_pil_to_png_upload(att, f"ref_{i}.png"))

    kwargs: dict = {
        "model": model,
        "image": uploads,
        "prompt": prompt,
        "size": _openai_size_for(primary_image),
        "quality": OPENAI_IMAGE_QUALITY,
    }
    if model.startswith("gpt-image-1"):
        kwargs["input_fidelity"] = "high"
    if mask is not None:
        # OpenAI verlangt Maske == Bildgröße. Der Canvas exportiert in der
        # Natural-Resolution des Result-Bildes, die i.d.R. schon passt —
        # aber ein Resize kostet nichts und macht den Aufruf robust.
        if mask.size != primary_image.size:
            mask = mask.resize(primary_image.size)
        kwargs["mask"] = _pil_to_png_upload(mask, "mask.png", keep_alpha=True)

    response = get_openai_client().images.edit(**kwargs)
    record_usage(usage_sink, role="generator", provider="openai_image", model=model, response=response)
    if not getattr(response, "data", None) or not response.data[0].b64_json:
        raise RuntimeError(
            f"Generator {model!r} hat kein Bild zurückgegeben. "
            f"Vermutlich Content-Filter oder leere Antwort."
        )
    return Image.open(BytesIO(base64.b64decode(response.data[0].b64_json))).convert("RGB")


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def call_validator(
    bundle: SceneBundle,
    result_img: Image.Image,
    user_request: str,
    mode: Mode,
    final_prompt: str,
    *,
    language: Language = "de",
    model: str = DEFAULT_VALIDATOR_MODEL,
    usage_sink: Optional[list] = None,
) -> str:
    """Validator-Call (Anthropic Vision).

    Bilder: Beauty (+ Depth falls vorhanden) + Result. Validator-System-
    Prompt wird passend zu view_type geladen. Output ist Markdown-Report
    plus abschließender JSON-Score-Block (siehe
    `parse_validator_scores_json`).
    """
    system = load_validator_system(bundle.meta.view_type)
    text = build_validator_user_text(
        bundle, mode, user_request, final_prompt, language=language,
    )
    # Bild-Reihenfolge: Beauty (1) + ggf. Depth (2) + ggf. Material-ID +
    # Object-ID + Result (letztes). Result kommt zuletzt, damit der Index
    # im Text („Bild N (letztes) = Result") berechnet werden kann.
    images = [to_anthropic_image_block(bundle.beauty)]
    next_idx = 2
    if bundle.depth is not None:
        images.append(to_anthropic_image_block(bundle.depth))
        next_idx += 1
    id_blocks, _, next_idx = _id_pass_blocks(bundle, start_index=next_idx)
    images.extend(id_blocks)
    images.append(to_anthropic_image_block(result_img))

    response = get_anthropic_client().messages.create(
        model=model,
        max_tokens=VALIDATOR_MAX_TOKENS,
        system=system,
        messages=[{
            "role": "user",
            "content": [*images, {"type": "text", "text": text}],
        }],
    )
    record_usage(usage_sink, role="validator", provider="anthropic", model=model, response=response)
    return response.content[0].text


def call_validator_adhoc(
    input_image: Image.Image,
    result_img: Image.Image,
    user_request: str,
    mode: Mode,
    final_prompt: str,
    *,
    language: Language = "de",
    model: str = DEFAULT_VALIDATOR_MODEL,
    iteration_type: IterationType = "initial",
    usage_sink: Optional[list] = None,
) -> str:
    """Validator-Call im Ad-hoc-Modus (Anthropic Vision, vision-only).

    Wie `call_validator`, aber ohne SceneBundle: das User-Input-Bild ist
    die Geometrie-Ground-Truth (statt Enscape-Beauty + BIM). Kein Depth,
    keine ID-Passes, keine BIM-Materialliste. Nutzt `validator_adhoc_
    system.md` — gleiches JSON-Score-Schema wie Exterior, also identisch
    parsbar durch `add_score_summary_to_inputs(... VALIDATOR_SCORE_SCHEMA)`.

    Bild-Reihenfolge: Input (1) + Result (2, letztes).
    """
    from render_director.prompts import validator_lang_directive

    system = load_validator_adhoc_system()
    # Bei Inpaint weiß der Validator sonst nicht, dass nur eine markierte
    # Region geändert wurde — und bemängelt dann Dinge im unveränderten Rest.
    inpaint_note = ""
    if iteration_type == "inpaint":
        inpaint_note = (
            "\nWICHTIG — DIES WAR EIN INPAINT (maskierter, lokaler Edit):\n"
            "Es wurde NUR eine vom User markierte Region neu gerendert; der "
            "restliche Bildinhalt stammt unverändert aus dem vorigen Render. Der "
            "obige DIRECTOR-PROMPT beschreibt genau diesen lokalen Eingriff (nicht "
            "die ganze Szene). Bewerte primär, ob dieser Eingriff in der Region "
            "gelungen ist und sich sauber einfügt. Bemängele NICHT Dinge im "
            "unveränderten Rest des Bildes — die waren schon vorher da und sind "
            "nicht Ziel dieses Schritts.\n"
        )
    text = (
        f"{validator_lang_directive(language)}\n"
        "\n"
        "URSPRÜNGLICHE NUTZER-ANFORDERUNG:\n"
        f"{user_request}\n"
        "\n"
        f"MODUS: {mode} ({MODE_LABELS[mode]})\n"
        "\n"
        "KEINE BIM-METADATEN verfügbar (Ad-hoc-Modus) — Materialien rein "
        "visuell auf Plausibilität/Konsistenz prüfen.\n"
        "\n"
        "DIRECTOR-PROMPT (zum Kontext, was angefordert wurde):\n"
        f"{final_prompt}\n"
        f"{inpaint_note}"
        "\n"
        "ANHANG:\n"
        "  Bild 1 = User-Input-Bild (Geometrie- und Kompositions-Ground-Truth)\n"
        "  Bild 2 (letztes) = Generierter Render (zu prüfen)\n"
    )
    images = [
        to_anthropic_image_block(input_image),
        to_anthropic_image_block(result_img),
    ]
    response = get_anthropic_client().messages.create(
        model=model,
        max_tokens=VALIDATOR_MAX_TOKENS,
        system=system,
        messages=[{
            "role": "user",
            "content": [*images, {"type": "text", "text": text}],
        }],
    )
    record_usage(usage_sink, role="validator", provider="anthropic", model=model, response=response)
    return response.content[0].text


# ---------------------------------------------------------------------------
# Director-Advisor (Iterations-Empfehlung)
# ---------------------------------------------------------------------------

def call_director_advisor(
    anchor_image: Image.Image,
    result_img: Image.Image,
    validator_reply: str,
    *,
    language: Language = "de",
    model: str = DEFAULT_DIRECTOR_MODEL,
    usage_sink: Optional[list] = None,
) -> str:
    """Director-Advisor-Call — gibt ACTION/GRUND/KONFIDENZ aus.

    Wird nach dem Validator gerufen, BEVOR der User die naechste Iteration
    triggert. So sieht der User die Empfehlung mit dem aktuellen Result
    zusammen und kann handeln, statt sie post-hoc in der naechsten
    Iteration zu lesen.

    Inputs: `anchor_image` (Geometrie-Anker: Snapshot-Beauty ODER Ad-hoc-
    Input-Bild) + Result (was bewertet wurde) + Validator-Reply als Text.
    Bewusst KEIN final_prompt und KEIN mode — der Advisor soll sich nur am
    Validator-Bericht orientieren, nicht am Prompt-Reasoning des Directors
    (sonst kann er den Director-Bias uebernehmen).

    Output: 3-Zeilen-Block, parsbar durch `parse_advisor_recommendation`.
    """
    system = load_advisor_system()
    user_text = (
        "{lang}\n\n"
        "Du siehst zwei Bilder:\n"
        "  Bild 1 = Geometrie-Anker (Original-Input bzw. Enscape-Beauty)\n"
        "  Bild 2 = Result (gerade generiert, vom Validator unten "
        "bewertet)\n\n"
        "Validator-Reply unten. Lies ihn vollstaendig, dann gib deine "
        "Empfehlung im vorgeschriebenen 3-Zeilen-Format aus.\n\n"
        "--- VALIDATOR-REPLY ---\n"
        "{reply}\n"
        "--- ENDE VALIDATOR-REPLY ---"
    ).format(lang=advisor_lang_directive(language), reply=validator_reply)

    images = [
        to_anthropic_image_block(anchor_image),
        to_anthropic_image_block(result_img),
    ]
    response = get_anthropic_client().messages.create(
        model=model,
        max_tokens=ADVISOR_MAX_TOKENS,
        system=system,
        messages=[{
            "role": "user",
            "content": [*images, {"type": "text", "text": user_text}],
        }],
    )
    record_usage(usage_sink, role="advisor", provider="anthropic", model=model, response=response)
    return response.content[0].text


# ---------------------------------------------------------------------------
# Validator + Advisor als wiederverwendbarer Schritt (Zwei-Phasen-Split)
# ---------------------------------------------------------------------------
# Damit das UI das Bild sofort nach dem Generator zeigen kann und Validator +
# Advisor erst danach nachlaufen, ist der Bewertungs-Schritt aus run_iteration/
# run_iteration_adhoc herausgelöst. Beide Pfade (klassisch Ein-Pass ODER
# nachgelagert via finalize_*) rufen denselben Helfer — keine Duplikation.


@dataclass
class ValidationOutcome:
    """Ergebnis des Validator+Advisor-Schritts (Phase 2)."""
    validator_reply: str
    final_score: Optional[float]
    section_means: dict[str, Optional[float]] = field(default_factory=dict)
    advisor_action: Optional[AdvisorAction] = None
    advisor_reason: Optional[str] = None
    advisor_confidence: Optional[AdvisorConfidence] = None
    advisor_reply: Optional[str] = None


def _write_usage(run_dir: Path, usage_summary: dict) -> None:
    """Schreibt den `usage`-Block frisch in inputs.json (defensiv).

    Liest inputs.json neu ein (add_score_summary_to_inputs hat evtl. schon
    section_means reingeschrieben) und ergänzt/überschreibt nur `usage`.
    """
    import json as _json
    try:
        p = run_dir / "inputs.json"
        d = _json.loads(p.read_text(encoding="utf-8"))
        d["usage"] = usage_summary
        p.write_text(_json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        import sys as _sys
        print("[usage] WARN: konnte usage nicht schreiben: {!r}".format(exc), file=_sys.stderr)


def _run_advisor(
    run_dir: Path,
    anchor_image: Image.Image,
    result_img: Image.Image,
    validator_reply: str,
    *,
    validator_language: Language,
    director_model: str,
    usage_sink: list,
) -> tuple[Optional[AdvisorAction], Optional[str], Optional[AdvisorConfidence], Optional[str]]:
    """Advisor-Call (defensiv) + advisor_reply.md schreiben. Fehler → (None,…)."""
    try:
        advisor_reply = call_director_advisor(
            anchor_image, result_img, validator_reply,
            language=validator_language, model=director_model, usage_sink=usage_sink,
        )
        (run_dir / "advisor_reply.md").write_text(advisor_reply, encoding="utf-8")
        parsed = parse_advisor_recommendation(advisor_reply)
        if parsed is not None:
            return (*parsed, advisor_reply)
        return None, None, None, advisor_reply
    except Exception as exc:
        import sys as _sys
        print(
            "[advisor] WARN: Advisor-Call fehlgeschlagen: {!r} — "
            "Empfehlung wird nicht angezeigt.".format(exc),
            file=_sys.stderr,
        )
        return None, None, None, None


def _validate_snapshot(
    run_dir: Path,
    *,
    bundle: SceneBundle,
    result_img: Image.Image,
    user_prompt: str,
    mode: Mode,
    final_prompt: str,
    validator_language: Language,
    validator_model: str,
    director_model: str,
    usage_sink: list,
) -> ValidationOutcome:
    """Validator + Score + Advisor für die Snapshot-Schiene. Appended an
    `usage_sink` (Caller schreibt den usage-Block). Schreibt validator_reply.md,
    advisor_reply.md + section_means/final_score in inputs.json."""
    import json as _json
    validator_reply = call_validator(
        bundle, result_img, user_prompt, mode, final_prompt,
        language=validator_language, model=validator_model, usage_sink=usage_sink,
    )
    (run_dir / "validator_reply.md").write_text(validator_reply, encoding="utf-8")
    schema = (
        VALIDATOR_SCORE_SCHEMA_INTERIOR
        if bundle.meta.view_type == "interior"
        else VALIDATOR_SCORE_SCHEMA
    )
    final_score = add_score_summary_to_inputs(run_dir, validator_reply, schema=schema)
    payload = _json.loads((run_dir / "inputs.json").read_text(encoding="utf-8"))
    section_means = payload.get("section_means") or {}
    aa, ar, ac, arep = _run_advisor(
        run_dir, bundle.beauty, result_img, validator_reply,
        validator_language=validator_language, director_model=director_model,
        usage_sink=usage_sink,
    )
    return ValidationOutcome(validator_reply, final_score, section_means, aa, ar, ac, arep)


def _validate_adhoc(
    run_dir: Path,
    *,
    input_image: Image.Image,
    result_img: Image.Image,
    user_prompt: str,
    mode: Mode,
    final_prompt: str,
    validator_language: Language,
    validator_model: str,
    director_model: str,
    usage_sink: list,
    iteration_type: IterationType = "initial",
) -> ValidationOutcome:
    """Validator + Score + Advisor für die Ad-hoc-Schiene (vision-only, kein
    BIM → immer Exterior-Schema, Anker = Input-Bild)."""
    import json as _json
    validator_reply = call_validator_adhoc(
        input_image, result_img, user_prompt, mode, final_prompt,
        language=validator_language, model=validator_model,
        iteration_type=iteration_type, usage_sink=usage_sink,
    )
    (run_dir / "validator_reply.md").write_text(validator_reply, encoding="utf-8")
    final_score = add_score_summary_to_inputs(run_dir, validator_reply, schema=VALIDATOR_SCORE_SCHEMA)
    payload = _json.loads((run_dir / "inputs.json").read_text(encoding="utf-8"))
    section_means = payload.get("section_means") or {}
    aa, ar, ac, arep = _run_advisor(
        run_dir, input_image, result_img, validator_reply,
        validator_language=validator_language, director_model=director_model,
        usage_sink=usage_sink,
    )
    return ValidationOutcome(validator_reply, final_score, section_means, aa, ar, ac, arep)


# ---------------------------------------------------------------------------
# Referenzbibliothek (Tier-0-RAG)
# ---------------------------------------------------------------------------

def _augment_with_reference_library(
    forwarded: list[Image.Image],
    *,
    k: int,
    root: Optional[Path | str],
    project_query: Optional[str],
    materials: Optional[dict] = None,
    time_of_day: Optional[str] = None,
    extra_text: str = "",
) -> list[str]:
    """Füllt `forwarded` mit passenden Bibliotheks-Fotos auf `k` Bilder auf.

    Augment, nicht Override: hat der User schon genug eigene Referenzen
    weitergereicht, kommt nichts dazu (kuratierte Wahl gewinnt). Hängt die
    Bilder direkt an `forwarded` an und liefert ihre Dateinamen (Forensik in
    inputs.json). Defensiv: fehlt der Ordner oder crasht das Matching, läuft
    der Render ohne Referenzen weiter.
    """
    n_needed = max(0, k - len(forwarded))
    if n_needed == 0:
        return []
    used: list[str] = []
    try:
        from render_director.rag.library import select_reference_images
        refs = select_reference_images(
            root=root if root is not None else REFERENCE_LIBRARY_DIR,
            k=n_needed,
            project_query=project_query,
            materials=materials,
            time_of_day=time_of_day,
            extra_text=extra_text,
        )
        for r in refs:
            forwarded.append(Image.open(r.path).convert("RGB"))
            used.append(r.path.name)
    except Exception as exc:
        import sys as _sys
        print(
            "[reflib] WARN: Referenzbibliothek-Match fehlgeschlagen: "
            "{!r} — Render läuft ohne Referenzen weiter.".format(exc),
            file=_sys.stderr,
        )
    return used


# ---------------------------------------------------------------------------
# IterationResult + run_iteration
# ---------------------------------------------------------------------------

@dataclass
class IterationResult:
    """Ergebnis eines `run_iteration`-Aufrufs.

    Enthält alle Run-Artefakte als Pfade (für Webapp-Anzeige) plus die
    drei AI-Replies als Strings (für Chat-UI + Re-Use im nächsten
    Iterations-Director). `final_score` ist der Mittelwert über alle
    nicht-`null` Sub-Dimensionen — `None`, wenn der Validator keinen
    parsbaren JSON-Block geliefert hat (sollte nicht passieren, aber
    robust).
    """
    run_dir: Path
    run_id: str
    generated_image_path: Path
    final_prompt: str
    director_reply: str
    validator_reply: str
    final_score: Optional[float]
    section_means: dict[str, Optional[float]] = field(default_factory=dict)
    iteration_type: IterationType = "initial"
    parent_run_id: Optional[str] = None
    # DEPRECATED 2026-05-27: Empfehlung kommt jetzt vom Director-Advisor
    # (`advisor_*`-Felder unten), nicht mehr aus dem Director-Iteration-
    # Reply. Felder bleiben fuer Backward-Kompat in alten Run-Artefakten.
    director_recommendation: Optional[str] = None  # "regenerate" | "refine"
    director_recommendation_reason: Optional[str] = None
    # Advisor-Empfehlung — neu seit 2026-05-27. None heisst entweder
    # initial-Run (kein Advisor gelaufen) oder Parser-Fail.
    advisor_action: Optional[AdvisorAction] = None
    advisor_reason: Optional[str] = None
    advisor_confidence: Optional[AdvisorConfidence] = None
    advisor_reply: Optional[str] = None  # roher Text, fuers Debugging
    # Tier-0-Referenzbibliothek: Dateinamen der als Stil-Anker injizierten
    # Bibliotheks-Fotos (leer, wenn nicht genutzt / kein Match).
    reference_library_refs: list[str] = field(default_factory=list)


def run_iteration(
    snapshot_dir: Path | str,
    user_prompt: str,
    *,
    mode: Mode = "A",
    iteration_type: IterationType = "initial",
    parent_run_id: Optional[str] = None,
    previous_image_path: Optional[Path | str] = None,
    previous_final_prompt: Optional[str] = None,
    previous_validator_reply: Optional[str] = None,
    user_feedback: Optional[str] = None,
    attachments: Optional[list[Image.Image]] = None,
    attachment_description: Optional[str] = None,
    run_index: int = 1,
    generations_root: Optional[Path | str] = None,
    phase_label: Optional[str] = None,
    director_model: str = DEFAULT_DIRECTOR_MODEL,
    generator_model: str = DEFAULT_GENERATOR_MODEL,
    generator_thinking_budget: Optional[int] = None,
    validator_model: str = DEFAULT_VALIDATOR_MODEL,
    validator_language: Language = "de",
    use_reference_library: bool = False,
    reference_library_k: int = 2,
    reference_library_root: Optional[Path | str] = None,
    reference_project_tag: Optional[str] = None,
    defer_validation: bool = False,
) -> IterationResult:
    """Eine vollständige Iteration: Director → Generator → Validator → Score.

    `defer_validation=True` (Zwei-Phasen-UI): stoppt nach dem Persistieren des
    Bildes und gibt ein IterationResult OHNE Validator/Score/Advisor zurück
    (diese Felder sind None). Der Caller lässt danach `finalize_iteration(
    run_dir)` nachlaufen. So kann das UI das Bild sofort zeigen.

    Bei `iteration_type="initial"`:
      - `call_director_v1` mit Beauty als Anker.
      - Generator wird mit Beauty (+ optional Depth) + final_prompt aufgerufen.
      - Validator scort Beauty (+ Depth) vs Result.

    Bei `iteration_type in {"regenerate", "refine"}`:
      - `call_director_iteration` mit Beauty + previous_image als Bilder,
        plus previous_final_prompt + previous_validator_reply (+ optional
        user_feedback) als Text.
      - Director-Empfehlung muss aber **nicht** mit `iteration_type`
        übereinstimmen — der Director liefert seinen eigenen Vorschlag
        (`director_recommendation`), die Wahl, was tatsächlich ausgeführt
        wird, liegt beim Caller (Webapp). Hier wird der `iteration_type`-
        Parameter respektiert: `regenerate` → Generator(Beauty+Depth),
        `refine` → Generator(previous_image, ohne Depth).
      - `parent_run_id` ist Pflicht.

    Speichert in `<generations_root>/[<phase_label>/<Scene_XX>/]
    <timestamp>_<scene_id>_mode<M>_run<NN>_<NB-Label>/` mit allen
    Artefakten (result.png, final_prompt.txt, director_reply.md,
    inputs.json, validator_reply.md).
    """
    snapshot_dir = Path(snapshot_dir)
    bundle = load_scene_bundle(snapshot_dir)

    # Phase-5-Site-Analyse on-demand: wenn Exterior-Render mit Lat/Lon
    # gesetzt ist und noch kein Cache existiert, einmalig Wikimedia+OSM
    # ziehen + Claude-Vision-Analyse laufen lassen + cachen.
    # Defensive: any failure -> silent skip, Pipeline läuft ohne SITE-
    # ANALYSE weiter (der GEO-KONTEXT-Block bleibt dann nur die Coords).
    if (
        bundle.site_analysis is None
        and bundle.meta.view_type == "exterior"
        and bundle.meta.site_location is not None
        and bundle.meta.site_location.latitude_deg is not None
        and bundle.meta.site_location.longitude_deg is not None
    ):
        try:
            from render_director.geo.site_analyzer import get_site_analysis
            analysis_text = get_site_analysis(
                snapshot_dir,
                bundle.meta.site_location.latitude_deg,
                bundle.meta.site_location.longitude_deg,
            )
            if analysis_text:
                bundle.site_analysis = analysis_text
        except Exception as exc:
            import sys as _sys
            print(
                "[site_analysis] WARN: konnte Analyse nicht laden/erzeugen: "
                "{!r} — Pipeline läuft ohne SITE-ANALYSE weiter.".format(exc),
                file=_sys.stderr,
            )
    attachments = (attachments or [])[:MAX_ATTACHMENTS_PER_TURN]

    # Cost-Logging: sammelt Token-Usage jedes API-Calls (Director + Generator
    # + Validator + Advisor), wird am Ende als `usage`-Block in inputs.json
    # geschrieben. `record_usage`/`summarize` sind No-ops ohne diese Liste.
    usage_sink: list = []

    # --- Director ---------------------------------------------------------
    if iteration_type == "initial":
        if parent_run_id is not None:
            raise ValueError("initial-Run darf keine parent_run_id haben")
        director_reply = call_director_v1(
            bundle, user_prompt, mode,
            attachments=attachments,
            attachment_description=attachment_description,
            language=validator_language,
            model=director_model,
            usage_sink=usage_sink,
        )
        recommendation: Optional[str] = None
        recommendation_reason: Optional[str] = None
        previous_image: Optional[Image.Image] = None
    else:
        if parent_run_id is None:
            raise ValueError(f"{iteration_type}-Run braucht eine parent_run_id")
        if not (previous_image_path and previous_final_prompt and previous_validator_reply):
            raise ValueError(
                f"{iteration_type}-Run braucht previous_image_path + "
                "previous_final_prompt + previous_validator_reply"
            )
        previous_image = Image.open(previous_image_path).convert("RGB")
        director_reply = call_director_iteration(
            bundle, user_prompt, mode,
            final_prompt_v1=previous_final_prompt,
            validator_reply_v1=previous_validator_reply,
            previous_image=previous_image,
            user_feedback=user_feedback,
            attachments=attachments,
            attachment_description=attachment_description,
            language=validator_language,
            model=director_model,
            usage_sink=usage_sink,
        )
        rec = parse_director_recommendation(director_reply)
        if rec is not None:
            recommendation, recommendation_reason = rec
        else:
            recommendation = None
            recommendation_reason = None

    # Parse FORWARD_ATTACHMENTS-Decision aus dem Director-Reply
    forward_indices = parse_forward_attachments(director_reply) if attachments else []
    forwarded = [
        attachments[i - 1] for i in forward_indices
        if 1 <= i <= len(attachments)
    ]

    # --- Tier-0-Referenzbibliothek (Augment) ------------------------------
    # Nur Exterior (die Bibliotheks-Fotos sind Fassaden/Außen). Projektname
    # zuerst (passt er zu einem Ordner der Bibliothek, gewinnt das), sonst
    # Material + Tageszeit aus meta.json. Ein vom User im Formular getippter
    # Tag überschreibt den BIM-Projektnamen (z.B. wenn der Revit-Name nicht
    # zum Foto-Ordner passt).
    reference_library_used: list[str] = []
    if use_reference_library and bundle.meta.view_type == "exterior":
        reference_library_used = _augment_with_reference_library(
            forwarded,
            k=reference_library_k,
            root=reference_library_root,
            project_query=(reference_project_tag or bundle.meta.project_name),
            materials=bundle.meta.materials,
            time_of_day=bundle.meta.time_of_day,
            extra_text=user_prompt,
        )

    final_prompt = extract_final_prompt(director_reply)
    if final_prompt is None:
        raise DirectorNoPromptError(director_reply)

    # --- Generator --------------------------------------------------------
    if iteration_type == "refine":
        assert previous_image is not None  # garantiert durch Branch oben
        result_img = call_generator(
            final_prompt, primary_image=previous_image, depth=None,
            forwarded_attachments=forwarded,
            model=generator_model,
            thinking_budget=generator_thinking_budget,
            usage_sink=usage_sink,
        )
    else:  # initial oder regenerate → vom Beauty
        result_img = call_generator(
            final_prompt,
            primary_image=bundle.beauty,
            depth=bundle.depth,
            forwarded_attachments=forwarded,
            model=generator_model,
            thinking_budget=generator_thinking_budget,
            usage_sink=usage_sink,
        )

    # --- Persistieren -----------------------------------------------------
    gen_root = Path(generations_root) if generations_root else GENERATIONS_DIR
    run_dir = make_run_dir(
        gen_root, bundle.scene_id, mode,
        run_index=run_index,
        label=label_from_model(generator_model),
        phase_label=phase_label,
    )
    run_id = run_dir.name

    result_path = run_dir / "result.png"
    result_img.save(result_path)
    (run_dir / "final_prompt.txt").write_text(final_prompt, encoding="utf-8")
    (run_dir / "director_reply.md").write_text(director_reply, encoding="utf-8")

    # Attachments persistieren (forensisch nachvollziehbar, was dem Director
    # mitgegeben wurde + welche an den Generator weitergeleitet wurden).
    attachment_filenames: list[str] = []
    for i, att in enumerate(attachments, start=1):
        att_filename = "attachment_{:02d}.png".format(i)
        att.save(run_dir / att_filename)
        attachment_filenames.append(att_filename)

    iteration_reason = recommendation_reason if iteration_type != "initial" else None
    extra: dict = {"validator_language": validator_language}
    if user_feedback:
        extra["user_feedback"] = user_feedback
    if recommendation:
        extra["director_recommendation"] = recommendation
    if attachment_filenames:
        extra["attachments"] = attachment_filenames
        extra["attachment_description"] = attachment_description
        extra["forwarded_to_generator"] = forward_indices  # 1-indexed
    if reference_library_used:
        # Forensik: welche Bibliotheks-Fotos als Stil-Anker mitgingen.
        extra["reference_library_refs"] = reference_library_used
    write_inputs_json(
        run_dir,
        scene_id=bundle.scene_id,
        scene_path=bundle.path,
        mode=mode,
        user_request=user_prompt,
        director_model=director_model,
        iteration_type=iteration_type,
        parent_run_id=parent_run_id,
        iteration_reason=iteration_reason,
        generator_model=generator_model,
        validator_model=validator_model,
        extra=extra or None,
    )

    # --- Phase 1 fertig: Bild ist persistiert -----------------------------
    # Bei defer_validation hier abbrechen und ohne Validator/Advisor
    # zurückgeben — das UI zeigt das Bild sofort, `finalize_iteration(run_dir)`
    # läuft danach nach. Usage bisher = nur Director + Generator.
    if defer_validation:
        _write_usage(run_dir, summarize(usage_sink))
        return IterationResult(
            run_dir=run_dir,
            run_id=run_id,
            generated_image_path=result_path,
            final_prompt=final_prompt,
            director_reply=director_reply,
            validator_reply=None,
            final_score=None,
            section_means={},
            iteration_type=iteration_type,
            parent_run_id=parent_run_id,
            director_recommendation=recommendation,
            director_recommendation_reason=recommendation_reason,
            reference_library_refs=reference_library_used,
        )

    # --- Phase 2: Validator + Score + Advisor (klassischer Ein-Pass) ------
    outcome = _validate_snapshot(
        run_dir, bundle=bundle, result_img=result_img, user_prompt=user_prompt,
        mode=mode, final_prompt=final_prompt, validator_language=validator_language,
        validator_model=validator_model, director_model=director_model,
        usage_sink=usage_sink,
    )
    _write_usage(run_dir, summarize(usage_sink))

    return IterationResult(
        run_dir=run_dir,
        run_id=run_id,
        generated_image_path=result_path,
        final_prompt=final_prompt,
        director_reply=director_reply,
        validator_reply=outcome.validator_reply,
        final_score=outcome.final_score,
        section_means=outcome.section_means,
        iteration_type=iteration_type,
        parent_run_id=parent_run_id,
        director_recommendation=recommendation,
        director_recommendation_reason=recommendation_reason,
        advisor_action=outcome.advisor_action,
        advisor_reason=outcome.advisor_reason,
        advisor_confidence=outcome.advisor_confidence,
        advisor_reply=outcome.advisor_reply,
        reference_library_refs=reference_library_used,
    )


def finalize_iteration(
    run_dir: Path | str,
    *,
    validator_model: Optional[str] = None,
    director_model: Optional[str] = None,
    validator_language: Optional[Language] = None,
) -> ValidationOutcome:
    """Phase 2 (Snapshot): Validator + Score + Advisor auf einem bereits
    generierten Run (result.png + inputs.json existieren schon).

    Rekonstruiert das Bundle + den Kontext aus den Run-Artefakten (scene_path,
    user_request, mode, Modelle, Sprache aus inputs.json). Merged die neue
    Usage (Validator+Advisor) mit der Phase-1-Usage (Director+Generator).
    """
    import json as _json
    run_dir = Path(run_dir)
    payload = _json.loads((run_dir / "inputs.json").read_text(encoding="utf-8"))
    bundle = load_scene_bundle(Path(payload["scene_path"]))
    result_img = Image.open(run_dir / "result.png").convert("RGB")
    final_prompt = (run_dir / "final_prompt.txt").read_text(encoding="utf-8")
    user_prompt = payload.get("user_request", "")
    mode: Mode = payload.get("mode", "A")  # type: ignore[assignment]
    vlang: Language = validator_language or payload.get("validator_language") or "de"  # type: ignore[assignment]
    vmodel = validator_model or payload.get("validator_model") or DEFAULT_VALIDATOR_MODEL
    dmodel = director_model or payload.get("director_model") or DEFAULT_DIRECTOR_MODEL

    usage_sink: list = []
    outcome = _validate_snapshot(
        run_dir, bundle=bundle, result_img=result_img, user_prompt=user_prompt,
        mode=mode, final_prompt=final_prompt, validator_language=vlang,
        validator_model=vmodel, director_model=dmodel, usage_sink=usage_sink,
    )
    existing = (payload.get("usage") or {}).get("records") or []
    _write_usage(run_dir, summarize(existing + usage_sink))
    return outcome


# ---------------------------------------------------------------------------
# Ad-hoc-Pipeline (single image + prompt, kein Validator, kein BIM)
# ---------------------------------------------------------------------------

ADHOC_SUBDIR = "_adhoc"


def _format_adhoc_geo_block(
    latitude_deg: Optional[float],
    longitude_deg: Optional[float],
    place_name: Optional[str],
) -> str:
    """Knapper GEO-KONTEXT-Block fuer ad-hoc — keine site_analysis hier."""
    if latitude_deg is None or longitude_deg is None:
        return ""
    lines = ["GEO-KONTEXT (User-eingegeben):"]
    lines.append(f"  Lat/Lon: {latitude_deg:.6f}, {longitude_deg:.6f}")
    if place_name:
        lines.append(f"  Ortsname: {place_name}")
    return "\n".join(lines) + "\n\n"


def call_director_adhoc(
    input_image: Image.Image,
    user_request: str,
    *,
    iteration_type: IterationType = "initial",
    previous_image: Optional[Image.Image] = None,
    previous_final_prompt: Optional[str] = None,
    previous_validator_reply: Optional[str] = None,
    user_feedback: Optional[str] = None,
    site_reference: Optional[Image.Image] = None,
    site_reference_marker: Optional[tuple[float, float]] = None,
    latitude_deg: Optional[float] = None,
    longitude_deg: Optional[float] = None,
    place_name: Optional[str] = None,
    mode: Mode = "A",
    language: Language = "de",
    attachments: Optional[list[Image.Image]] = None,
    attachment_description: Optional[str] = None,
    model: str = DEFAULT_DIRECTOR_MODEL,
    usage_sink: Optional[list] = None,
) -> str:
    """Director-Call im Ad-hoc-Modus.

    Vision-only, kein SceneBundle, kein meta.json. Director sieht das
    User-Bild (+ bei Iteration den vorigen Output und den vorigen Prompt)
    und schreibt einen EN-Prompt nach dem 7-Block-Schema.

    `mode` (A/B/C/D) steuert die Tonalität — anders als im Snapshot-Modus
    OHNE Rückfragen/Varianten (Ad-hoc ist immer One-Pass). Bei Modus D
    wird zusätzlich `_MODE_D_INSTRUCTION` eingespielt (Geometrie-Lock
    entfällt, kreative Form-Exploration erlaubt).

    Optional: ein Standort-Referenzbild (Drohne / Maps / Site-Foto) mit
    optionalem Pin (x_pct, y_pct), plus optionale Geo-Koordinaten +
    Ortsname. Pin wird zur Render-Zeit auf eine Kopie der Site-Reference
    eingebrannt.
    """
    system = load_director_adhoc_system()
    geo_block = _format_adhoc_geo_block(latitude_deg, longitude_deg, place_name)
    # Sprach-Direktive + Modus-Block: beide landen vor dem USER-WUNSCH
    # (in beiden Branches via {mode_block}), damit Sprache + Tonalität den
    # ganzen Prompt einfärben. Sprache lokalisiert nur den Vorspann — der
    # EN-Prompt im Code-Block bleibt Englisch.
    mode_block = "{}\n\nRENDER-MODUS: {} ({})\n{}\n".format(
        director_lang_directive(language),
        mode, MODE_LABELS[mode], _MODE_D_INSTRUCTION if mode == "D" else "",
    )

    # Site-Reference mit Marker einbrennen, falls beide vorhanden.
    site_ref_for_director: Optional[Image.Image] = None
    if site_reference is not None:
        site_ref_for_director = site_reference
        if site_reference_marker is not None:
            from render_director.utils import render_site_reference_marker
            x, y = site_reference_marker
            site_ref_for_director = render_site_reference_marker(site_reference, x, y)

    if iteration_type == "initial":
        site_anhang = ""
        if site_ref_for_director is not None:
            marker_hint = (
                " Der rote Pin auf diesem Bild markiert die geplante Gebaeude-"
                "Position — beschreibe Vegetation, Umgebung und Bergsilhouetten "
                "relativ dazu."
                if site_reference_marker is not None else ""
            )
            site_anhang = (
                "BILD 2: STANDORT-REFERENZ (Drohne / Google-Maps/Earth-"
                "Screenshot / Site-Visit-Foto). Wahrheit fuer Vegetation, "
                "Umgebung, Bergsilhouetten, Atmosphaere. KEINE Komposition "
                "uebernehmen.{}\n\n".format(marker_hint)
            )

        user_text = (
            "BILD 1: das vom User hochgeladene Input-Bild. Es ist die "
            "Geometrie- und Kompositions-Wahrheit fuer den finalen Render.\n\n"
            "{site_anhang}"
            "{geo_block}"
            "{mode_block}"
            "USER-WUNSCH:\n{user_request}\n\n"
            "Schreibe den finalen EN-Prompt im 7-Block-Schema, einleitend "
            "maximal 2 Saetze Kontext."
        ).format(
            site_anhang=site_anhang,
            geo_block=geo_block,
            mode_block=mode_block,
            user_request=user_request.strip() or "(kein Text angegeben — frei interpretieren)",
        )
        images = [to_anthropic_image_block(input_image)]
        if site_ref_for_director is not None:
            images.append(to_anthropic_image_block(site_ref_for_director))
    else:
        if previous_image is None or previous_final_prompt is None:
            raise ValueError(
                f"iteration_type={iteration_type} braucht previous_image + previous_final_prompt"
            )
        # Site-Ref-Index im Anhang anpassen (Bild 1+2 sind Input + Previous)
        site_anhang = ""
        if site_ref_for_director is not None:
            marker_hint = (
                " Roter Pin markiert geplante Gebaeude-Position."
                if site_reference_marker is not None else ""
            )
            site_anhang = (
                "BILD 3: STANDORT-REFERENZ (Drohne/Maps/Foto) — Wahrheit fuer "
                "Vegetation/Umgebung/Bergsilhouetten, KEINE Komposition.{}\n\n"
                .format(marker_hint)
            )
        user_text = (
            "BILD 1: das urspruengliche User-Input-Bild (Geometrie-Anker).\n"
            "BILD 2: das zuletzt generierte Result, das jetzt iteriert wird.\n\n"
            "{site_anhang}"
            "{geo_block}"
            "{mode_block}"
            "USER-WUNSCH (urspruenglich):\n{user_request}\n\n"
            "USER-FEEDBACK FUER DIESE ITERATION:\n{user_feedback}\n\n"
            "VORHERIGER FINAL-PROMPT (zur Referenz, nicht 1:1 wiederholen):\n"
            "```\n{prev_prompt}\n```\n\n"
            "{validator_block}"
            "ITERATIONS-TYP: {itype}\n"
            "{itype_hint}\n\n"
            "Schreibe den ueberarbeiteten EN-Prompt im 7-Block-Schema. "
            "Adressiere maximal zwei Top-Punkte aus dem Validator-Report + "
            "User-Feedback, halte den Rest stabil. Maximal 2 Saetze "
            "einleitender Kontext."
        ).format(
            site_anhang=site_anhang,
            geo_block=geo_block,
            mode_block=mode_block,
            user_request=user_request.strip() or "(kein Original-Text)",
            user_feedback=(user_feedback or "").strip() or "(kein Feedback — nur stilistische Re-Iteration)",
            prev_prompt=previous_final_prompt.strip(),
            validator_block=(
                "VALIDATOR-REPORT zum vorigen Result (adressiere die "
                "Top-Issues):\n{}\n\n".format(previous_validator_reply.strip())
                if previous_validator_reply and previous_validator_reply.strip()
                else ""
            ),
            itype=iteration_type,
            itype_hint=(
                "Regenerate-Hinweis: Generator startet wieder vom Input-Bild — "
                "Prompt darf freier sein, Atmosphaere wird neu erzeugt."
                if iteration_type == "regenerate"
                else "Refine-Hinweis: Generator bekommt das vorige Result als Basis — "
                "Prompt konservativer formulieren ('maintain existing atmosphere, only adjust …'), "
                "Geometrie-Drift-Risiko beachten."
            ),
        )
        images = [
            to_anthropic_image_block(input_image),
            to_anthropic_image_block(previous_image),
        ]
        if site_ref_for_director is not None:
            images.append(to_anthropic_image_block(site_ref_for_director))

    # User-Per-Turn-Attachments anhaengen (Gatekeeper via FORWARD_ATTACHMENTS,
    # identisch zum Snapshot-Pfad). start_index = Anzahl bisheriger Bilder + 1.
    attachments = (attachments or [])[:MAX_ATTACHMENTS_PER_TURN]
    if attachments:
        att_blocks, att_lines, _ = _attachment_blocks(
            attachments, start_index=len(images) + 1,
            description=attachment_description,
        )
        images.extend(att_blocks)
        suffix_parts = []
        if attachment_description:
            suffix_parts.append(
                "\nUSER-ATTACHMENT-BESCHREIBUNG:\n{}\n".format(attachment_description)
            )
        suffix_parts.append("\nATTACHMENT-BILDER:\n  " + "\n  ".join(att_lines) + "\n")
        suffix_parts.append(_FORWARD_ATTACHMENTS_INSTRUCTION.format(n_att=len(attachments)))
        user_text = user_text + "".join(suffix_parts)

    response = get_anthropic_client().messages.create(
        model=model,
        max_tokens=DIRECTOR_MAX_TOKENS,
        system=system,
        messages=[{
            "role": "user",
            "content": [*images, {"type": "text", "text": user_text}],
        }],
    )
    record_usage(usage_sink, role="director", provider="anthropic", model=model, response=response)
    return response.content[0].text


@dataclass
class AdhocIterationResult:
    """Ergebnis eines `run_iteration_adhoc`-Aufrufs.

    Seit 2026-07-02 mit vollem Validator+Advisor-Pfad wie `IterationResult`
    (1:1-Parität zum Snapshot-Flow, aber vision-only ohne BIM). Storage
    liegt in der Session-Struktur unter dem Ad-hoc-Root.
    """
    run_dir: Path
    run_id: str
    input_image_path: Path
    generated_image_path: Path
    final_prompt: str
    director_reply: str
    iteration_type: IterationType = "initial"
    parent_run_id: Optional[str] = None
    site_reference_path: Optional[Path] = None
    # Validator + Advisor (neu 2026-07-02). None = Validator/Advisor nicht
    # gelaufen oder Parser-Fail (defensiv, wie im Snapshot-Flow).
    validator_reply: Optional[str] = None
    final_score: Optional[float] = None
    section_means: dict[str, Optional[float]] = field(default_factory=dict)
    advisor_action: Optional[AdvisorAction] = None
    advisor_reason: Optional[str] = None
    advisor_confidence: Optional[AdvisorConfidence] = None
    advisor_reply: Optional[str] = None
    # Projekt-Tag-Referenzfotos (Dateinamen), die als Stil-Anker mitgingen.
    reference_library_refs: list[str] = field(default_factory=list)


def _count_adhoc_runs(generations_root: Path) -> int:
    adhoc_dir = generations_root / ADHOC_SUBDIR
    if not adhoc_dir.is_dir():
        return 0
    return sum(1 for p in adhoc_dir.iterdir() if p.is_dir())


def _make_adhoc_run_dir(
    generations_root: Path,
    run_index: int,
    label: Optional[str] = None,
) -> Path:
    """Legt einen Ad-hoc-Run-Ordner an: `<root>/_adhoc/<stamp>_adhoc_run<NN>[_<label>]/`."""
    from datetime import datetime as _dt
    stamp = _dt.now().strftime("%Y-%m-%d_%H%M%S")
    name = f"{stamp}_adhoc_run{run_index:02d}"
    if label:
        name = f"{name}_{label}"
    parent = generations_root / ADHOC_SUBDIR
    parent.mkdir(parents=True, exist_ok=True)
    run_dir = parent / name
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _slugify_adhoc(text: str, maxlen: int = 32) -> str:
    """Kurzer, dateisystem-sicherer Slug aus dem User-Prompt fuer den
    Session-Ordnernamen (browsbar im Explorer)."""
    import re
    s = re.sub(r"[^\w\s-]", "", (text or "").strip().lower(), flags=re.UNICODE)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:maxlen].rstrip("-") or "adhoc"


def run_iteration_adhoc(
    input_image: Image.Image,
    user_prompt: str,
    *,
    iteration_type: IterationType = "initial",
    parent_run_id: Optional[str] = None,
    previous_image: Optional[Image.Image] = None,
    previous_final_prompt: Optional[str] = None,
    previous_validator_reply: Optional[str] = None,
    user_feedback: Optional[str] = None,
    attachments: Optional[list[Image.Image]] = None,
    attachment_description: Optional[str] = None,
    mask: Optional[Image.Image] = None,
    site_reference: Optional[Image.Image] = None,
    site_reference_marker: Optional[tuple[float, float]] = None,
    latitude_deg: Optional[float] = None,
    longitude_deg: Optional[float] = None,
    place_name: Optional[str] = None,
    project_tag: Optional[str] = None,
    reference_library_root: Optional[Path | str] = None,
    mode: Mode = "A",
    session_dir: Optional[Path | str] = None,
    generations_root: Optional[Path | str] = None,
    director_model: str = DEFAULT_DIRECTOR_MODEL,
    generator_model: str = DEFAULT_GENERATOR_MODEL,
    generator_thinking_budget: Optional[int] = None,
    validator_model: str = DEFAULT_VALIDATOR_MODEL,
    validator_language: Language = "de",
    defer_validation: bool = False,
) -> AdhocIterationResult:
    """Ad-hoc-Iteration: Director → Generator → Validator → Advisor.

    `defer_validation=True` (Zwei-Phasen-UI): stoppt nach dem Persistieren des
    Bildes und gibt ein Ergebnis OHNE Validator/Score/Advisor zurück; der
    Caller lässt `finalize_adhoc_run(run_dir)` nachlaufen.

    Seit 2026-07-02 mit vollem Validator+Advisor-Pfad (1:1-Parität zum
    Snapshot-`run_iteration`), aber vision-only ohne BIM: das User-Input-
    Bild ist der Geometrie-Anker für den Validator (`call_validator_adhoc`).

    Pipeline-Pfad:
      1. `call_director_adhoc(...)` → final_prompt (bekommt bei Iteration
         zusätzlich `previous_validator_reply` wie der Snapshot-Director)
      2. `call_generator(prompt, primary_image=<input oder previous>)` → result
      3. Persistenz + `inputs.json`
      4. `call_validator_adhoc(...)` → `validator_reply.md` + Score-Block in
         inputs.json (`add_score_summary_to_inputs`, VALIDATOR_SCORE_SCHEMA)
      5. `call_director_advisor(input_image, ...)` → `advisor_reply.md`
         (defensiv: Fehler brechen den Run nicht ab)

    `primary_image` fuer den Generator:
    - initial / regenerate → das Input-Bild (Geometrie-Anker, Vorlage)
    - refine               → das previous_image (Edit-Pass auf vorigem Result)
    - inpaint              → das previous_image + `mask` (nur gpt-image)

    Inpaint-Sonderfall: der Director wird übersprungen (MVP). Der User-Text
    (`user_feedback`, sonst `user_prompt`) beschreibt direkt, was in die
    maskierte Region kommt und wird als `final_prompt` an den Generator
    gegeben. Validator + Advisor laufen unverändert weiter (bewerten das
    Gesamtbild). Inpaint braucht zwingend `mask` + ein gpt-image-Backend.
    """
    import json as _json

    if generations_root is None:
        generations_root = GENERATIONS_DIR
    generations_root = Path(generations_root)

    if iteration_type == "initial" and parent_run_id is not None:
        raise ValueError("initial-Ad-hoc-Run darf keine parent_run_id haben")
    if iteration_type != "initial" and parent_run_id is None:
        raise ValueError(f"{iteration_type}-Ad-hoc-Run braucht eine parent_run_id")
    if iteration_type != "initial" and (previous_image is None or previous_final_prompt is None):
        raise ValueError(
            f"{iteration_type}-Ad-hoc-Run braucht previous_image + previous_final_prompt"
        )
    if iteration_type == "inpaint":
        if mask is None:
            raise ValueError("inpaint-Ad-hoc-Run braucht eine Maske (mask)")
        if not generator_model.startswith("gpt-image"):
            raise ValueError(
                "Inpaint ist nur mit dem gpt-image-1-Backend verfügbar "
                "(native Masken-Unterstützung) — bitte Generator 'GPT' wählen."
            )

    attachments = (attachments or [])[:MAX_ATTACHMENTS_PER_TURN]

    # Cost-Logging (wie Snapshot-Flow): Token-Usage aller 4 Calls sammeln,
    # am Ende als `usage`-Block in inputs.json.
    usage_sink: list = []

    # --- Director ---------------------------------------------------------
    reference_library_used: list[str] = []
    if iteration_type == "inpaint":
        # MVP-Sonderpfad: Director wird übersprungen. Der User-Text
        # beschreibt direkt, was in die maskierte Region kommt und wird 1:1
        # als final_prompt an den Generator gegeben. Kein 7-Block-Prompt,
        # kein FORWARD_ATTACHMENTS-Gatekeeper (die Maske lokalisiert den
        # Eingriff). Der director_reply ist ein Platzhalter für die Artefakte.
        inpaint_instruction = (user_feedback or user_prompt or "").strip()
        if not inpaint_instruction:
            raise ValueError(
                "inpaint-Ad-hoc-Run braucht eine Beschreibung, was in die "
                "markierte Region kommt (user_feedback bzw. user_prompt)."
            )
        final_prompt = inpaint_instruction
        director_reply = (
            "_(Inpaint — Director übersprungen. User-Text wurde direkt als "
            "Generator-Prompt verwendet:)_\n\n```\n{}\n```".format(inpaint_instruction)
        )
        forwarded = []
    else:
        director_reply = call_director_adhoc(
            input_image,
            user_prompt,
            iteration_type=iteration_type,
            previous_image=previous_image,
            previous_final_prompt=previous_final_prompt,
            previous_validator_reply=previous_validator_reply,
            user_feedback=user_feedback,
            attachments=attachments,
            attachment_description=attachment_description,
            site_reference=site_reference,
            site_reference_marker=site_reference_marker,
            latitude_deg=latitude_deg,
            longitude_deg=longitude_deg,
            place_name=place_name,
            mode=mode,
            language=validator_language,
            model=director_model,
            usage_sink=usage_sink,
        )
        final_prompt = extract_final_prompt(director_reply)
        if not final_prompt:
            raise DirectorNoPromptError(director_reply)

        # Gatekeeper: Director entscheidet via FORWARD_ATTACHMENTS, welche der
        # User-Attachments an den Generator gehen (identisch zum Snapshot-Pfad).
        forward_indices = parse_forward_attachments(director_reply) if attachments else []
        forwarded = [
            attachments[i - 1] for i in forward_indices
            if 1 <= i <= len(attachments)
        ]

        # --- Tier-0-Projekt-Tag-Referenzen (Augment) ----------------------
        # Passt der Projekt-Tag zu einem Ordner der Bibliothek, gehen ein paar
        # Fotos dieses Projekts als Stil-Anker an den Generator. Ohne Projekt-
        # Match bleibt es leer (der Sofort-Render hat keine Material-Metadaten,
        # auf die sonst zurückgefallen würde).
        if project_tag and iteration_type != "inpaint":
            reference_library_used = _augment_with_reference_library(
                forwarded, k=2, root=reference_library_root, project_query=project_tag,
            )

    # --- Generator --------------------------------------------------------
    # refine/inpaint → vorheriges Result als Basis. Sonst Input-Bild
    # (initial+regenerate). Die Maske geht nur beim Inpaint mit.
    primary = previous_image if iteration_type in ("refine", "inpaint") else input_image
    result_img = call_generator(
        final_prompt,
        primary_image=primary,
        depth=None,
        forwarded_attachments=forwarded or None,
        mask=mask if iteration_type == "inpaint" else None,
        model=generator_model,
        thinking_budget=generator_thinking_budget,
        usage_sink=usage_sink,
    )

    # --- Persist ----------------------------------------------------------
    # Session-Struktur (User-Wunsch 2026-07-02): <adhoc_root>/<datum_zeit_
    # slug>/<run>/. Initial legt die Session an (Slug aus dem Prompt);
    # Iterationen landen als weiterer Run in derselben Session
    # (session_dir kommt vom Caller = previous_run_dir.parent).
    from datetime import datetime as _dt3
    _now = _dt3.now()
    if session_dir is None:
        session_dir = generations_root / "{}_{}".format(
            _now.strftime("%Y-%m-%d_%H%M"), _slugify_adhoc(user_prompt),
        )
    else:
        session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    run_index = sum(1 for p in session_dir.iterdir() if p.is_dir()) + 1
    _lbl = label_from_model(generator_model)
    run_name = "{}_run{:02d}".format(_now.strftime("%Y-%m-%d_%H%M%S"), run_index)
    if _lbl:
        run_name = "{}_{}".format(run_name, _lbl)
    run_dir = session_dir / run_name
    run_dir.mkdir(parents=True, exist_ok=False)
    run_id = run_dir.name

    # Input-Bild in jedem Run-Dir mitspeichern (self-contained fuer Reload-
    # Loader, kostet ein paar Hundert KB pro Run — vertretbar).
    input_path = run_dir / "input.png"
    input_image.save(input_path)
    if previous_image is not None:
        previous_image.save(run_dir / "previous.png")
    # Inpaint-Maske forensisch mitspeichern (RGBA, transparent = neu gemalt).
    if mask is not None:
        mask.convert("RGBA").save(run_dir / "mask.png")
    site_ref_path: Optional[Path] = None
    if site_reference is not None:
        site_ref_path = run_dir / "site_reference.png"
        site_reference.save(site_ref_path)

    attachment_filenames: list[str] = []
    for att_i, att in enumerate(attachments, start=1):
        att_filename = "attachment_{:02d}.png".format(att_i)
        att.save(run_dir / att_filename)
        attachment_filenames.append(att_filename)

    result_path = run_dir / "result.png"
    result_img.save(result_path)
    (run_dir / "final_prompt.txt").write_text(final_prompt, encoding="utf-8")
    (run_dir / "director_reply.md").write_text(director_reply, encoding="utf-8")

    from datetime import datetime as _dt2
    inputs_payload: dict = {
        "timestamp": _dt2.now().isoformat(timespec="seconds"),
        # `mode` = gewählter Render-Modus (A/B/C/D), konsistent zum Snapshot-
        # Flow. `flow` markiert die Herkunft (früher trug `mode` den Wert
        # "adhoc" — der Loader normalisiert Altdaten auf "A").
        "flow": "adhoc",
        "mode": mode,
        "validator_language": validator_language,
        "user_request": user_prompt,
        "iteration": {
            "type": iteration_type,
            "parent_run_id": parent_run_id,
        },
        "director_model": director_model,
        "generator_model": generator_model,
    }
    if user_feedback:
        inputs_payload["user_feedback"] = user_feedback
    if generator_thinking_budget is not None:
        inputs_payload["generator_thinking_budget"] = generator_thinking_budget
    if mask is not None:
        inputs_payload["mask_filename"] = "mask.png"
    if site_reference is not None:
        inputs_payload["site_reference_filename"] = "site_reference.png"
    if attachment_filenames:
        inputs_payload["attachments"] = attachment_filenames
        if attachment_description:
            inputs_payload["attachment_description"] = attachment_description
    if site_reference_marker is not None:
        inputs_payload["site_reference_marker"] = {
            "x_pct": round(float(site_reference_marker[0]), 4),
            "y_pct": round(float(site_reference_marker[1]), 4),
        }
    if latitude_deg is not None or longitude_deg is not None or place_name:
        inputs_payload["site_location"] = {
            "latitude_deg": latitude_deg,
            "longitude_deg": longitude_deg,
            "place_name": place_name or None,
        }
    if project_tag and project_tag.strip():
        inputs_payload["project_tag"] = project_tag.strip()
    if reference_library_used:
        # Forensik: welche Bibliotheks-Fotos als Stil-Anker mitgingen.
        inputs_payload["reference_library_refs"] = reference_library_used
    (run_dir / "inputs.json").write_text(
        _json.dumps(inputs_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # --- Phase 1 fertig: Bild ist persistiert -----------------------------
    # Bei defer_validation ohne Validator/Advisor zurückgeben (UI zeigt Bild
    # sofort, `finalize_adhoc_run(run_dir)` läuft danach nach). Usage bisher =
    # nur Director + Generator.
    if defer_validation:
        _write_usage(run_dir, summarize(usage_sink))
        return AdhocIterationResult(
            run_dir=run_dir,
            run_id=run_id,
            input_image_path=input_path,
            generated_image_path=result_path,
            final_prompt=final_prompt,
            director_reply=director_reply,
            iteration_type=iteration_type,
            parent_run_id=parent_run_id,
            site_reference_path=site_ref_path,
            validator_reply=None,
            final_score=None,
            section_means={},
            reference_library_refs=reference_library_used,
        )

    # --- Phase 2: Validator + Score + Advisor (klassischer Ein-Pass) ------
    outcome = _validate_adhoc(
        run_dir, input_image=input_image, result_img=result_img,
        user_prompt=user_prompt, mode=mode, final_prompt=final_prompt,
        validator_language=validator_language, validator_model=validator_model,
        director_model=director_model, usage_sink=usage_sink,
        iteration_type=iteration_type,
    )
    _write_usage(run_dir, summarize(usage_sink))

    return AdhocIterationResult(
        run_dir=run_dir,
        run_id=run_id,
        input_image_path=input_path,
        generated_image_path=result_path,
        final_prompt=final_prompt,
        director_reply=director_reply,
        iteration_type=iteration_type,
        parent_run_id=parent_run_id,
        site_reference_path=site_ref_path,
        validator_reply=outcome.validator_reply,
        final_score=outcome.final_score,
        section_means=outcome.section_means,
        advisor_action=outcome.advisor_action,
        advisor_reason=outcome.advisor_reason,
        advisor_confidence=outcome.advisor_confidence,
        advisor_reply=outcome.advisor_reply,
        reference_library_refs=reference_library_used,
    )


def finalize_adhoc_run(
    run_dir: Path | str,
    *,
    validator_model: Optional[str] = None,
    director_model: Optional[str] = None,
    validator_language: Optional[Language] = None,
) -> ValidationOutcome:
    """Phase 2 (Ad-hoc): Validator + Score + Advisor auf einem bereits
    generierten Run. Anker = `input.png` (vision-only, kein BIM). Kontext +
    Modelle kommen aus inputs.json; Usage wird mit der Phase-1-Usage gemerged.
    """
    import json as _json
    run_dir = Path(run_dir)
    payload = _json.loads((run_dir / "inputs.json").read_text(encoding="utf-8"))
    input_image = Image.open(run_dir / "input.png").convert("RGB")
    result_img = Image.open(run_dir / "result.png").convert("RGB")
    final_prompt = (run_dir / "final_prompt.txt").read_text(encoding="utf-8")
    user_prompt = payload.get("user_request", "")
    mode: Mode = payload.get("mode", "A")  # type: ignore[assignment]
    vlang: Language = validator_language or payload.get("validator_language") or "de"  # type: ignore[assignment]
    vmodel = validator_model or payload.get("validator_model") or DEFAULT_VALIDATOR_MODEL
    dmodel = director_model or payload.get("director_model") or DEFAULT_DIRECTOR_MODEL
    itype: IterationType = (payload.get("iteration") or {}).get("type", "initial")  # type: ignore[assignment]

    usage_sink: list = []
    outcome = _validate_adhoc(
        run_dir, input_image=input_image, result_img=result_img,
        user_prompt=user_prompt, mode=mode, final_prompt=final_prompt,
        validator_language=vlang, validator_model=vmodel, director_model=dmodel,
        usage_sink=usage_sink, iteration_type=itype,
    )
    existing = (payload.get("usage") or {}).get("records") or []
    _write_usage(run_dir, summarize(existing + usage_sink))
    return outcome


# ---------------------------------------------------------------------------
# CLI: python -m render_director.pipeline <snapshot_dir> "<user_prompt>"
# ---------------------------------------------------------------------------

def _cli_generations_root(arg: Optional[str] = None) -> Optional[Path]:
    """Ziel-Wurzel für CLI-Runs: Argument vor `RENDER_GENERATIONS_ROOT` vor Default.

    Ohne beides bleibt es bei `GENERATIONS_DIR` (None wird in `run_iteration`
    zum Default aufgelöst). So respektiert die CLI dieselbe Env-Variable wie
    die Webapp, statt immer ins Repo zu schreiben.
    """
    import os

    from render_director.clients import _ensure_dotenv

    _ensure_dotenv()
    value = (arg or os.environ.get("RENDER_GENERATIONS_ROOT", "")).strip()
    return Path(value) if value else None


def _cli() -> None:
    import argparse
    import sys

    # Windows-Default-Konsole ist cp1252 und frisst keine Unicode-Pfeile.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    p = argparse.ArgumentParser(
        prog="render_director.pipeline",
        description="Eine vollständige Render-Iteration als CLI-Smoke-Test.",
    )
    p.add_argument("snapshot_dir", type=Path)
    p.add_argument("user_prompt", type=str)
    p.add_argument("--mode", default="A", choices=["A", "B", "C", "D"])
    p.add_argument("--phase-label", default=None)
    p.add_argument(
        "--generations-root", default=None,
        help="Wurzelordner für Run-Ordner. Default: RENDER_GENERATIONS_ROOT, sonst data/generations.",
    )
    p.add_argument("--language", default="de", choices=["de", "it"])
    args = p.parse_args()

    print(f"→ Snapshot:    {args.snapshot_dir}")
    print(f"→ User-Prompt: {args.user_prompt!r}")
    print(f"→ Modus:       {args.mode}")
    print()
    res = run_iteration(
        args.snapshot_dir,
        args.user_prompt,
        mode=args.mode,
        phase_label=args.phase_label,
        generations_root=_cli_generations_root(args.generations_root),
        validator_language=args.language,
    )
    print(f"→ Run-ID:      {res.run_id}")
    print(f"→ Run-Dir:     {res.run_dir}")
    print(f"→ Bild:        {res.generated_image_path}")
    print(f"→ Final-Score: {res.final_score!r}")
    print(f"→ Section-Means: {res.section_means}")


if __name__ == "__main__":
    _cli()
