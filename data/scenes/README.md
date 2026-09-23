# `data/scenes/`

Input scenes for the pipeline. One folder per camera view. A scene is
either exported by the pyRevit button (see
[`tools/pyrevit_export`](../../tools/pyrevit_export/README.md)) or created
by hand from an Enscape export.

Everything below `data/scenes/<scene>/` is gitignored. Two synthetic
example scenes live in [`examples/scenes`](../../examples/scenes); run the
app with `RENDER_SNAPSHOT_ROOT=examples/scenes` to use them.

## Layout

```
data/scenes/ProjectA_Lobby/
├── ProjectA_Lobby_Beauty.jpg        # required: Enscape beauty render
├── ProjectA_Lobby_Depth.png         # recommended: depth pass
├── ProjectA_Lobby_Material_ID.png   # optional: material ID pass
├── ProjectA_Lobby_Object_ID.png     # optional: object ID pass
└── meta.json                        # required: SceneMetadata
```

The loader (`render_director.utils.load_scene_bundle`) is tolerant:

- Pass suffixes (`_Beauty`, `_Depth`, `_Material_ID`, `_Object_ID`) are
  matched case-insensitively and ignore `-`/`_`, so `_MaterialID`,
  `_material-id` and `_materialid` all work.
- File extension: `.png`, `.jpg` or `.jpeg`.
- A bare `beauty.jpg` also works and wins over a prefixed match.
- Two candidates for the same pass in one folder are a hard error.
  Two camera positions means two scene folders.

Optional extras: `site_reference.{png,jpg}` (drone photo or map
screenshot of the site) and `site_analysis.md` (cached location analysis,
written by `render_director.geo.site_analyzer`).

## Why the depth pass is recommended

Without it, the validator tends to infer geometry from shadows and
placeholder volumes and sometimes reports roof pitches or massing that
are not in the model. The depth pass is a spatial anchor: it is passed
to the generator and the validator whenever it exists.

The material and object ID passes go to the director and the validator
only. Their colours are arbitrary region labels; a generator would
render them as real colours.

## `meta.json`

Validated by `SceneMetadata` in `src/render_director/utils.py`.

| Field | Type | Example |
|---|---|---|
| `project_name` | str | `"Residential Complex North"` |
| `project_type` | str | `"Wohnbau"` |
| `location` | str | `"Innsbruck, Österreich"` |
| `facade_orientation` | str | `"Süd-Südwest"` |
| `materials` | dict[str, str] | `{"Fassade": "Sichtbeton, Brettschalung", "Dach": "Zink Stehfalz"}` |
| `camera_focal_length_mm` | float | `24.0` |
| `camera_height_m` | float | `1.6` |
| `time_of_day` | str | `"goldene Stunde"` |
| `view_type` | `"exterior"` \| `"interior"` | selects prompts and score schema, default `"exterior"` |
| `program_type` | enum | interior only: `lobby`, `wohnraum`, `hotelzimmer`, `restaurant`, `kueche`, `spa`, `bar`, `konferenz`, `sauna`, `sonstiges` |
| `furniture_state` | `"placeholder"` \| `"final"` | interior only: may the director replace furniture? |
| `site_context` | object | Enscape site context status, see below |
| `site_location` | object | optional: `latitude_deg`, `longitude_deg`, `elevation_m`, `time_zone_utc_offset_h`, `place_name` |
| `site_reference_marker` | object | optional: `x_pct`, `y_pct` of the building position on `site_reference.*` |
| `notes` | str | free text, goes into the director prompt |

Field values are German because the prompts are German. The
director reads them as free text.

### `site_context`

```json
"site_context": {
  "active": true,
  "source": "enscape_osm",
  "neighbors_visible": true,
  "notes": "Neighbouring farm to the south from OSM"
}
```

When `neighbors_visible` is true, grey volumes in the beauty render are
real neighbouring buildings and must not be replaced by invented
context.

### Interior scenes

`view_type: "interior"` switches to `director_interior_system.md`,
`validator_interior_system.md` and the interior score schema.
`program_type` selects the default light profile for mode A;
`furniture_state: "final"` extends the geometry lock to furniture.
Interior `materials` use building-part keys:

```json
"materials": {
  "Wandbelag": "Kalkputz warmweiß",
  "Deckenfinish": "Lärchen-Schalung sichtbar, geölt",
  "Bodenbelag": "Eiche breitdielig, geölt",
  "Möbel": "Polsterung Naturleinen, Tische massiv Eiche",
  "Fensterleibung": "Eiche, tief"
}
```
