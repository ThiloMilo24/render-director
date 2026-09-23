# Evaluation

Iterating on prompts only makes sense if the effect of a change is
measurable. The validator therefore produces two layers per run.

## Layer 1: report for humans

Five Markdown sections. The first three carry a three-level rating in the
heading:

| Section | Rating |
|---|---|
| Geometry and composition | ✓ correct / ⚠ minor deviations / ✗ significantly wrong |
| Material | ✓ / ⚠ / ✗ |
| Requirements (mood, time of day, mode) | ✓ / ⚠ / ✗ |
| Concrete improvements | 1–3 edit instructions the director can apply directly |
| What worked | Positive feedback |

The director reads this report in the next iteration.

Three levels are too coarse for comparisons: in early test runs,
clearly different detail problems (roof shape, concrete texture, a
leftover placeholder volume) all ended up as "⚠" at section level.

## Layer 2: JSON scores for the pipeline

Every report ends with a fenced `json` block, one score from 1 to 5 per
sub-dimension (`null` if not visible or not relevant):

| Section | Exterior sub-dimensions | Interior sub-dimensions |
|---|---|---|
| `geometrie` | massing, proportionen, fensterachsen_tueren, dachform, elemente_komplett, komposition_treue | raumproportionen, wandflaechen, deckenhoehe, tueroeffnungen, einbauten_position |
| `material` | fassade, dach, fenster, boden_paving | wandbelag, deckenfinish, bodenbelag_innen, moebelmaterial, fensterleibung |
| `anforderung` | atmosphaere, tageszeit_licht, staffage, modus_passung, weisse_placeholder_ersetzt | raumstimmung, lichtszenario, moeblierungs_vollstaendig, modus_passung, default_objekte_ersetzt |

The schemas are defined once in `src/render_director/utils.py`
(`VALIDATOR_SCORE_SCHEMA`, `VALIDATOR_SCORE_SCHEMA_INTERIOR`) and
repeated verbatim in the validator prompts. The ad-hoc validator uses
the exterior schema so parsing and UI stay identical.

## Parsing and aggregation

| Function | Purpose |
|---|---|
| `parse_validator_scores_json(reply)` | Last `json` block, `None` if missing or malformed |
| `flatten_validator_scores(scores, schema)` | `{"geometrie.dachform": 4, ...}`, missing keys filled with `None`, unexpected keys kept |
| `mean_validator_score(scores, section=None, schema)` | Mean over non-null values, overall or per section |
| `add_score_summary_to_inputs(run_dir, reply, schema)` | Writes `final_score` and `section_means` into `inputs.json` |
| `aggregate_validator_scores([...], schema)` | Per sub-dimension `mean`, `min`, `max`, `n_valid`, `n_null` over several samples |

A missing score block never becomes a silent zero: the fields are simply
not written.

## Comparing prompt variants

Generator and validator are both stochastic, so single runs are
anecdotes. The working rule was: at least three samples per variant, then
compare mean differences on the 1–5 scale:

| Δ mean | Reading |
|---|---|
| > 0.8 | Robust effect |
| 0.4 – 0.8 | Indication; decide by looking at the images |
| < 0.4 | No signal |

Variance per sub-dimension is typically around ±1 point, so these
thresholds are rough and meant for N = 3.

## Known limits

- **Validator hallucination.** With low-detail beauty renders the
  validator can infer geometry that is not there. The depth pass is
  passed along whenever it exists to anchor it.
- **Cost of sampling.** Every sample is a generator plus a validator
  call; comparisons multiply quickly.
- **Scores are per scene.** Scenes differ in difficulty, so scores are
  compared within a scene, not across scenes.
- **Validator self-consistency is not measured yet.** A useful next test:
  one fixed image, N validator calls, score variance.
