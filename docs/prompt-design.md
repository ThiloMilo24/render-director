# Prompt design

All system prompts live in [`prompts/`](../prompts) as Markdown and are
read from disk on every call, so they can be tuned without restarting the
app. They are written in German because the users are German-speaking
architects; the image prompt the director produces is always English.

| File | Role |
|---|---|
| `director_system.md` | Director, exterior views |
| `director_interior_system.md` | Director, interior views |
| `director_adhoc_system.md` | Director for a single uploaded image, no BIM |
| `validator_system.md` / `validator_interior_system.md` / `validator_adhoc_system.md` | Validator variants with matching score schemas |
| `director_advisor_system.md` | Advisor: regenerate / refine / stop |

The location analysis prompt is inline in
`src/render_director/geo/site_analyzer.py`.

## The director's seven-block image prompt

Every image prompt has the same structure, so the validator report,
iterations and diffs between runs line up:

```
[SCENE TYPE & CAMERA]
[GEOMETRY LOCK — HARD CONSTRAINT]
[MATERIALS]
[LIGHTING & ATMOSPHERE]
[ENVIRONMENT & CONTEXT]     # interior: [SPATIAL & FURNISHING CONTEXT]
[STAFFAGE]
[STYLE REFERENCE]
[NEGATIVE CONSTRAINTS]
```

The code extracts the first fenced code block as the final prompt. If
there is none, the pipeline raises `DirectorNoPromptError` and tells
apart a follow-up question from a reply cut off by the token limit
(unbalanced fence).

## Rules that came out of testing

These rules are in the prompts because the generator failed without them:

- **Geometry is sacred.** The beauty render is ground truth for massing,
  openings and roof shape. The geometry lock is phrased as a hard
  constraint and repeated in the negative constraints.
- **Fixed and soft blocks.** Shortening prompts across the board caused
  geometry and style drift. Geometry lock and negative constraints are
  never shortened; lighting is fixed in mode A (client presentation);
  furnishing context is fixed when interior furniture is marked final.
  Other blocks may be condensed.
- **At most two issues per iteration.** The director addresses the
  failed item plus the most visible warning from the validator report.
  Mentioning everything again spreads the generator's attention.
- **Material anchors survive iterations.** In interior iterations the
  floor material line from the first prompt is always carried over.
  Dropping it made the generator fall back to a default CGI floor.
- **Placeholders are not design.** Enscape shows unmodelled context
  (neighbours, trees, mountains, cut terrain edges) as white volumes. The
  director replaces them with plausible regional context, unless the
  metadata says the grey volumes are real OpenStreetMap neighbours.
- **Interior exposure.** Views through windows are kept 1 to 1.5 stops
  below the interior light level; otherwise backlit windows blow out to
  white and read as a render artefact.
- **Don't invent landmarks.** With coordinates, vegetation, mountain
  character and light follow the region, but named peaks or lakes only
  appear if they come from OSM data.

## Modes

The user picks the mode up front instead of letting the model guess:

| Mode | Purpose | Director behaviour |
|---|---|---|
| A | Client presentation | Safe, inviting, at most one clarifying question |
| B | Competition | Asks for the core concept, bolder light and weather |
| C | Mood exploration | Three contrasting variants |
| D | Creative exploration | Geometry lock replaced by an explicit exploration block |

The ad-hoc director always does a single pass: the mode only changes
tone, never adds questions or variants.

## Keeping machine-parsed parts stable

Output language (German or Italian) is set by a directive at the top of
the user message, per role. Each directive states what must not be
translated:

- director: the English prompt and the `FORWARD_ATTACHMENTS` token
- validator: JSON keys and structure
- advisor: `ACTION` / `GRUND` / `KONFIDENZ` labels and their tokens

## Validator and advisor

The validator writes five sections for humans (geometry, material,
requirements, concrete edit instructions, what worked) and closes with a
JSON block of 1–5 scores per sub-dimension, `null` when not applicable.
Composition fidelity is a separate sub-dimension: an image can look
convincing and still invent a mountain range that is not in the input.

The advisor is a separate call with a strict three-line output and
explicit anti-loophole rules: no `stop` because the trend improved or the
score is "almost" there, no `refine` when there is a geometry issue, no
`regenerate` when only details are off.

## Regional default

The director prompts contain the placeholder `{{DEFAULT_REGION}}`. It is
replaced at load time from `RENDER_DEFAULT_REGION` (default
`Alpenraum`), so the regional assumptions can be changed without editing
the prompts.
