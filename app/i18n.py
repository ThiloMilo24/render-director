"""Leichtgewichtige UI-Übersetzung (de/it) für die Templates.

Kein gettext/Babel — nur ein Dict + eine `translate()`-Funktion, die in jedes
Template als `t("key")` injiziert wird (siehe Context-Processor in main.py).
Die Sprache kommt aus dem `ui_lang`-Cookie (Header-Umschalter), unabhängig von
`validator_language` (= Sprache der KI-Berichte). Fallback: fehlt ein Key/eine
Sprache → Deutsch → der Key selbst.

WICHTIG: Das betrifft NUR die Oberfläche. Die KI-Prompt-Sprache bleibt über
`validator_language` gesteuert; der Generator-Prompt ist immer Englisch.
"""
from __future__ import annotations

UI_LANGS = ("de", "it")
DEFAULT_UI_LANG = "de"

# key -> {"de": ..., "it": ...}. Gruppiert nach Oberfläche.
TRANSLATIONS: dict[str, dict[str, str]] = {
    # --- Header / Navigation ---
    "nav.snapshots": {"de": "Snapshots", "it": "Snapshot"},
    "nav.adhoc": {"de": "Sofort-Render", "it": "Render immediato"},
    "nav.cost": {"de": "Kosten", "it": "Costi"},
    "nav.language": {"de": "Sprache", "it": "Lingua"},

    # --- Sofort-Render-Seite (adhoc.html) ---
    "adhoc.title": {"de": "Sofort-Render", "it": "Render immediato"},
    "adhoc.subtitle": {
        "de": "Bild droppen oder waehlen, kurzen Wunsch dazu — Director schreibt den Prompt, Generator rendert, Validator bewertet. Ohne Revit/BIM.",
        "it": "Trascina o scegli un'immagine e aggiungi un breve desiderio — il Director scrive il prompt, il generatore renderizza, il validatore valuta. Senza Revit/BIM.",
    },
    "adhoc.saved_in": {"de": "Gespeichert in:", "it": "Salvato in:"},
    "adhoc.change_folder": {"de": "Ordner ändern…", "it": "Cambia cartella…"},
    "adhoc.history": {"de": "Iterations-Verlauf", "it": "Cronologia iterazioni"},
    "adhoc.filter": {"de": "Filter", "it": "Filtro"},
    "adhoc.filter_all": {"de": "Alle Tags", "it": "Tutti i tag"},
    "adhoc.filter_reset": {"de": "✕ zuruecksetzen", "it": "✕ azzera"},
    "adhoc.loading": {
        "de": "Director → Generator laeuft, ca. 30–60 s …",
        "it": "Director → generatore in corso, circa 30–60 s …",
    },
    "adhoc.cancel": {"de": "✕ Abbrechen", "it": "✕ Annulla"},
    "adhoc.drop_here": {
        "de": "Bild hier ablegen oder klicken zum Auswaehlen",
        "it": "Rilascia l'immagine qui o clicca per sceglierla",
    },
    "adhoc.drop_hint": {
        "de": "PNG, JPG, WEBP — beliebige Quelle (Enscape, Foto, Skizze, Screenshot)",
        "it": "PNG, JPG, WEBP — qualsiasi origine (Enscape, foto, schizzo, screenshot)",
    },
    "adhoc.change_image": {"de": "Klick zum Wechseln", "it": "Clicca per cambiare"},
    "adhoc.render_wish": {"de": "Render-Wunsch", "it": "Desiderio di render"},
    "adhoc.render_wish_ph": {
        "de": "z.B. fotorealistisch, goldene Stunde, Berghintergrund stimmig",
        "it": "es. fotorealistico, ora dorata, sfondo montano coerente",
    },
    "adhoc.project_tag": {"de": "Projekt-Tag (optional)", "it": "Tag di progetto (facoltativo)"},
    "adhoc.project_tag_ph": {
        "de": "z.B. wohnanlage-nord, hotel-x, exp-fassade",
        "it": "es. residenza-nord, hotel-x, exp-facciata",
    },
    "adhoc.project_tag_hint": {
        "de": "Iterationen mit gleichem Tag sind im Filter oben gruppierbar. Vererbt sich auf Refine/Regenerate.",
        "it": "Le iterazioni con lo stesso tag sono raggruppabili nel filtro in alto. Si eredita su Refine/Regenerate.",
    },
    "adhoc.location_context": {"de": "Standort-Kontext (optional)", "it": "Contesto di luogo (facoltativo)"},
    "adhoc.place_name": {"de": "Ortsname (optional)", "it": "Nome del luogo (facoltativo)"},
    "adhoc.location_hint": {
        "de": "Director nutzt Lat/Lon fuer regionale Stimmigkeit (Vegetation, Bergsilhouette, Lichtcharakter).",
        "it": "Il Director usa lat/lon per la coerenza regionale (vegetazione, profilo montano, carattere della luce).",
    },
    "adhoc.siteref_title": {
        "de": "Standort-Foto (Drohne / Maps / Site-Visit)",
        "it": "Foto del luogo (drone / Maps / sopralluogo)",
    },
    "adhoc.siteref_hint": {"de": "Drag-and-Drop oder Klick", "it": "Trascina o clicca"},
    "adhoc.siteref_pin": {
        "de": "Click ins Bild = Pin setzen · Click ausserhalb = Neues Bild waehlen",
        "it": "Clic sull'immagine = posiziona spillo · Clic fuori = scegli nuova immagine",
    },
    "adhoc.render_btn": {"de": "Rendern", "it": "Renderizza"},

    # --- Optionen (adhoc + snapshot) ---
    "opt.mode": {"de": "Modus", "it": "Modalità"},
    "opt.mode_a": {"de": "A (Präsentation)", "it": "A (Presentazione)"},
    "opt.mode_b": {"de": "B (Wettbewerb)", "it": "B (Concorso)"},
    "opt.mode_c": {"de": "C (Stimmung)", "it": "C (Atmosfera)"},
    "opt.mode_d": {"de": "D (Kreativ, geometrie-frei)", "it": "D (Creativo, senza geometria)"},
    "opt.language": {"de": "Sprache", "it": "Lingua"},
    "opt.generator": {"de": "Generator", "it": "Generatore"},
    "opt.thinking": {"de": "Thinking", "it": "Thinking"},

    # --- Chat-Bubble (Ergebnis) ---
    "bubble.input": {"de": "Input", "it": "Input"},
    "bubble.result": {"de": "Result", "it": "Risultato"},
    "bubble.edited_base": {"de": "Basis (bearbeitet)", "it": "Base (modificata)"},
    "reflib.badge": {"de": "Referenzbibliothek", "it": "Libreria di riferimento"},
    "reflib.title": {
        "de": "Fotos aus der Referenzbibliothek gingen als Stil-Referenz an den Generator "
              "(Projektname passte zu einem Ordner in der Bildbibliothek).",
        "it": "Foto della libreria di riferimento sono state usate come riferimento di "
              "stile per il generatore (il nome del progetto corrisponde a una "
              "cartella nella libreria immagini).",
    },
    "bubble.validating": {
        "de": "Validator + Advisor prüfen das Bild … (läuft im Hintergrund)",
        "it": "Validatore + Advisor stanno valutando l'immagine … (in background)",
    },
    "bubble.show_validator": {"de": "Validator-Report anzeigen", "it": "Mostra report del validatore"},
    "bubble.show_director": {
        "de": "Director-Reply + Final-Prompt anzeigen",
        "it": "Mostra risposta del Director + prompt finale",
    },
    "bubble.delete": {"de": "✕ loeschen", "it": "✕ elimina"},
    "bubble.delete_confirm": {
        "de": "Diesen Sofort-Render unwiderruflich loeschen?",
        "it": "Eliminare definitivamente questo render immediato?",
    },
    "bubble.feedback_ph": {
        "de": "Was soll diesmal anders sein? (bei Inpaint: was kommt in die markierte Region)",
        "it": "Cosa deve cambiare stavolta? (per Inpaint: cosa va nella regione selezionata)",
    },
    "bubble.refine": {"de": "✎ Refine", "it": "✎ Affina"},
    "bubble.regenerate": {"de": "↻ Regenerate", "it": "↻ Rigenera"},
    "bubble.location_context": {"de": "Standort-Kontext", "it": "Contesto di luogo"},

    # --- Inpaint-Editor ---
    "inpaint.toggle": {"de": "◱ Region markieren (Inpaint)", "it": "◱ Seleziona regione (Inpaint)"},
    "inpaint.gpt_only": {"de": "nur GPT-Backend", "it": "solo backend GPT"},
    "inpaint.paint": {"de": "🖌 Malen", "it": "🖌 Dipingi"},
    "inpaint.erase": {"de": "🩹 Radieren", "it": "🩹 Cancella"},
    "inpaint.brush": {"de": "Pinsel", "it": "Pennello"},
    "inpaint.reset": {"de": "✕ Zurücksetzen", "it": "✕ Azzera"},
    "inpaint.hint": {
        "de": "Übermale die Fläche, die neu gerendert werden soll, und beschreibe oben, was reinkommt. Der Rest bleibt unangetastet.",
        "it": "Colora l'area da rigenerare e descrivi sopra cosa deve andarci. Il resto resta intatto.",
    },
    "inpaint.submit": {"de": "◱ Inpaint", "it": "◱ Inpaint"},

    # --- Score-Block ---
    "score.confidence": {"de": "Konfidenz", "it": "Affidabilità"},
    "score.rec_regenerate": {"de": "↻ Regenerate empfohlen", "it": "↻ Rigenerazione consigliata"},
    "score.rec_refine": {"de": "✎ Refine empfohlen", "it": "✎ Affinamento consigliato"},
    "score.rec_stop": {"de": "✓ Stop — Render passt", "it": "✓ Stop — il render va bene"},
    "score.user_decides": {"de": "(User entscheidet)", "it": "(decide l'utente)"},

    # --- Attachment-Zone ---
    "att.title": {"de": "Visuelle Anhänge (max 3)", "it": "Allegati visivi (max 3)"},
    "att.drop": {
        "de": "Bilder hierher ziehen oder klicken zum Auswählen",
        "it": "Trascina qui le immagini o clicca per sceglierle",
    },
    "att.hint": {"de": "PNG / JPG, max 3 Bilder", "it": "PNG / JPG, max 3 immagini"},
    "att.desc_ph": {
        "de": "Was zeigen die Anhänge? (optional, z.B. „der Stuhl links und die Wand-Schraffur sollen so aussehen“)",
        "it": "Cosa mostrano gli allegati? (facoltativo, es. „la sedia a sinistra e la texture del muro devono essere così“)",
    },

    # --- Kosten (cost.html) ---
    "cost.title": {"de": "Kosten & Token-Verbrauch", "it": "Costi e consumo di token"},
    "cost.est_cost": {"de": "Geschätzte Kosten", "it": "Costi stimati"},
    "cost.total_tokens": {"de": "Tokens gesamt", "it": "Token totali"},
    "cost.avg_per_run": {"de": "Ø Kosten / Run", "it": "Ø costo / render"},
    "cost.by_month": {"de": "Nach Monat", "it": "Per mese"},
    "cost.by_model": {"de": "Nach Modell", "it": "Per modello"},
    "cost.by_day": {"de": "Nach Tag", "it": "Per giorno"},
    "cost.running": {"de": "laufend", "it": "in corso"},
    "cost.month": {"de": "Monat", "it": "Mese"},
    "cost.model": {"de": "Modell", "it": "Modello"},
    "cost.date": {"de": "Datum", "it": "Data"},
    "cost.calls": {"de": "Calls", "it": "Chiamate"},
    "cost.runs": {"de": "Runs", "it": "Render"},
    "cost.tokens": {"de": "Tokens", "it": "Token"},
    "cost.cost": {"de": "Kosten", "it": "Costi"},
    "cost.runs_recorded": {"de": "Runs erfasst", "it": "render registrati"},
    "cost.empty": {
        "de": "Noch keine Runs mit Kosten-Daten. Neue Runs (Snapshot + Sofort-Render) werden ab jetzt automatisch erfasst — diese Seite füllt sich danach.",
        "it": "Ancora nessun render con dati di costo. I nuovi render (Snapshot + Render immediato) vengono registrati automaticamente d'ora in poi — questa pagina si popolerà.",
    },
    # Monatsnamen (für die "Nach Monat"-Tabelle)
    "month.1": {"de": "Januar", "it": "Gennaio"},
    "month.2": {"de": "Februar", "it": "Febbraio"},
    "month.3": {"de": "März", "it": "Marzo"},
    "month.4": {"de": "April", "it": "Aprile"},
    "month.5": {"de": "Mai", "it": "Maggio"},
    "month.6": {"de": "Juni", "it": "Giugno"},
    "month.7": {"de": "Juli", "it": "Luglio"},
    "month.8": {"de": "August", "it": "Agosto"},
    "month.9": {"de": "September", "it": "Settembre"},
    "month.10": {"de": "Oktober", "it": "Ottobre"},
    "month.11": {"de": "November", "it": "Novembre"},
    "month.12": {"de": "Dezember", "it": "Dicembre"},

    "cost.footnote": {
        "de": "Kosten sind Schätzungen aus einer statischen Preistabelle. Tokens stammen exakt aus den API-Antworten. Für Abrechnungsgenauigkeit die offiziellen Provider-Dashboards nutzen.",
        "it": "I costi sono stime da una tabella prezzi statica. I token provengono esattamente dalle risposte API. Per l'accuratezza contabile usare i dashboard ufficiali dei provider.",
    },
}


def translate(key: str, lang: str = DEFAULT_UI_LANG) -> str:
    """key → Text in `lang`; Fallback lang→de→key."""
    entry = TRANSLATIONS.get(key)
    if not entry:
        return key
    return entry.get(lang) or entry.get(DEFAULT_UI_LANG) or key


def ui_lang_from_request(request) -> str:
    """Liest die UI-Sprache aus dem `ui_lang`-Cookie (Default de)."""
    lang = request.cookies.get("ui_lang", DEFAULT_UI_LANG)
    return lang if lang in UI_LANGS else DEFAULT_UI_LANG


# --- Provider-Budget-/Quota-Fehler erkennen --------------------------------
# Wenn ein Provider wegen erreichtem Monatslimit / leerem Guthaben nicht mehr
# liefert, kommt eine SDK-Exception hoch (openai.*, anthropic.*, google.*). Die
# Endpoints fangen sie ab und zeigen statt eines kryptischen 500ers eine
# freundliche Bubble ("Monatslimit für … erreicht"). Bewusst tolerant: lieber
# einmal zu viel als kryptisch — Detailtext steht zusätzlich in der Bubble.

_BUDGET_PROVIDERS = {
    "openai": {"name": "ChatGPT / OpenAI (Generator GPT)", "dash": "platform.openai.com → Billing → Limits"},
    "anthropic": {"name": "Claude / Anthropic (Director + Validator + Advisor)", "dash": "console.anthropic.com → Billing"},
    "google": {"name": "Gemini / Google (Generator NB1/NB2)", "dash": "Google Cloud → Billing / AI Studio"},
}

# Starke Hinweise auf Budget/Guthaben/Quota im Fehlertext (klein geschrieben).
_BUDGET_SIGNALS = (
    "insufficient_quota", "exceeded your current quota", "quota exceeded",
    "credit balance is too low", "insufficient credit", "out of credit",
    "billing", "budget", "spend limit", "payment required", "hard limit",
    "resource_exhausted", "resource exhausted",
)


def classify_provider_budget_error(exc, lang: str = DEFAULT_UI_LANG):
    """Erkennt Budget-/Quota-Fehler der drei Provider. Gibt `{title, hint}` in
    der UI-Sprache zurück oder `None`, wenn es kein Budget-Fehler ist (dann
    lässt der Caller die Exception normal weiterlaufen)."""
    mod = (getattr(type(exc), "__module__", "") or "").lower()
    parts = [str(exc)]
    for attr in ("message", "body"):
        v = getattr(exc, attr, None)
        if v:
            parts.append(str(v))
    msg = " ".join(parts).lower()
    status = getattr(exc, "status_code", None)

    if mod.startswith("openai") or "openai" in mod:
        provider = "openai"
    elif mod.startswith("anthropic") or "anthropic" in mod:
        provider = "anthropic"
    elif "google" in mod or "genai" in mod or "generativelanguage" in msg:
        provider = "google"
    elif "claude" in msg or "anthropic" in msg:
        provider = "anthropic"
    elif "openai" in msg:
        provider = "openai"
    else:
        provider = None

    is_budget = (status == 402) or any(s in msg for s in _BUDGET_SIGNALS)
    if not provider or not is_budget:
        return None

    p = _BUDGET_PROVIDERS[provider]
    if lang == "it":
        return {
            "title": "Limite raggiunto: {} non risponde più".format(p["name"]),
            "hint": (
                "L'API di {} restituisce un errore di budget/credito — "
                "probabilmente è stato raggiunto il limite mensile o il credito "
                "è esaurito. Controlla/aumenta il budget nel dashboard ({}), poi "
                "riavvia il render. L'immagine non è stata generata."
            ).format(p["name"], p["dash"]),
        }
    return {
        "title": "Limit erreicht: {} liefert nicht mehr".format(p["name"]),
        "hint": (
            "Die API von {} meldet einen Budget-/Guthaben-Fehler — vermutlich "
            "ist das Monatslimit erreicht oder das Guthaben aufgebraucht. Bitte "
            "im Dashboard ({}) das Budget prüfen/erhöhen bzw. Guthaben aufladen "
            "und den Render erneut starten. Es wurde kein Bild erzeugt."
        ).format(p["name"], p["dash"]),
    }
