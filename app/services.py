"""Backend-Services: Snapshot-Discovery + Wrapper für run_iteration.

Trennt Pipeline-Aufruf von HTTP-Layer — Endpoints in `main.py` rufen
nur diese Helper, damit die Logik unit-testbar bleibt ohne TestClient.

URL-driven Project-Scoping (Phase 4.5): alle Helper akzeptieren einen
optionalen `project_dir`-Param. Wenn gesetzt, wird er statt
`settings.snapshot_root` als Wurzel benutzt. So kann der pyRevit-Render-
Tool-Button die Webapp auf das aktive Revit-Projekt umschwenken, ohne
dass uvicorn neu gestartet werden muss.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.config import settings
from app.models import IterationRequest, IterationResponse, SnapshotDetail, SnapshotSummary
from render_director.pipeline import ADHOC_SUBDIR, AdhocIterationResult, IterationResult, run_iteration
from render_director.utils import _find_pass, load_scene_bundle, parse_advisor_recommendation


def _resolve_snapshot_root(project_dir: Optional[str | Path] = None) -> Path:
    """Liefert den effektiven Snapshot-Root.

    - Wenn `project_dir` gesetzt und existiert: dieser Pfad wird genutzt.
    - Sonst: `settings.snapshot_root` (Default `data/scenes/`).
    """
    if project_dir:
        p = Path(project_dir)
        if p.is_dir():
            return p
    return settings.snapshot_root


def _is_snapshot_dir(p: Path) -> bool:
    """Snapshot = Ordner mit beauty.* + meta.json (siehe utils.load_scene_bundle)."""
    if not p.is_dir():
        return False
    if not (p / "meta.json").is_file():
        return False
    return _find_pass(p, "beauty") is not None


def _count_runs_for_view(view: str) -> int:
    """Zählt bisherige Run-Ordner zu dieser View unter generations_root.

    Generations bleibt zentral (settings.generations_root), nicht
    project-scoped — über Projekte hinweg vergleichbar.
    """
    gen_root = settings.generations_root
    if not gen_root.is_dir():
        return 0
    view_capitalized = view.capitalize() if "_" in view else view
    candidates: list[Path] = []
    for phase_dir in gen_root.iterdir():
        if not phase_dir.is_dir():
            continue
        view_dir = phase_dir / view_capitalized
        if view_dir.is_dir():
            candidates.extend(p for p in view_dir.iterdir() if p.is_dir())
    return len(candidates)


def _build_snapshot_file_url(
    view: str, filename: str, project_dir: Optional[str | Path] = None
) -> str:
    """Baut die URL zum Custom-File-Serving-Endpoint /files/snapshot-file.

    Wenn `project_dir` gesetzt: wird als Query-Param mitgegeben, damit
    der Endpoint die Datei aus dem Projekt-Pfad statt aus dem Default-
    Snapshot-Root lädt.
    """
    from urllib.parse import quote
    base = f"/files/snapshot-file/{quote(view)}/{quote(filename)}"
    if project_dir:
        base += f"?project_dir={quote(str(project_dir), safe='')}"
    return base


def list_snapshots(project_dir: Optional[str | Path] = None) -> list[SnapshotSummary]:
    """Findet alle Snapshot-Ordner unter dem effektiven Snapshot-Root."""
    root = _resolve_snapshot_root(project_dir)
    if not root.is_dir():
        return []
    out: list[SnapshotSummary] = []
    for p in sorted(root.iterdir()):
        if not _is_snapshot_dir(p):
            continue
        bundle = load_scene_bundle(p)
        out.append(SnapshotSummary(
            view=p.name,
            project_name=bundle.meta.project_name,
            view_type=bundle.meta.view_type,
            has_depth=bundle.depth is not None,
            has_reference=bundle.reference is not None,
            n_runs=_count_runs_for_view(p.name),
        ))
    return out


def get_snapshot_detail(
    view: str, project_dir: Optional[str | Path] = None
) -> Optional[SnapshotDetail]:
    """Detail eines einzelnen Snapshots. None falls nicht gefunden."""
    root = _resolve_snapshot_root(project_dir)
    p = root / view
    if not _is_snapshot_dir(p):
        return None
    bundle = load_scene_bundle(p)
    beauty_path = _find_pass(p, "beauty")
    depth_path = _find_pass(p, "depth")
    ref_path = _find_pass(p, "reference")
    matid_path = _find_pass(p, "material_id")
    objid_path = _find_pass(p, "object_id")
    site_ref_path = _find_pass(p, "site_reference")

    return SnapshotDetail(
        view=view,
        project_name=bundle.meta.project_name,
        view_type=bundle.meta.view_type,
        has_depth=bundle.depth is not None,
        has_reference=bundle.reference is not None,
        n_runs=_count_runs_for_view(view),
        beauty_url=_build_snapshot_file_url(view, beauty_path.name, project_dir)
                   if beauty_path else "",
        depth_url=_build_snapshot_file_url(view, depth_path.name, project_dir)
                  if depth_path else None,
        reference_url=_build_snapshot_file_url(view, ref_path.name, project_dir)
                      if ref_path else None,
        material_id_url=_build_snapshot_file_url(view, matid_path.name, project_dir)
                        if matid_path else None,
        object_id_url=_build_snapshot_file_url(view, objid_path.name, project_dir)
                      if objid_path else None,
        site_reference_url=_build_snapshot_file_url(view, site_ref_path.name, project_dir)
                           if site_ref_path else None,
        meta=bundle.meta.model_dump(),
    )


# --- Site-Reference + Coords --------------------------------------------------

_SITE_REF_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}


def _resolve_snapshot_dir(view: str, project_dir: Optional[str | Path]) -> Path:
    root = _resolve_snapshot_root(project_dir)
    p = root / view
    if not _is_snapshot_dir(p):
        raise FileNotFoundError(f"Snapshot {view!r} nicht gefunden in {root}")
    return p


def save_site_reference(
    view: str,
    project_dir: Optional[str | Path],
    file_bytes: bytes,
    content_type: Optional[str],
    original_filename: Optional[str],
) -> Path:
    """Speichert ein hochgeladenes Site-Reference-Bild im Snapshot-Ordner.

    Loescht vorhandene `site_reference.*`-Files (egal welche Extension),
    schreibt dann das neue Bild. Extension aus MIME-Type, Fallback auf
    Datei-Endung des Original-Filenames, Default `.png`.

    Wirft FileNotFoundError, wenn das Snapshot nicht existiert. Akzeptiert
    PNG/JPG/WEBP — andere MIME-Typen werden als `.png` gespeichert (Best-
    Effort, der Loader behandelt das Bild eh als RGB).
    """
    if not file_bytes:
        raise ValueError("Datei ist leer.")
    snapshot_dir = _resolve_snapshot_dir(view, project_dir)

    ext = _SITE_REF_EXT_BY_MIME.get((content_type or "").lower())
    if not ext and original_filename:
        suffix = Path(original_filename).suffix.lower()
        if suffix in (".png", ".jpg", ".jpeg", ".webp"):
            ext = ".jpg" if suffix == ".jpeg" else suffix
    if not ext:
        ext = ".png"

    # Alte Site-Reference-Files entfernen (Extensions koennten differieren).
    for existing in snapshot_dir.glob("site_reference.*"):
        try:
            existing.unlink()
        except OSError:
            pass

    target = snapshot_dir / f"site_reference{ext}"
    target.write_bytes(file_bytes)
    return target


def delete_site_reference(view: str, project_dir: Optional[str | Path]) -> int:
    """Loescht alle `site_reference.*`-Files im Snapshot-Ordner. Returns Anzahl."""
    snapshot_dir = _resolve_snapshot_dir(view, project_dir)
    n = 0
    for existing in snapshot_dir.glob("site_reference.*"):
        try:
            existing.unlink()
            n += 1
        except OSError:
            pass
    return n


def save_site_reference_marker(
    view: str,
    project_dir: Optional[str | Path],
    x_pct: Optional[float],
    y_pct: Optional[float],
) -> Optional[dict]:
    """Setzt/loescht den Pin auf der Site-Reference.

    Beide None → Marker geloescht. Beide gesetzt + im Bereich [0,1] → Pin
    gespeichert. Returns das gespeicherte Marker-Dict oder None.
    """
    import json as _json

    snapshot_dir = _resolve_snapshot_dir(view, project_dir)
    meta_path = snapshot_dir / "meta.json"
    raw = _json.loads(meta_path.read_text(encoding="utf-8"))

    if x_pct is None or y_pct is None:
        raw["site_reference_marker"] = None
        result: Optional[dict] = None
    else:
        x = max(0.0, min(1.0, float(x_pct)))
        y = max(0.0, min(1.0, float(y_pct)))
        result = {"x_pct": round(x, 4), "y_pct": round(y, 4)}
        raw["site_reference_marker"] = result

    meta_path.write_text(
        _json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    return result


def update_site_location(
    view: str,
    project_dir: Optional[str | Path],
    latitude_deg: Optional[float],
    longitude_deg: Optional[float],
    place_name: Optional[str] = None,
) -> dict:
    """Patcht `meta.json/site_location` mit User-eingegebenen Koordinaten.

    - Wenn lat+lon beide None: site_location wird auf None gesetzt (= geloescht).
    - Sonst: bestehender Block wird ergaenzt/ueberschrieben; Hoehe und Zeitzone
      bleiben erhalten (falls vorhanden).
    - Bei Coord-Change wird die `site_analysis.md`-Cache geloescht, damit
      die naechste Iteration einen frischen Wikidata/OSM-Pull macht
      (die alte Analyse waere zu den alten Koordinaten gewesen).

    Returns das aktualisierte `site_location`-Dict (oder leeres Dict bei clear).
    """
    import json as _json

    snapshot_dir = _resolve_snapshot_dir(view, project_dir)
    meta_path = snapshot_dir / "meta.json"
    raw = _json.loads(meta_path.read_text(encoding="utf-8"))

    old_lat = (raw.get("site_location") or {}).get("latitude_deg")
    old_lon = (raw.get("site_location") or {}).get("longitude_deg")
    coords_changed = (old_lat != latitude_deg) or (old_lon != longitude_deg)

    if latitude_deg is None and longitude_deg is None:
        raw["site_location"] = None
        new_block: dict = {}
    else:
        existing = raw.get("site_location") or {}
        if not isinstance(existing, dict):
            existing = {}
        new_block = {
            **existing,
            "latitude_deg": latitude_deg,
            "longitude_deg": longitude_deg,
        }
        if place_name is not None:
            new_block["place_name"] = place_name or None
        raw["site_location"] = new_block

    meta_path.write_text(
        _json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8",
    )

    # Site-Analyse-Cache invalidieren, falls Coordinates sich geaendert haben.
    if coords_changed:
        cache = snapshot_dir / "site_analysis.md"
        if cache.is_file():
            try:
                cache.unlink()
            except OSError:
                pass
        site_refs_dir = snapshot_dir / "site_references"
        if site_refs_dir.is_dir():
            import shutil as _shutil
            try:
                _shutil.rmtree(site_refs_dir)
            except OSError:
                pass

    return new_block


def _list_run_dirs_for_view(view: str) -> list[Path]:
    """Findet alle Run-Ordner zu einer View unter `generations_root`.

    Struktur: `<gen_root>/<phase_label>/<View_Capitalized>/<run_id>/`.
    Sortiert chronologisch (run_id beginnt mit Timestamp `YYYY-MM-DD_HHMMSS`).
    """
    gen_root = settings.generations_root
    if not gen_root.is_dir():
        return []
    view_capitalized = view.capitalize() if "_" in view else view
    runs: list[Path] = []
    for phase_dir in gen_root.iterdir():
        if not phase_dir.is_dir():
            continue
        view_dir = phase_dir / view_capitalized
        if view_dir.is_dir():
            runs.extend(p for p in view_dir.iterdir() if p.is_dir())
    runs.sort(key=lambda p: p.name)
    return runs


def list_iterations_for_view(
    view: str,
    project_dir: Optional[str | Path] = None,
    *,
    limit: Optional[int] = None,
    before_run_id: Optional[str] = None,
) -> tuple[list[dict], bool]:
    """Lädt persistierte Iterationen so dass sie wie frisch gerenderte Bubbles
    aussehen — fixt den UX-Bug, dass Page-Reload den Chat-Verlauf leert.

    Pro Run-Ordner: liest `inputs.json` (user_request, mode, generator_model,
    iteration-Block, scores) + `validator_reply.md` + `advisor_reply.md`
    (geparst) + verifiziert dass `result.png` existiert. Runs ohne diese
    Pflicht-Artefakte werden uebersprungen (defekte/abgebrochene Runs).

    Pagination:
    - `limit=None` (default): alle Iterationen, has_more_older=False
    - `limit=N, before_run_id=None`: die N neuesten Iterationen
    - `limit=N, before_run_id=X`: N Iterationen chronologisch *vor* X

    Returns: Tuple (Liste oldest→newest, has_more_older).
    """
    import json as _json

    gen_root = settings.generations_root
    pd_str = str(project_dir) if project_dir else None

    all_run_dirs = _list_run_dirs_for_view(view)
    if before_run_id is not None:
        # nur Runs vor dem Cursor (alphabetisch = chronologisch da Timestamp-Prefix)
        all_run_dirs = [r for r in all_run_dirs if r.name < before_run_id]

    has_more_older = False
    if limit is not None and len(all_run_dirs) > limit:
        has_more_older = True
        all_run_dirs = all_run_dirs[-limit:]  # die N neuesten aus dem Rest

    out: list[dict] = []
    for run_dir in all_run_dirs:
        inputs_path = run_dir / "inputs.json"
        result_png = run_dir / "result.png"
        if not inputs_path.is_file() or not result_png.is_file():
            continue
        try:
            payload = _json.loads(inputs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        def _read_optional(name: str) -> str:
            p = run_dir / name
            return p.read_text(encoding="utf-8") if p.is_file() else ""

        validator_reply = _read_optional("validator_reply.md")
        director_reply = _read_optional("director_reply.md")
        final_prompt = _read_optional("final_prompt.txt")

        advisor_action: Optional[str] = None
        advisor_reason: Optional[str] = None
        advisor_confidence: Optional[str] = None
        advisor_text = _read_optional("advisor_reply.md")
        if advisor_text:
            try:
                parsed = parse_advisor_recommendation(advisor_text)
                if parsed is not None:
                    advisor_action, advisor_reason, advisor_confidence = parsed
            except Exception:
                pass

        iter_block = payload.get("iteration") or {}
        if not isinstance(iter_block, dict):
            iter_block = {}

        rel = run_dir.relative_to(gen_root).as_posix()
        response = IterationResponse(
            run_id=run_dir.name,
            run_dir=str(run_dir),
            result_url=f"/files/runs/{rel}/result.png",
            final_prompt=final_prompt,
            director_reply=director_reply,
            validator_reply=validator_reply,
            final_score=payload.get("final_score"),
            section_means=payload.get("section_means") or {},
            iteration_type=iter_block.get("type", "initial"),
            parent_run_id=iter_block.get("parent_run_id"),
            director_recommendation=payload.get("director_recommendation"),
            director_recommendation_reason=iter_block.get("reason"),
            advisor_action=advisor_action,
            advisor_reason=advisor_reason,
            advisor_confidence=advisor_confidence,
        )

        out.append({
            "result": response,
            "result_url": response.result_url,
            "user_prompt": payload.get("user_request", ""),
            "user_feedback": payload.get("user_feedback"),
            "snapshot": view,
            "mode": payload.get("mode", "A"),
            # validator_language seit 2026-07-02 in inputs.json persistiert
            # (Reload behält die Sprache). Altdaten ohne Feld -> "de".
            # generator_thinking_budget bleibt Default (nicht persistiert).
            "validator_language": payload.get("validator_language") or "de",
            "generator_model": payload.get("generator_model") or "gemini-2.5-flash-image",
            "generator_thinking_budget": None,
            "project_dir": pd_str,
        })

    return out, has_more_older


# --- Ad-hoc-Zielordner (Option B) -----------------------------------------
# Ad-hoc-Runs liegen NICHT im Repo, sondern in einem user-nahen, browsbaren
# Ordner. Prioritaet: Env RENDER_ADHOC_ROOT > webapp.json:adhoc_output_dir >
# Default Documents\Render Director. Session-Struktur: <root>/<datum_zeit_slug>/<run>/.

def _webapp_config_path() -> Path:
    import os
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "render_director" / "webapp.json"


def _read_webapp_config() -> dict:
    import json as _json
    p = _webapp_config_path()
    if not p.is_file():
        return {}
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get_adhoc_root() -> Path:
    """Zielordner fuer Ad-hoc-Runs (Option B). Wird bei Bedarf angelegt."""
    import os
    from render_director.clients import _ensure_dotenv
    _ensure_dotenv()  # .env kann vor dem ersten API-Call noch ungeladen sein
    env = os.environ.get("RENDER_ADHOC_ROOT")
    if env:
        root = Path(env)
    else:
        stored = _read_webapp_config().get("adhoc_output_dir")
        root = Path(stored) if stored else (Path.home() / "Documents" / "Render Director")
    root.mkdir(parents=True, exist_ok=True)
    return root


def set_adhoc_root(path: str) -> Path:
    """Speichert den Ad-hoc-Zielordner in webapp.json und legt ihn an.

    Wirft ValueError bei einem Laufwerks-Root (z.B. `C:\\`): dort würde der
    History-Loader das ganze Laufwerk scannen und auf geschützte System-
    Junctions (`C:\\Documents and Settings`) stoßen → PermissionError.
    """
    root = Path(path)
    if root == root.parent or not root.name:
        raise ValueError(
            "Ungültiger Zielordner {!r} — bitte einen konkreten Unterordner "
            "wählen, kein Laufwerks-Root.".format(str(root))
        )
    import json as _json
    root.mkdir(parents=True, exist_ok=True)
    cfg = _read_webapp_config()
    cfg["adhoc_output_dir"] = str(root)
    p = _webapp_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return root


def pick_folder_native(initial: Optional[str] = None) -> Optional[str]:
    """Öffnet einen nativen Windows-Ordner-Dialog in einem Subprozess (um
    tkinter-Thread-Probleme im Server-Threadpool zu vermeiden). Funktioniert,
    weil die Webapp lokal in der User-Session läuft. Gibt den gewählten Pfad
    zurück oder None (Abbruch/Fehler)."""
    import subprocess
    import sys as _sys
    script = (
        "import tkinter, tkinter.filedialog as fd, sys\n"
        "r = tkinter.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "p = fd.askdirectory(title='Render Director - Ad-hoc-Zielordner waehlen'"
        + ((", initialdir=%r" % initial) if initial else "")
        + ")\n"
        "sys.stdout.write(p or '')\n"
    )
    try:
        out = subprocess.run(
            [_sys.executable, "-c", script],
            capture_output=True, text=True, timeout=300,
        )
        return (out.stdout or "").strip() or None
    except Exception:
        return None


def _iter_adhoc_run_dirs(adhoc_root: Path):
    """Yields alle Run-Ordner unter adhoc_root (Session/Run-Nesting).

    Defensiv gegen unzugängliche Ordner: wurde der adhoc_root versehentlich zu
    hoch gesetzt (z.B. auf `C:\\`), stößt iterdir() auf geschützte Windows-
    Junctions wie `C:\\Documents and Settings` und wirft PermissionError. Das
    darf nicht die ganze Seite killen — unzugängliche Ordner werden übersprungen.
    """
    try:
        if not adhoc_root.is_dir():
            return
        sessions = list(adhoc_root.iterdir())
    except (PermissionError, OSError):
        return
    for session in sessions:
        try:
            if not session.is_dir():
                continue
            runs = list(session.iterdir())
        except (PermissionError, OSError):
            continue
        for run in runs:
            try:
                if run.is_dir():
                    yield run
            except (PermissionError, OSError):
                continue


def list_adhoc_iterations(
    *,
    limit: Optional[int] = None,
    before_run_id: Optional[str] = None,
    tag: Optional[str] = None,
) -> tuple[list[dict], bool]:
    """Laedt persistierte Ad-hoc-Iterationen fuer den Reload-Verlauf.

    Scannt `<gen_root>/_adhoc/<run_id>/`, sortiert chronologisch (Timestamp-
    Prefix), filtert Runs ohne `result.png` / `inputs.json` raus.

    Pagination wie in `list_iterations_for_view`. `tag` filtert auf
    `inputs.json:project_tag` (case-insensitive). Wird der Tag-Filter
    angewendet, wirkt `limit` auf die gefilterte Menge.

    Returns: Tuple (Liste oldest→newest, has_more_older).
    """
    import json as _json

    adhoc_root = get_adhoc_root()
    run_dirs = sorted(_iter_adhoc_run_dirs(adhoc_root), key=lambda p: p.name)
    if before_run_id is not None:
        run_dirs = [r for r in run_dirs if r.name < before_run_id]

    out: list[dict] = []
    tag_lower = tag.strip().lower() if tag else None
    for run_dir in run_dirs:
        inputs_path = run_dir / "inputs.json"
        result_png = run_dir / "result.png"
        input_png = run_dir / "input.png"
        if not (inputs_path.is_file() and result_png.is_file() and input_png.is_file()):
            continue
        try:
            payload = _json.loads(inputs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        project_tag = payload.get("project_tag") or None

        # Tag-Filter: nur Runs mit passendem (oder geerbtem) Tag durchlassen
        if tag_lower is not None:
            run_tag = (project_tag or "").strip().lower()
            if run_tag != tag_lower:
                continue

        def _read_optional(name: str) -> str:
            p = run_dir / name
            return p.read_text(encoding="utf-8") if p.is_file() else ""

        director_reply = _read_optional("director_reply.md")
        final_prompt = _read_optional("final_prompt.txt")
        validator_reply = _read_optional("validator_reply.md")

        # Advisor-Empfehlung von Disk rekonstruieren (wie Snapshot-Loader).
        adv_action: Optional[str] = None
        adv_reason: Optional[str] = None
        adv_confidence: Optional[str] = None
        advisor_text = _read_optional("advisor_reply.md")
        if advisor_text:
            try:
                parsed = parse_advisor_recommendation(advisor_text)
                if parsed is not None:
                    adv_action, adv_reason, adv_confidence = parsed
            except Exception:
                pass

        iter_block = payload.get("iteration") or {}
        if not isinstance(iter_block, dict):
            iter_block = {}

        rel = run_dir.relative_to(adhoc_root).as_posix()
        site_ref_url = None
        if (run_dir / "site_reference.png").is_file():
            site_ref_url = f"/files/adhoc-file/{rel}/site_reference.png"
        # Render-Modus normalisieren: Altdaten trugen hier den Marker
        # "adhoc" (kein gültiger Modus) — auf "A" defaulten, damit das
        # Iterations-Formular kein ungültiges mode weiterreicht.
        run_mode = payload.get("mode", "A")
        if run_mode not in ("A", "B", "C", "D"):
            run_mode = "A"
        run_lang = payload.get("validator_language", "de")
        if run_lang not in ("de", "it"):
            run_lang = "de"
        out.append({
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "mode": run_mode,
            "validator_language": run_lang,
            "input_url": f"/files/adhoc-file/{rel}/input.png",
            "result_url": f"/files/adhoc-file/{rel}/result.png",
            # Bei Iterationen (refine/inpaint) das bearbeitete Basis-Bild
            # (= voriges Result) für die Bubble-Anzeige, damit klar ist, dass
            # der Edit auf dem GENERIERTEN Bild passiert, nicht auf dem Original.
            "previous_url": (
                f"/files/adhoc-file/{rel}/previous.png"
                if (run_dir / "previous.png").is_file() else None
            ),
            "site_reference_url": site_ref_url,
            "site_reference_marker": payload.get("site_reference_marker"),
            "site_location": payload.get("site_location"),
            "user_prompt": payload.get("user_request", ""),
            "user_feedback": payload.get("user_feedback"),
            "project_tag": project_tag,
            "reference_library_refs": payload.get("reference_library_refs") or [],
            "final_prompt": final_prompt,
            "director_reply": director_reply,
            "validator_reply": validator_reply,
            "final_score": payload.get("final_score"),
            "section_means": payload.get("section_means") or {},
            "advisor_action": adv_action,
            "advisor_reason": adv_reason,
            "advisor_confidence": adv_confidence,
            "iteration_type": iter_block.get("type", "initial"),
            "parent_run_id": iter_block.get("parent_run_id"),
            "generator_model": payload.get("generator_model") or "gemini-2.5-flash-image",
            "generator_thinking_budget": payload.get("generator_thinking_budget"),
        })

    # Pagination: nach Filterung + Build die N neuesten Eintraege zurueckgeben.
    has_more_older = False
    if limit is not None and len(out) > limit:
        has_more_older = True
        out = out[-limit:]
    return out, has_more_older


def _recent_projects_path() -> Path:
    """User-lokaler Speicher fuer die letzten besuchten Projekte.

    Bewusst pro User-Home (nicht im Repo): bei Multi-User-Deployment hat
    jeder Architekt seine eigene Liste, ohne dass die sich gegenseitig
    ueberschreiben.
    """
    p = Path.home() / ".render_director"
    p.mkdir(parents=True, exist_ok=True)
    return p / "recent_projects.json"


_RECENT_PROJECTS_LIMIT = 12

def _derive_project_display_name(project_dir: str) -> str:
    """Liefert den User-sichtbaren Projekt-Namen aus dem `?project_dir=...`-Pfad.

    `project_dir` zeigt auf den `Revit Render`-Ordner neben der Revit-Datei,
    der Projekt-Name ist deshalb der Eltern-Folder (parts[-2]). Fallback:
    parts[-1] oder 'Projekt'. Bewusst ohne Annahmen über Laufwerke oder
    Ablage-Konventionen eines bestimmten Büros.
    """
    norm = project_dir.replace("/", "\\")
    parts = [p for p in norm.split("\\") if p]
    # Eltern-Folder von 'Revit Render'/'Revit_Render'
    if len(parts) >= 2:
        return parts[-2]
    if parts:
        return parts[-1]
    return "Projekt"


def record_recent_project(project_dir: Optional[str | Path]) -> None:
    """Stempelt ein Projekt mit `last_used=now`. No-op bei leerem project_dir.

    Schreibt in `~/.render_director/recent_projects.json` — Liste von Dicts
    `{project_dir, project_name, last_used}`. Dupletten werden anhand
    project_dir aufgeloest (neuere Verwendung gewinnt). Liste cappt bei
    `_RECENT_PROJECTS_LIMIT`.
    """
    if not project_dir:
        return
    import json as _json
    from datetime import datetime as _dt

    pd_str = str(Path(project_dir))
    display = _derive_project_display_name(pd_str)

    path = _recent_projects_path()
    entries: list[dict] = []
    if path.is_file():
        try:
            raw = _json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                entries = [e for e in raw if isinstance(e, dict)]
        except (OSError, ValueError):
            entries = []

    # Bestehenden Eintrag fuer denselben project_dir entfernen
    entries = [e for e in entries if e.get("project_dir") != pd_str]
    entries.insert(0, {
        "project_dir": pd_str,
        "project_name": display,
        "last_used": _dt.now().isoformat(timespec="seconds"),
    })
    entries = entries[:_RECENT_PROJECTS_LIMIT]

    try:
        path.write_text(
            _json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        # Best-effort: wenn der User-Home read-only ist, leise weiterlaufen.
        pass


def list_recent_projects() -> list[dict]:
    """Liest die Liste der zuletzt besuchten Projekte. Filtert nicht-mehr-
    existierende Pfade raus."""
    import json as _json
    path = _recent_projects_path()
    if not path.is_file():
        return []
    try:
        raw = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        pd = e.get("project_dir")
        if not pd or not Path(pd).is_dir():
            continue
        # project_name kann veraltet sein (alte Heuristik geschrieben) —
        # bei jedem Read auf die aktuelle Derivations-Regel re-projizieren.
        out.append({
            "project_dir": pd,
            "project_name": _derive_project_display_name(pd),
            "last_used": e.get("last_used"),
        })
    return out


def delete_run(run_dir: str | Path) -> bool:
    """Loescht einen Run-Ordner samt aller Artefakte.

    Path-Traversal-Schutz: der Pfad muss unter `settings.generations_root`
    liegen, sonst wird verweigert. Returns True wenn geloescht, False wenn
    nicht gefunden. Hard-error bei Permission-Fehlern.
    """
    import shutil as _shutil
    p = Path(run_dir).resolve()
    gen_root = settings.generations_root.resolve()
    try:
        p.relative_to(gen_root)
    except ValueError:
        raise ValueError(
            f"Pfad-Traversal nicht erlaubt: {p} liegt nicht unter {gen_root}"
        )
    if not p.is_dir():
        return False
    _shutil.rmtree(p)
    return True


def list_adhoc_tags() -> list[str]:
    """Sammelt alle distinct `project_tag`-Werte aus persistierten Ad-hoc-Runs.

    Sortiert alphabetisch (case-insensitive), Duplikate weg, leere Tags
    übersprungen. Wird vom UI-Filter-Dropdown auf /adhoc benutzt.
    """
    import json as _json

    tags: set[str] = set()
    for run_dir in _iter_adhoc_run_dirs(get_adhoc_root()):
        p = run_dir / "inputs.json"
        if not p.is_file():
            continue
        try:
            payload = _json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        t = payload.get("project_tag")
        if t and t.strip():
            tags.add(t.strip())
    return sorted(tags, key=str.lower)


def execute_adhoc_iteration(
    input_image,  # PIL.Image.Image, lazily typed (Phase-4-Konvention)
    user_prompt: str,
    *,
    iteration_type: str = "initial",
    parent_run_id: Optional[str] = None,
    previous_run_dir: Optional[Path] = None,
    user_feedback: Optional[str] = None,
    attachments=None,  # list[PIL.Image.Image] | None
    attachment_description: Optional[str] = None,
    mask=None,  # PIL.Image.Image | None — Inpaint-Maske (RGBA, transparent = neu malen)
    site_reference=None,  # PIL.Image.Image | None
    site_reference_marker: Optional[tuple[float, float]] = None,
    latitude_deg: Optional[float] = None,
    longitude_deg: Optional[float] = None,
    place_name: Optional[str] = None,
    project_tag: Optional[str] = None,
    mode: str = "A",
    generator_model: str = "gemini-2.5-flash-image",
    generator_thinking_budget: Optional[int] = None,
    validator_language: str = "de",
    defer_validation: bool = False,
) -> AdhocIterationResult:
    """Wrapper um `run_iteration_adhoc` fuer den HTTP-Layer.

    Bei Iteration: liest `input.png` (Original-Anker) + `result.png` (=das,
    was iteriert wird) + `final_prompt.txt` aus dem `previous_run_dir`.
    Site-Reference + Marker + Coords werden aus inputs.json des vorigen
    Runs uebernommen, sofern der Caller nicht explizit neue Werte
    mitgibt (= neue Site-Ref-Auswahl in der Iteration).
    """
    import json as _json
    from PIL import Image as _Image
    from render_director.pipeline import run_iteration_adhoc

    prev_image = None
    prev_final_prompt = None
    prev_validator_reply = None
    if iteration_type != "initial":
        if previous_run_dir is None:
            raise ValueError(
                f"iteration_type={iteration_type} braucht previous_run_dir"
            )
        prev = Path(previous_run_dir)
        # Original-Input aus dem vorigen Run-Dir uebernehmen — bleibt
        # ueber die ganze Iterations-Kette stabil. Bei regenerate nutzt der
        # Generator das Input-Bild, bei refine das previous_result.
        input_image = _Image.open(prev / "input.png").convert("RGB")
        prev_image = _Image.open(prev / "result.png").convert("RGB")
        prev_final_prompt = (prev / "final_prompt.txt").read_text(encoding="utf-8")
        # Vorigen Validator-Report mitgeben (wie Snapshot-Flow), damit der
        # Iteration-Director gezielt die Top-Issues adressiert. Optional:
        # alte Runs ohne Validator -> None (Director iteriert dann nur auf
        # Prompt + User-Feedback).
        prev_validator_path = prev / "validator_reply.md"
        prev_validator_reply = (
            prev_validator_path.read_text(encoding="utf-8")
            if prev_validator_path.is_file() else None
        )

        # Site-Ref + Marker + Coords aus vorigem Run uebernehmen, sofern
        # diese Iteration keine neuen mitgibt. So bleibt der Standort-
        # Kontext ueber die Kette stabil ohne dass der User ihn jedes Mal
        # neu hochladen muss.
        prev_inputs_path = prev / "inputs.json"
        if prev_inputs_path.is_file():
            try:
                prev_inputs = _json.loads(prev_inputs_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prev_inputs = {}
            if site_reference is None:
                prev_site_ref = prev / "site_reference.png"
                if prev_site_ref.is_file():
                    site_reference = _Image.open(prev_site_ref).convert("RGB")
            if site_reference_marker is None:
                m = prev_inputs.get("site_reference_marker")
                if isinstance(m, dict) and m.get("x_pct") is not None and m.get("y_pct") is not None:
                    site_reference_marker = (float(m["x_pct"]), float(m["y_pct"]))
            if latitude_deg is None and longitude_deg is None:
                sl = prev_inputs.get("site_location") or {}
                if isinstance(sl, dict):
                    latitude_deg = sl.get("latitude_deg")
                    longitude_deg = sl.get("longitude_deg")
                    if not place_name:
                        place_name = sl.get("place_name")
            if not project_tag:
                project_tag = prev_inputs.get("project_tag")

    session_dir = None
    if iteration_type != "initial" and previous_run_dir is not None:
        session_dir = Path(previous_run_dir).parent

    return run_iteration_adhoc(
        input_image,
        user_prompt,
        iteration_type=iteration_type,  # type: ignore[arg-type]
        parent_run_id=parent_run_id,
        previous_image=prev_image,
        previous_final_prompt=prev_final_prompt,
        previous_validator_reply=prev_validator_reply,
        user_feedback=user_feedback,
        attachments=attachments,
        attachment_description=attachment_description,
        mask=mask,
        site_reference=site_reference,
        site_reference_marker=site_reference_marker,
        latitude_deg=latitude_deg,
        longitude_deg=longitude_deg,
        place_name=place_name,
        project_tag=project_tag,
        reference_library_root=settings.reference_library_root,
        mode=mode,  # type: ignore[arg-type]
        session_dir=session_dir,
        generations_root=get_adhoc_root(),
        generator_model=generator_model,
        generator_thinking_budget=generator_thinking_budget,
        validator_language=validator_language,  # type: ignore[arg-type]
        defer_validation=defer_validation,
    )


def execute_iteration(req: IterationRequest, defer_validation: bool = False) -> IterationResult:
    """Wrapper um `run_iteration`, mappt das Pydantic-Request auf Kwargs.

    Für regenerate/refine erwartet der Service, dass
    `previous_run_dir` gesetzt ist; daraus werden result.png + Prompt
    + Validator-Reply gezogen.

    Wenn `req.project_dir` gesetzt: das Snapshot wird aus diesem Pfad
    geladen statt aus dem Default-Snapshot-Root.
    """
    root = _resolve_snapshot_root(req.project_dir)
    snapshot_dir = root / req.snapshot
    if not _is_snapshot_dir(snapshot_dir):
        raise FileNotFoundError(
            f"Snapshot {req.snapshot!r} nicht gefunden in {root}"
        )

    prev_image_path: Optional[Path] = None
    prev_final_prompt: Optional[str] = None
    prev_validator_reply: Optional[str] = None

    if req.iteration_type != "initial":
        if req.previous_run_dir is None:
            raise ValueError(
                f"iteration_type={req.iteration_type} braucht previous_run_dir"
            )
        prev = Path(req.previous_run_dir)
        prev_image_path = prev / "result.png"
        prev_final_prompt = (prev / "final_prompt.txt").read_text(encoding="utf-8")
        prev_validator_reply = (prev / "validator_reply.md").read_text(encoding="utf-8")

    run_index = 1 if req.iteration_type == "initial" else (_count_runs_for_view(req.snapshot) + 1)

    return run_iteration(
        snapshot_dir,
        req.user_prompt,
        mode=req.mode,
        iteration_type=req.iteration_type,
        parent_run_id=req.parent_run_id,
        previous_image_path=prev_image_path,
        previous_final_prompt=prev_final_prompt,
        previous_validator_reply=prev_validator_reply,
        user_feedback=req.user_feedback,
        attachments=req.attachment_images or None,
        attachment_description=req.attachment_description,
        run_index=run_index,
        generations_root=settings.generations_root,
        phase_label=settings.phase_label,
        validator_language=req.validator_language,
        generator_model=req.generator_model,
        generator_thinking_budget=req.generator_thinking_budget,
        use_reference_library=req.use_reference_library,
        reference_library_root=settings.reference_library_root,
        reference_project_tag=req.reference_project_tag,
        defer_validation=defer_validation,
    )


def _path_within(child: Path, parent: Path) -> bool:
    """True, wenn `child` (aufgelöst) unter `parent` liegt — Traversal-Schutz
    für die run_dir-Parameter der Phase-2-Endpoints (Client-geliefert)."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def finalize_snapshot_validation(run_dir: Path | str):
    """Phase 2 (Snapshot): Validator + Advisor auf einem bereits generierten
    Run. `run_dir` muss unter dem generations_root liegen (Traversal-Schutz)."""
    from render_director.pipeline import finalize_iteration
    p = Path(run_dir)
    if not _path_within(p, Path(settings.generations_root)):
        raise ValueError(f"run_dir liegt nicht unter generations_root: {run_dir}")
    return finalize_iteration(p)


def finalize_adhoc_validation(run_dir: Path | str):
    """Phase 2 (Ad-hoc): Validator + Advisor auf einem bereits generierten Run.
    `run_dir` muss unter dem Ad-hoc-Root liegen (Traversal-Schutz)."""
    from render_director.pipeline import finalize_adhoc_run
    p = Path(run_dir)
    if not _path_within(p, get_adhoc_root()):
        raise ValueError(f"run_dir liegt nicht unter dem Ad-hoc-Root: {run_dir}")
    return finalize_adhoc_run(p)


# --- Cost-Controlling: Usage-Aggregation über alle Runs --------------------

def aggregate_usage() -> dict:
    """Scannt Snapshot- + Ad-hoc-Runs, summiert Token/Kosten aus den
    `usage`-Blöcken der `inputs.json`.

    Runs von vor der Cost-Logging-Einführung haben keinen `usage`-Block →
    werden als `n_runs_no_usage` gezählt (informativ), fließen aber nicht
    in die Summen. Kosten sind Schätzungen aus `render_director.usage.PRICING`.

    Returns ein für das `cost.html`-Template aufbereitetes Dict.
    """
    import json as _json

    roots = [
        ("snapshot", Path(settings.generations_root)),
        ("adhoc", get_adhoc_root()),
    ]

    total_in = total_out = 0
    total_cost = 0.0
    n_runs = 0
    n_runs_no_usage = 0
    by_model: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    by_month: dict[str, dict] = {}
    by_flow: dict[str, dict] = {}

    for flow, root in roots:
        if not root or not Path(root).is_dir():
            continue
        for inputs_path in Path(root).rglob("inputs.json"):
            try:
                payload = _json.loads(inputs_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            usage = payload.get("usage")
            if not isinstance(usage, dict) or not usage.get("records"):
                n_runs_no_usage += 1
                continue

            n_runs += 1
            run_in = int(usage.get("input_tokens", 0) or 0)
            run_out = int(usage.get("output_tokens", 0) or 0)
            run_cost = float(usage.get("estimated_cost_usd", 0.0) or 0.0)
            total_in += run_in
            total_out += run_out
            total_cost += run_cost

            fl = by_flow.setdefault(flow, {"runs": 0, "cost_usd": 0.0, "total_tokens": 0})
            fl["runs"] += 1
            fl["cost_usd"] += run_cost
            fl["total_tokens"] += run_in + run_out

            day = (payload.get("timestamp") or "")[:10] or "unbekannt"
            d = by_day.setdefault(day, {"runs": 0, "cost_usd": 0.0, "total_tokens": 0})
            d["runs"] += 1
            d["cost_usd"] += run_cost
            d["total_tokens"] += run_in + run_out

            # Monats-Bucket (YYYY-MM): jeder Kalendermonat sammelt seine eigene
            # Summe; ein neuer Monat startet automatisch eine neue Zeile, alte
            # Monate bleiben stehen (User-Wunsch Monats-Abrechnung).
            month = (payload.get("timestamp") or "")[:7] or "unbekannt"
            mo = by_month.setdefault(month, {"runs": 0, "cost_usd": 0.0, "total_tokens": 0})
            mo["runs"] += 1
            mo["cost_usd"] += run_cost
            mo["total_tokens"] += run_in + run_out

            for rec in usage["records"]:
                model = rec.get("model", "?")
                m = by_model.setdefault(
                    model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                )
                m["calls"] += 1
                m["input_tokens"] += int(rec.get("input_tokens", 0) or 0)
                m["output_tokens"] += int(rec.get("output_tokens", 0) or 0)
                m["cost_usd"] += float(rec.get("cost_usd", 0.0) or 0.0)

    by_model_list = sorted(
        ({"model": k, **v} for k, v in by_model.items()),
        key=lambda x: x["cost_usd"], reverse=True,
    )
    by_day_list = sorted(
        ({"date": k, **v} for k, v in by_day.items()),
        key=lambda x: x["date"], reverse=True,
    )
    # Monats-Liste mit deutschem Label ("2026-07" → "Juli 2026"), neuester zuerst.
    _MONTHS_DE = [
        "Januar", "Februar", "März", "April", "Mai", "Juni",
        "Juli", "August", "September", "Oktober", "November", "Dezember",
    ]

    def _month_meta(ym: str) -> dict:
        """Deutsches Fallback-Label + (idx, year) für die Template-Lokalisierung."""
        try:
            y, m = ym.split("-")
            idx = int(m)
            return {"label": "{} {}".format(_MONTHS_DE[idx - 1], y), "month_idx": idx, "year": y}
        except (ValueError, IndexError):
            return {"label": ym, "month_idx": None, "year": None}

    by_month_list = sorted(
        ({"month": k, **_month_meta(k), **v} for k, v in by_month.items()),
        key=lambda x: x["month"], reverse=True,
    )
    return {
        "total_cost_usd": round(total_cost, 4),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_tokens": total_in + total_out,
        "n_runs": n_runs,
        "n_runs_no_usage": n_runs_no_usage,
        "by_model": by_model_list,
        "by_day": by_day_list,
        "by_month": by_month_list,
        "by_flow": by_flow,
    }
