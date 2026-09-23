"""FastAPI-Hauptmodul. Wird gestartet via:

    .venv\\Scripts\\python.exe -m uvicorn app.main:app --reload --port 8765

Default-Port 8765 (Phase-4-Konvention, von Chrome --app aus geöffnet).
Reload-Mode nur für lokale Entwicklung — pyRevit-Trigger startet
später ohne --reload.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from io import BytesIO

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

from app.config import settings
from app.models import IterationRequest, IterationResponse, SnapshotDetail, SnapshotSummary
from app.services import (
    _derive_project_display_name,
    _resolve_snapshot_root,
    aggregate_usage,
    delete_run,
    delete_site_reference,
    execute_adhoc_iteration,
    execute_iteration,
    finalize_adhoc_validation,
    finalize_snapshot_validation,
    get_adhoc_root,
    get_snapshot_detail,
    list_adhoc_iterations,
    list_adhoc_tags,
    list_iterations_for_view,
    list_recent_projects,
    list_snapshots,
    record_recent_project,
    save_site_reference,
    save_site_reference_marker,
    update_site_location,
)

# Lazy-Load: initial nur die letzten N Iterationen rendern. Bei klassischem
# Snapshot-View mit 30+ Runs spart das massiv HTML-Bytes; alte Runs holt
# der User bei Bedarf via "Ältere anzeigen"-Button via htmx-Roundtrip.
ITERATIONS_PAGE_SIZE = 10
from render_director.clients import use_mock_providers
from render_director.pipeline import DirectorNoPromptError

from app.i18n import (
    UI_LANGS,
    DEFAULT_UI_LANG,
    classify_provider_budget_error,
    translate,
    ui_lang_from_request,
)


def _i18n_context(request: Request) -> dict:
    """Context-Processor: injiziert die UI-Sprache + `t("key")` in JEDES
    Template (auch htmx-Fragmente), ohne dass jeder Endpoint es mitgeben muss."""
    lang = ui_lang_from_request(request)
    return {
        "ui_lang": lang,
        "t": lambda key: translate(key, lang),
        "mock_mode": use_mock_providers(),
    }


TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates"),
    context_processors=[_i18n_context],
)

app = FastAPI(
    title="Render Director",
    description="Lokale Webapp für conversational Render-Iteration (Phase 4 MVP).",
    version="0.1.0",
)


@app.on_event("startup")
def _ensure_paths() -> None:
    """Bei Start kurz die konfigurierten Pfade loggen — Default vs Env-Override."""
    print(f"[render-director] snapshot_root    = {settings.snapshot_root}")
    print(f"[render-director] generations_root = {settings.generations_root}")
    print(f"[render-director] phase_label      = {settings.phase_label}")
    if use_mock_providers():
        print("[render-director] RENDER_MOCK_PROVIDERS aktiv — keine echten API-Calls")


# --- Static-Files ----------------------------------------------------------
# Generations bleibt zentral (settings.generations_root) — kann gemountet
# bleiben. App-eigene Assets analog. Snapshot-Files dagegen brauchen
# project_dir-aware Resolving und sind über den /files/snapshot-file/-
# Endpoint unten implementiert. StaticFiles prüft den Ordner schon beim
# Mounten, daher hier (nicht erst im Startup-Event) anlegen.
settings.generations_root.mkdir(parents=True, exist_ok=True)
app.mount(
    "/files/runs",
    StaticFiles(directory=settings.generations_root),
    name="run-files",
)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="app-static",
)


@app.get("/files/snapshot-file/{view}/{filename}")
def serve_snapshot_file(
    view: str, filename: str, project_dir: Optional[str] = None,
) -> FileResponse:
    """Custom File-Server für Snapshot-Bilder (Beauty, Depth, Reference).

    Mit `?project_dir=<Pfad>` werden Dateien aus dem Projekt-Snapshot-Root
    geladen statt aus `settings.snapshot_root`. Path-Traversal-Schutz:
    der finale Pfad muss unter dem effektiven Root liegen.
    """
    root = _resolve_snapshot_root(project_dir)
    candidate = (root / view / filename).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise HTTPException(403, "Pfad-Traversal nicht erlaubt")
    if not candidate.is_file():
        raise HTTPException(404, f"Datei nicht gefunden: {view}/{filename}")
    return FileResponse(candidate)


@app.get("/files/adhoc-file/{filepath:path}")
def serve_adhoc_file(filepath: str) -> FileResponse:
    """File-Server für Ad-hoc-Runs. Diese liegen im konfigurierten
    adhoc_root (Default Documents\\Render Director), außerhalb von
    generations_root — daher eigener Endpoint mit Path-Traversal-Schutz."""
    root = get_adhoc_root().resolve()
    candidate = (root / filepath).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(403, "Pfad-Traversal nicht erlaubt")
    if not candidate.is_file():
        raise HTTPException(404, f"Datei nicht gefunden: {filepath}")
    return FileResponse(candidate)


@app.post("/ui/adhoc/change-folder")
def ui_adhoc_change_folder() -> RedirectResponse:
    """Öffnet den nativen Ordner-Dialog (lokaler Server, User-Session) und
    speichert die Wahl als neuen Ad-hoc-Zielordner. Abbruch → unverändert."""
    from app.services import pick_folder_native, set_adhoc_root
    picked = pick_folder_native(initial=str(get_adhoc_root()))
    if picked:
        try:
            set_adhoc_root(picked)
        except ValueError:
            # z.B. Laufwerks-Root gewählt → ignorieren, alter Ordner bleibt.
            pass
    return RedirectResponse("/adhoc", status_code=303)


# --- UI: Root → Sidebar mit Snapshot-Liste --------------------------------
@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    snapshot: Optional[str] = None,
    project_dir: Optional[str] = None,
) -> HTMLResponse:
    """Root-Route der Chat-UI. Liefert Layout-Shell + Sidebar-Listing
    server-side gerendert.

    Query-Params:
    - `snapshot=Scene_06` → Detail wird direkt mit-gerendert
    - `project_dir=<Pfad>` → Sidebar zeigt nur Views aus diesem Projekt
      (kommt vom pyRevit-Render-Tool-Button)
    """
    record_recent_project(project_dir)
    detail = get_snapshot_detail(snapshot, project_dir) if snapshot else None
    effective_root = _resolve_snapshot_root(project_dir)
    past_iterations: list = []
    has_more_older = False
    if detail:
        past_iterations, has_more_older = list_iterations_for_view(
            snapshot, project_dir, limit=ITERATIONS_PAGE_SIZE,
        )
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "snapshots": list_snapshots(project_dir),
            "snapshot_root": str(effective_root),
            "active_snapshot": snapshot if detail else None,
            "detail": detail,
            "project_dir": project_dir,
            "past_iterations": past_iterations,
            "has_more_older": has_more_older,
            "active_tab": "snapshots",
            "recent_projects": list_recent_projects(),
            "project_display_name": (
                _derive_project_display_name(project_dir) if project_dir else None
            ),
        },
    )


@app.post("/ui/iterate", response_class=HTMLResponse)
async def ui_iterate(
    request: Request,
    snapshot: str = Form(...),
    user_prompt: str = Form(...),
    mode: Literal["A", "B", "C", "D"] = Form("A"),
    validator_language: Literal["de", "it"] = Form("de"),
    iteration_type: Literal["initial", "regenerate", "refine"] = Form("initial"),
    parent_run_id: Optional[str] = Form(None),
    previous_run_dir: Optional[str] = Form(None),
    user_feedback: Optional[str] = Form(None),
    generator_model: str = Form("gemini-2.5-flash-image"),
    generator_thinking_budget: Optional[int] = Form(None),
    use_reference_library: bool = Form(False),
    reference_project_tag: Optional[str] = Form(None),
    attachments: list[UploadFile] = File(default=[]),
    attachment_description: Optional[str] = Form(None),
    project_dir: Optional[str] = Form(None),
) -> HTMLResponse:
    """HTML-Form-Endpoint für die Chat-UI. Liefert eine `chat_iteration`-
    Bubble als Fragment zurück, die htmx ans #chat-list anhängt.

    Existiert parallel zum JSON-`/iterate` — JSON bleibt für Swagger/
    curl/Tests, dieses hier wird vom Form-Submit in der Chat-UI
    aufgerufen. Beide rufen dieselbe `execute_iteration` darunter.

    Bei Iterationen (regenerate/refine) wird die Textarea als
    `user_feedback` interpretiert, der ursprüngliche `user_prompt`
    kommt als verstecktes Feld aus der vorherigen Bubble weitergereicht.
    """
    # UploadFile → PIL.Image, leere Uploads (kein File ausgewählt) skippen.
    attachment_images: list[Image.Image] = []
    for upload in attachments or []:
        if not upload.filename:
            continue
        data = await upload.read()
        if not data:
            continue
        try:
            img = Image.open(BytesIO(data)).convert("RGB")
            attachment_images.append(img)
        except Exception as e:
            raise HTTPException(
                400, f"Attachment {upload.filename!r} konnte nicht als Bild "
                f"geladen werden: {e}",
            )

    req = IterationRequest(
        snapshot=snapshot,
        user_prompt=user_prompt,
        mode=mode,
        iteration_type=iteration_type,
        parent_run_id=parent_run_id,
        previous_run_dir=Path(previous_run_dir) if previous_run_dir else None,
        user_feedback=user_feedback,
        validator_language=validator_language,
        generator_model=generator_model,
        generator_thinking_budget=generator_thinking_budget,
        use_reference_library=use_reference_library,
        reference_project_tag=(reference_project_tag or None),
        attachment_images=attachment_images,
        attachment_description=attachment_description,
        project_dir=project_dir,
    )
    try:
        # Zwei-Phasen: Phase 1 (Director+Generator) rendert das Bild sofort,
        # der Validator/Advisor läuft danach via /ui/iterate/validate nach.
        result = await run_in_threadpool(execute_iteration, req, defer_validation=True)
    except DirectorNoPromptError as e:
        # Zwei Untertypen unterscheiden — Truncation oder echte Rückfrage.
        if e.truncated:
            title = "Director-Reply wurde abgeschnitten (Token-Limit)"
            hint = (
                "Der Director hat den Prompt angefangen, aber das Token-"
                "Budget hat nicht gereicht. Versuch's nochmal — DIRECTOR_MAX_"
                "TOKENS wurde schon auf 8192 hochgesetzt. Wenn das immer "
                "noch zu wenig ist, sag Bescheid, ich heb's weiter."
            )
        else:
            title = "Director hat eine Rückfrage statt eines Prompts geliefert"
            hint = (
                "Das passiert oft in Modus B (Wettbewerb), oder wenn der "
                "Render-Wunsch zu offen ist. Schärfe Tageszeit + Stimmung + "
                "ggf. konkrete Geometrie-Anker und versuch's nochmal oben "
                "im Initial-Form."
            )
        return TEMPLATES.TemplateResponse(
            request,
            "partials/error_bubble.html",
            {
                "user_prompt": user_feedback or user_prompt,
                "director_reply": e.director_reply,
                "title": title,
                "hint": hint,
            },
        )
    except FileNotFoundError as e:
        # Bewusst 200, damit htmx die Bubble swappt (kein Fehler aus
        # Nutzersicht, sondern Teil des Chat-Flows).
        return TEMPLATES.TemplateResponse(
            request,
            "partials/error_bubble.html",
            {
                "user_prompt": user_feedback or user_prompt,
                "title": "Snapshot oder Datei nicht gefunden",
                "error_detail": str(e),
            },
        )
    except ValueError as e:
        return TEMPLATES.TemplateResponse(
            request,
            "partials/error_bubble.html",
            {
                "user_prompt": user_feedback or user_prompt,
                "title": "Ungültige Iteration-Anfrage",
                "error_detail": str(e),
            },
        )
    except Exception as e:  # noqa: BLE001 — nur Budget-/Quota-Fehler abfangen
        bubble = classify_provider_budget_error(e, ui_lang_from_request(request))
        if bubble is None:
            raise  # kein Budget-Fehler → normal weiterreichen (500)
        return TEMPLATES.TemplateResponse(
            request,
            "partials/error_bubble.html",
            {
                "user_prompt": user_feedback or user_prompt,
                "title": bubble["title"],
                "hint": bubble["hint"],
                "error_detail": str(e)[:300],
            },
        )

    rel = result.run_dir.relative_to(settings.generations_root).as_posix()
    result_url = f"/files/runs/{rel}/result.png"
    return TEMPLATES.TemplateResponse(
        request,
        "partials/chat_iteration.html",
        {
            # User-Bubble zeigt: bei initial den user_prompt, bei iteration
            # das feedback (was der User „diesmal anders" wollte).
            "user_prompt": user_prompt,
            "user_feedback": user_feedback,
            "result": result,
            "result_url": result_url,
            # Forward-State für die nächsten Iteration-Buttons der NEUEN Bubble.
            "snapshot": snapshot,
            "mode": mode,
            "validator_language": validator_language,
            "generator_model": generator_model,
            "generator_thinking_budget": generator_thinking_budget,
            "use_reference_library": use_reference_library,
            "reference_project_tag": reference_project_tag or "",
            "project_dir": project_dir,
            # Phase-1-Bubble: Score-Block wird per /ui/iterate/validate nachgeladen.
            "pending_validation": True,
        },
    )


@app.post("/ui/iterate/validate", response_class=HTMLResponse)
async def ui_iterate_validate(
    request: Request,
    run_dir: str = Form(...),
) -> HTMLResponse:
    """Phase 2 (Snapshot): lässt Validator + Advisor auf dem bereits
    generierten Run nachlaufen und liefert den Score-Block als Fragment, das
    den Platzhalter in der Bubble ersetzt (htmx hx-swap=outerHTML)."""
    try:
        outcome = await run_in_threadpool(finalize_snapshot_validation, run_dir)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(
            '<div class="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 '
            'text-xs text-amber-800">Bewertung fehlgeschlagen: {} — das Bild oben '
            'ist trotzdem gespeichert.</div>'.format(str(e)[:200])
        )
    return TEMPLATES.TemplateResponse(
        request,
        "partials/iteration_scoreblock.html",
        {"result": {
            "final_score": outcome.final_score,
            "section_means": outcome.section_means,
            "advisor_action": outcome.advisor_action,
            "advisor_reason": outcome.advisor_reason,
            "advisor_confidence": outcome.advisor_confidence,
            "validator_reply": outcome.validator_reply,
        }},
    )


@app.get("/ui/snapshot/{view}", response_class=HTMLResponse)
def ui_snapshot_detail(
    request: Request, view: str, project_dir: Optional[str] = None,
) -> HTMLResponse:
    """HTML-Fragment für die Snapshot-Detail-Ansicht (htmx-Target #main-pane).

    Mit `?project_dir=<Pfad>` wird der Snapshot aus dem Projekt-Root
    geladen, sodass pyRevit-getriggerte Sessions ihre eigenen Views sehen.
    """
    detail = get_snapshot_detail(view, project_dir)
    if detail is None:
        raise HTTPException(404, f"Snapshot {view!r} nicht gefunden")
    # WICHTIG: list_iterations_for_view liefert ein Tuple (Liste, has_more_older)
    # — muss entpackt werden. Vorher wurde das ganze Tuple an das Template
    # gegeben, wodurch der Snapshot-History-Verlauf beim Klick auf einen
    # Snapshot nicht rendert (Bug: „bei Snapshots keine alte History").
    past_iterations, has_more_older = list_iterations_for_view(
        view, project_dir, limit=ITERATIONS_PAGE_SIZE,
    )
    return TEMPLATES.TemplateResponse(
        request,
        "partials/snapshot_detail.html",
        {
            "detail": detail,
            "project_dir": project_dir,
            "past_iterations": past_iterations,
            "has_more_older": has_more_older,
        },
    )


# --- Site-Reference + Koordinaten (Drone/Maps-Foto + Lat/Lon) -------------
def _render_snapshot_detail_fragment(
    request: Request, view: str, project_dir: Optional[str],
) -> HTMLResponse:
    """Hilfsfunktion: rendert das snapshot_detail.html-Fragment fuer htmx.

    Wird von den Site-Reference/Coords-Endpoints genutzt — alle drei haben
    htmx-Target=#main-pane und tauschen das ganze Snapshot-Detail aus.
    """
    detail = get_snapshot_detail(view, project_dir)
    if detail is None:
        raise HTTPException(404, f"Snapshot {view!r} nicht gefunden")
    past_iterations, has_more_older = list_iterations_for_view(
        view, project_dir, limit=ITERATIONS_PAGE_SIZE,
    )
    return TEMPLATES.TemplateResponse(
        request,
        "partials/snapshot_detail.html",
        {
            "detail": detail,
            "project_dir": project_dir,
            "past_iterations": past_iterations,
            "has_more_older": has_more_older,
        },
    )


@app.post("/ui/snapshot/{view}/site-reference", response_class=HTMLResponse)
async def ui_upload_site_reference(
    request: Request,
    view: str,
    project_dir: Optional[str] = Form(None),
    site_reference_file: UploadFile = File(...),
) -> HTMLResponse:
    """Lädt das Site-Reference-Bild hoch und speichert es im Snapshot-Ordner.

    Liefert das aktualisierte snapshot_detail-Fragment zurueck — htmx
    swappt das #main-pane.
    """
    if not site_reference_file.filename:
        raise HTTPException(400, "Keine Datei ausgewaehlt")
    data = await site_reference_file.read()
    try:
        save_site_reference(
            view, project_dir, data,
            content_type=site_reference_file.content_type,
            original_filename=site_reference_file.filename,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _render_snapshot_detail_fragment(request, view, project_dir)


@app.post("/ui/snapshot/{view}/site-reference/delete", response_class=HTMLResponse)
def ui_delete_site_reference(
    request: Request,
    view: str,
    project_dir: Optional[str] = Form(None),
) -> HTMLResponse:
    """Loescht die Site-Reference aus dem Snapshot-Ordner."""
    try:
        delete_site_reference(view, project_dir)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return _render_snapshot_detail_fragment(request, view, project_dir)


@app.post("/ui/snapshot/{view}/site-reference/marker", response_class=HTMLResponse)
def ui_save_site_reference_marker(
    request: Request,
    view: str,
    project_dir: Optional[str] = Form(None),
    x_pct: Optional[float] = Form(None),
    y_pct: Optional[float] = Form(None),
) -> HTMLResponse:
    """Speichert (oder loescht) den Pin auf dem Site-Reference-Bild.

    Beide x/y leer = Pin loeschen. Sonst werden Werte ins [0,1]-Intervall
    geclampt und in meta.json/site_reference_marker geschrieben.
    """
    try:
        save_site_reference_marker(view, project_dir, x_pct, y_pct)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return _render_snapshot_detail_fragment(request, view, project_dir)


@app.post("/ui/snapshot/{view}/coordinates", response_class=HTMLResponse)
def ui_update_coordinates(
    request: Request,
    view: str,
    project_dir: Optional[str] = Form(None),
    latitude_deg: Optional[str] = Form(None),
    longitude_deg: Optional[str] = Form(None),
    place_name: Optional[str] = Form(None),
) -> HTMLResponse:
    """Setzt/aktualisiert die Lat/Lon in meta.json/site_location.

    Leere Felder = Koordinaten loeschen. Bei Aenderung wird die
    site_analysis.md-Cache invalidiert (naechste Iteration zieht frisch).
    """
    def _parse(v: Optional[str]) -> Optional[float]:
        if v is None:
            return None
        s = v.strip().replace(",", ".")
        if not s:
            return None
        try:
            return float(s)
        except ValueError as exc:
            raise HTTPException(400, f"Ungueltige Zahl: {v!r} ({exc})") from exc

    lat = _parse(latitude_deg)
    lon = _parse(longitude_deg)
    if (lat is None) != (lon is None):
        raise HTTPException(400, "Lat und Lon muessen beide gesetzt oder beide leer sein")
    if lat is not None:
        if not (-90.0 <= lat <= 90.0):
            raise HTTPException(400, f"Lat muss zwischen -90 und 90 liegen (war {lat})")
        if not (-180.0 <= lon <= 180.0):
            raise HTTPException(400, f"Lon muss zwischen -180 und 180 liegen (war {lon})")

    try:
        update_site_location(view, project_dir, lat, lon, place_name=place_name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return _render_snapshot_detail_fragment(request, view, project_dir)


# --- Ad-hoc-Modus (vision-only, kein BIM/Snapshot) ------------------------
@app.get("/adhoc", response_class=HTMLResponse)
def adhoc_page(
    request: Request,
    project_dir: Optional[str] = None,
    tag: Optional[str] = None,
) -> HTMLResponse:
    """Eigene Seite fuer den Ad-hoc-Workflow.

    User droppt ein Bild + Prompt → Director schreibt EN-Prompt → Generator
    rendert. Kein Validator, kein BIM. Persistierte Iterationen werden
    chronologisch vor-gerendert (Reload-Verlauf).

    `?tag=…` filtert die Iterationsliste auf einen Projekt-Tag.
    """
    record_recent_project(project_dir)
    past_iterations, has_more_older = list_adhoc_iterations(
        limit=ITERATIONS_PAGE_SIZE,
        tag=tag,
    )
    return TEMPLATES.TemplateResponse(
        request,
        "adhoc.html",
        {
            "snapshots": list_snapshots(project_dir),
            "snapshot_root": str(_resolve_snapshot_root(project_dir)),
            "active_snapshot": None,
            "active_tab": "adhoc",
            "adhoc_root": str(get_adhoc_root()),
            "project_dir": project_dir,
            "past_iterations": past_iterations,
            "has_more_older": has_more_older,
            "available_tags": list_adhoc_tags(),
            "active_tag": tag,
            "recent_projects": list_recent_projects(),
            "project_display_name": (
                _derive_project_display_name(project_dir) if project_dir else None
            ),
        },
    )


@app.get("/cost", response_class=HTMLResponse)
def cost_page(request: Request) -> HTMLResponse:
    """Kosten-/Token-Dashboard: aggregiert die usage-Blöcke aller Runs
    (Snapshot + Ad-hoc). Kosten sind Schätzungen (siehe usage.PRICING)."""
    return TEMPLATES.TemplateResponse(
        request,
        "cost.html",
        {
            "active_tab": "cost",
            "usage": aggregate_usage(),
            "recent_projects": list_recent_projects(),
        },
    )


@app.get("/ui/snapshot/{view}/older", response_class=HTMLResponse)
def ui_snapshot_iterations_older(
    request: Request,
    view: str,
    project_dir: Optional[str] = None,
    before: str = "",
    limit: int = ITERATIONS_PAGE_SIZE,
) -> HTMLResponse:
    """Liefert das naechste Paket aelterer Iterationen als HTML-Fragment.

    htmx-Target ist der "Aeltere anzeigen"-Button selbst (hx-swap=outerHTML)
    — die Response enthaelt ggf. einen neuen Button (falls noch mehr) plus
    die nachgeladenen Iterationen.
    """
    past_iterations, has_more_older = list_iterations_for_view(
        view, project_dir, limit=limit, before_run_id=before or None,
    )
    return TEMPLATES.TemplateResponse(
        request,
        "partials/iterations_chunk_snapshot.html",
        {
            "past_iterations": past_iterations,
            "has_more_older": has_more_older,
            "snapshot_view": view,
            "project_dir": project_dir,
        },
    )


@app.get("/ui/adhoc/older", response_class=HTMLResponse)
def ui_adhoc_iterations_older(
    request: Request,
    before: str = "",
    limit: int = ITERATIONS_PAGE_SIZE,
    tag: Optional[str] = None,
) -> HTMLResponse:
    past_iterations, has_more_older = list_adhoc_iterations(
        limit=limit, before_run_id=before or None, tag=tag,
    )
    return TEMPLATES.TemplateResponse(
        request,
        "partials/iterations_chunk_adhoc.html",
        {
            "past_iterations": past_iterations,
            "has_more_older": has_more_older,
            "active_tag": tag,
        },
    )


@app.post("/ui/run/delete", response_class=HTMLResponse)
def ui_delete_run(
    run_dir: str = Form(...),
) -> HTMLResponse:
    """Loescht einen einzelnen Run-Ordner (Snapshot oder Ad-hoc).

    htmx-Caller setzt hx-target auf das `<article>` der Bubble und
    hx-swap=delete — leere Response reicht.
    """
    try:
        deleted = delete_run(run_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not deleted:
        raise HTTPException(404, f"Run-Ordner nicht gefunden: {run_dir}")
    return HTMLResponse("")


@app.post("/ui/adhoc/iterate", response_class=HTMLResponse)
async def ui_adhoc_iterate(
    request: Request,
    user_prompt: str = Form(...),
    iteration_type: Literal["initial", "regenerate", "refine", "inpaint"] = Form("initial"),
    parent_run_id: Optional[str] = Form(None),
    previous_run_dir: Optional[str] = Form(None),
    user_feedback: Optional[str] = Form(None),
    mode: Literal["A", "B", "C", "D"] = Form("A"),
    validator_language: Literal["de", "it"] = Form("de"),
    generator_model: str = Form("gemini-2.5-flash-image"),
    generator_thinking_budget: Optional[int] = Form(None),
    input_image: Optional[UploadFile] = File(None),
    mask: Optional[UploadFile] = File(None),
    site_reference_file: Optional[UploadFile] = File(None),
    site_reference_marker_x_pct: Optional[str] = Form(None),
    site_reference_marker_y_pct: Optional[str] = Form(None),
    latitude_deg: Optional[str] = Form(None),
    longitude_deg: Optional[str] = Form(None),
    place_name: Optional[str] = Form(None),
    project_tag: Optional[str] = Form(None),
    attachments: list[UploadFile] = File(default=[]),
    attachment_description: Optional[str] = Form(None),
) -> HTMLResponse:
    """HTML-Form-Endpoint fuer den Ad-hoc-Chat. Liefert eine Bubble als
    Fragment, htmx haengt sie ans #adhoc-chat-list an.

    `input_image` ist nur bei `iteration_type="initial"` Pflicht. Bei
    Iterationen wird der Anker aus `previous_run_dir/input.png` gezogen.
    """
    image = None
    if iteration_type == "initial":
        if input_image is None or not input_image.filename:
            return TEMPLATES.TemplateResponse(
                request,
                "partials/error_bubble.html",
                {
                    "user_prompt": user_prompt,
                    "title": "Bitte zuerst ein Bild aussuchen",
                    "error_detail": "Im Ad-hoc-Modus ist das Input-Bild Pflicht — Datei waehlen oder per Drag-and-Drop reinziehen.",
                },
            )
        data = await input_image.read()
        if not data:
            return TEMPLATES.TemplateResponse(
                request,
                "partials/error_bubble.html",
                {
                    "user_prompt": user_prompt,
                    "title": "Leere Datei",
                    "error_detail": "Die hochgeladene Datei war leer.",
                },
            )
        try:
            image = Image.open(BytesIO(data)).convert("RGB")
        except Exception as e:
            return TEMPLATES.TemplateResponse(
                request,
                "partials/error_bubble.html",
                {
                    "user_prompt": user_prompt,
                    "title": "Bild konnte nicht geladen werden",
                    "error_detail": str(e),
                },
            )

    # Inpaint-Maske (nur iteration_type=inpaint). RGBA erhalten — der
    # Alpha-Kanal trägt die Semantik (transparent = „hier neu malen"). Bei
    # Inpaint ist die Maske Pflicht + das gpt-image-Backend zwingend (nur
    # dort native Masken-Unterstützung; die UI sperrt das schon vorher).
    mask_image = None
    if iteration_type == "inpaint":
        if not generator_model.startswith("gpt-image"):
            return TEMPLATES.TemplateResponse(
                request,
                "partials/error_bubble.html",
                {
                    "user_prompt": user_feedback or user_prompt,
                    "title": "Inpaint braucht das GPT-Backend",
                    "error_detail": "Maskiertes Neu-Rendern läuft nur über gpt-image-1. Bitte oben Generator 'GPT' wählen.",
                },
            )
        if mask is None or not mask.filename:
            return TEMPLATES.TemplateResponse(
                request,
                "partials/error_bubble.html",
                {
                    "user_prompt": user_feedback or user_prompt,
                    "title": "Keine Region markiert",
                    "error_detail": "Bitte im Inpaint-Editor eine Region übermalen, bevor du 'Inpaint' klickst.",
                },
            )
        m_data = await mask.read()
        if not m_data:
            return TEMPLATES.TemplateResponse(
                request,
                "partials/error_bubble.html",
                {
                    "user_prompt": user_feedback or user_prompt,
                    "title": "Leere Maske",
                    "error_detail": "Die übermittelte Maske war leer.",
                },
            )
        try:
            mask_image = Image.open(BytesIO(m_data)).convert("RGBA")
        except Exception as e:
            return TEMPLATES.TemplateResponse(
                request,
                "partials/error_bubble.html",
                {
                    "user_prompt": user_feedback or user_prompt,
                    "title": "Maske konnte nicht geladen werden",
                    "error_detail": str(e),
                },
            )

    # Optionale Site-Reference (Drohne/Maps/Foto) einlesen
    site_ref_image = None
    if site_reference_file is not None and site_reference_file.filename:
        sr_data = await site_reference_file.read()
        if sr_data:
            try:
                site_ref_image = Image.open(BytesIO(sr_data)).convert("RGB")
            except Exception as e:
                return TEMPLATES.TemplateResponse(
                    request, "partials/error_bubble.html",
                    {
                        "user_prompt": user_prompt,
                        "title": "Site-Reference konnte nicht geladen werden",
                        "error_detail": str(e),
                    },
                )

    # Optionale Per-Turn-Attachments (max 3) → PIL. Leere/unlesbare skippen
    # (optionale Extras sollen den Render nicht blockieren).
    attachment_images: list = []
    for upload in attachments or []:
        if not upload.filename:
            continue
        a_data = await upload.read()
        if not a_data:
            continue
        try:
            attachment_images.append(Image.open(BytesIO(a_data)).convert("RGB"))
        except Exception:
            continue

    def _parse_float(v: Optional[str]) -> Optional[float]:
        if v is None:
            return None
        s = v.strip().replace(",", ".")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    marker_x = _parse_float(site_reference_marker_x_pct)
    marker_y = _parse_float(site_reference_marker_y_pct)
    marker = (marker_x, marker_y) if (marker_x is not None and marker_y is not None) else None
    lat = _parse_float(latitude_deg)
    lon = _parse_float(longitude_deg)

    try:
        result = await run_in_threadpool(
            execute_adhoc_iteration,
            image,
            user_prompt,
            iteration_type=iteration_type,
            parent_run_id=parent_run_id,
            previous_run_dir=Path(previous_run_dir) if previous_run_dir else None,
            user_feedback=user_feedback,
            attachments=attachment_images or None,
            attachment_description=attachment_description,
            mask=mask_image,
            site_reference=site_ref_image,
            site_reference_marker=marker,
            latitude_deg=lat,
            longitude_deg=lon,
            place_name=place_name,
            project_tag=project_tag,
            mode=mode,
            generator_model=generator_model,
            generator_thinking_budget=generator_thinking_budget,
            validator_language=validator_language,
            # Zwei-Phasen: Bild sofort, Validator/Advisor via /ui/adhoc/iterate/validate.
            defer_validation=True,
        )
    except DirectorNoPromptError as e:
        return TEMPLATES.TemplateResponse(
            request,
            "partials/error_bubble.html",
            {
                "user_prompt": user_feedback or user_prompt,
                "director_reply": e.director_reply,
                "title": "Director hat keinen Prompt-Block geliefert",
                "hint": "Versuch's nochmal mit etwas konkreterem Wunsch.",
            },
        )
    except (FileNotFoundError, ValueError) as e:
        return TEMPLATES.TemplateResponse(
            request,
            "partials/error_bubble.html",
            {
                "user_prompt": user_feedback or user_prompt,
                "title": "Ad-hoc-Iteration fehlgeschlagen",
                "error_detail": str(e),
            },
        )
    except Exception as e:  # noqa: BLE001 — nur Budget-/Quota-Fehler abfangen
        bubble = classify_provider_budget_error(e, ui_lang_from_request(request))
        if bubble is None:
            raise  # kein Budget-Fehler → normal weiterreichen (500)
        return TEMPLATES.TemplateResponse(
            request,
            "partials/error_bubble.html",
            {
                "user_prompt": user_feedback or user_prompt,
                "title": bubble["title"],
                "hint": bubble["hint"],
                "error_detail": str(e)[:300],
            },
        )

    rel = result.run_dir.relative_to(get_adhoc_root()).as_posix()
    site_ref_url = (
        f"/files/adhoc-file/{rel}/site_reference.png"
        if result.site_reference_path is not None else None
    )
    return TEMPLATES.TemplateResponse(
        request,
        "partials/chat_adhoc_iteration.html",
        {
            "result": {
                "run_id": result.run_id,
                "run_dir": str(result.run_dir),
                "input_url": f"/files/adhoc-file/{rel}/input.png",
                "result_url": f"/files/adhoc-file/{rel}/result.png",
                "previous_url": (
                    f"/files/adhoc-file/{rel}/previous.png"
                    if (result.run_dir / "previous.png").is_file() else None
                ),
                "site_reference_url": site_ref_url,
                "site_reference_marker": (
                    {"x_pct": marker[0], "y_pct": marker[1]} if marker else None
                ),
                "site_location": (
                    {"latitude_deg": lat, "longitude_deg": lon, "place_name": place_name}
                    if (lat is not None or lon is not None) else None
                ),
                "iteration_type": result.iteration_type,
                "parent_run_id": result.parent_run_id,
                "final_prompt": result.final_prompt,
                "director_reply": result.director_reply,
                "validator_reply": result.validator_reply,
                "final_score": result.final_score,
                "section_means": result.section_means,
                "advisor_action": result.advisor_action,
                "advisor_reason": result.advisor_reason,
                "advisor_confidence": result.advisor_confidence,
                "project_tag": project_tag or None,
                "reference_library_refs": result.reference_library_refs,
            },
            "user_prompt": user_prompt,
            "user_feedback": user_feedback,
            "mode": mode,
            "validator_language": validator_language,
            "generator_model": generator_model,
            "generator_thinking_budget": generator_thinking_budget,
            # Phase-1-Bubble: Score-Block wird per /ui/adhoc/iterate/validate nachgeladen.
            "pending_validation": True,
        },
    )


@app.post("/ui/adhoc/iterate/validate", response_class=HTMLResponse)
async def ui_adhoc_iterate_validate(
    request: Request,
    run_dir: str = Form(...),
) -> HTMLResponse:
    """Phase 2 (Ad-hoc): Validator + Advisor auf dem bereits generierten Run;
    liefert den Score-Block, der den Platzhalter in der Bubble ersetzt."""
    try:
        outcome = await run_in_threadpool(finalize_adhoc_validation, run_dir)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(
            '<div class="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 '
            'text-xs text-amber-800">Bewertung fehlgeschlagen: {} — das Bild oben '
            'ist trotzdem gespeichert.</div>'.format(str(e)[:200])
        )
    return TEMPLATES.TemplateResponse(
        request,
        "partials/iteration_scoreblock.html",
        {"result": {
            "final_score": outcome.final_score,
            "section_means": outcome.section_means,
            "advisor_action": outcome.advisor_action,
            "advisor_reason": outcome.advisor_reason,
            "advisor_confidence": outcome.advisor_confidence,
            "validator_reply": outcome.validator_reply,
        }},
    )


# --- UI-Sprache umschalten -------------------------------------------------
@app.get("/set-language/{lang}")
def set_language(lang: str, request: Request) -> RedirectResponse:
    """Setzt das `ui_lang`-Cookie (Oberflächen-Sprache) und leitet dorthin
    zurück, wo der User war (Referer-Pfad, Open-Redirect-sicher)."""
    from urllib.parse import urlparse
    chosen = lang if lang in UI_LANGS else DEFAULT_UI_LANG
    ref = request.headers.get("referer") or "/"
    parsed = urlparse(ref)
    target = parsed.path + (("?" + parsed.query) if parsed.query else "")
    if not target.startswith("/"):
        target = "/"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie("ui_lang", chosen, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


# --- Health ---------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "snapshot_root": str(settings.snapshot_root),
        "generations_root": str(settings.generations_root),
    }


# --- Snapshot-Listing -----------------------------------------------------
@app.get("/snapshots", response_model=list[SnapshotSummary])
def get_snapshots() -> list[SnapshotSummary]:
    return list_snapshots()


@app.get("/snapshot/{view}", response_model=SnapshotDetail)
def get_snapshot(view: str) -> SnapshotDetail:
    detail = get_snapshot_detail(view)
    if detail is None:
        raise HTTPException(404, f"Snapshot {view!r} nicht gefunden")
    return detail


# --- Iteration ------------------------------------------------------------
@app.post("/iterate", response_model=IterationResponse)
async def iterate(req: IterationRequest) -> IterationResponse:
    """Startet eine Render-Iteration. Blockiert ~30–45 s (3 sequentielle AI-Calls).

    Pipeline-Aufruf läuft in einem Threadpool, damit das Event-Loop
    frei bleibt für parallele Health-Checks o.Ä. — der Aufruf selbst
    bleibt sync (run_iteration ist nicht async).
    """
    try:
        result = await run_in_threadpool(execute_iteration, req)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Run-Dir liegt unter generations_root — relative URL für StaticFiles
    rel = result.run_dir.relative_to(settings.generations_root).as_posix()
    return IterationResponse(
        run_id=result.run_id,
        run_dir=str(result.run_dir),
        result_url=f"/files/runs/{rel}/result.png",
        final_prompt=result.final_prompt,
        director_reply=result.director_reply,
        validator_reply=result.validator_reply,
        final_score=result.final_score,
        section_means=result.section_means,
        iteration_type=result.iteration_type,
        parent_run_id=result.parent_run_id,
        director_recommendation=result.director_recommendation,
        director_recommendation_reason=result.director_recommendation_reason,
        advisor_action=result.advisor_action,
        advisor_reason=result.advisor_reason,
        advisor_confidence=result.advisor_confidence,
    )
