# `data/generations/`

Jeder Pipeline-Durchlauf landet hier in einem eigenen, sprechend benannten Ordner.

## Namensschema

```
YYYY-MM-DD_HHMMSS_sceneXX_modeY_runZZ/
```

Beispiel: `2026-04-15_143022_scene01_modeA_run03/`

- `sceneXX` — Scene-ID (matcht den Ordnernamen in `data/scenes/`)
- `modeY` — `A` (Kundenpräsentation), `B` (Wettbewerb), `C` (Stimmung)
- `runZZ` — laufender Zähler pro (Scene, Modus, Tag)

## Inhalt pro Run

| Datei | Zweck |
|---|---|
| `inputs.json` | Scene-ID, Modus, User-Anforderung, **Iterations-Block** (`type` + `parent_run_id` + `reason`), Modell-IDs, Zeitstempel |
| `director_reply.md` | Rohantwort vom Director-Agent (DE-Kommentar + EN-Prompt im Code-Block) |
| `final_prompt.txt` | Der aus der Director-Antwort extrahierte EN-Prompt, genau wie er an Nano Banana ging |
| `result.png` | Der generierte Render |
| `validator_reply.md` | Rohantwort vom Validation-Agent |
| `notes.md` | **Für dich** — manuelle Beobachtungen, Bewertung, Follow-ups |

## Git-Verhalten

Der gesamte Ordnerinhalt ist gitignored (nur `README.md` und `.gitkeep` committed). Generierte Bilder sind groß und teilweise vertraulich — sie gehören nicht ins Repo.
