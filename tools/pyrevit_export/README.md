# pyRevit extension: Render Director button

This folder is a pyRevit extension that connects Revit to the render
pipeline. It adds a **Render Director** tab with a single button.

## What the button does

Clicking the button asks for one of two paths:

**A) Enscape render (BIM).** Render-first workflow: render the view in
Enscape, switch back to Revit, click the button.

1. Reads the active view and the project path from Revit.
2. Finds the newest render in the configured Enscape output folder
   (last 24 hours). If none is found, a file picker opens.
3. Shows a confirmation dialog with source files and target folder.
4. Writes two JSON files into
   `<project folder>/Revit Render/<snapshot id>/`:
   - `materials_legend.json`: materials of the elements visible in the
     active view (name, shading colour, class, description, appearance
     asset). Filtering by view keeps the list small instead of exporting
     every material in the model.
   - `meta.json`: project info, site location (lat/lon, elevation, time
     zone), camera parameters, material names.
5. Moves the beauty image and the Enscape passes (`MaterialID`,
   `ObjectID`, `Depth`) into the same folder.
6. Opens the web app on that snapshot via the shared launcher.

The snapshot id is `<rvt file name>_view<N>`. The view number is stored
in `Revit Render/_view_index.json`, so exporting the same view again
reuses its folder. If a snapshot is already complete, the button offers
to open it instead of exporting again.

**B) Ad-hoc (without BIM).** Opens the web app on the ad-hoc page to
upload and edit a single image. The same path is available without
Revit through `tools/launcher/Render Director.cmd`.

The web app loads the exported `meta.json` directly:
`load_scene_bundle` detects the pyRevit shape and maps it to
`SceneMetadata` (see `_adapt_pyrevit_meta` in
`src/render_director/utils.py`).

## Installation

Requirements: Windows, Revit with [pyRevit](https://github.com/pyrevitlabs/pyRevit/releases),
Google Chrome, and a set-up repository (run `install.cmd` in the repo
root once; it creates `.venv` and asks for the API keys).

1. In Revit: tab **pyRevit** → **Settings** → **Custom Extension
   Directories** → **+**.
2. Add the path of this folder (`<repo>/tools/pyrevit_export`). The
   folder must contain `Render Director.extension/`.
3. **Save Settings and Reload**. A new tab **Render Director** appears.

The button finds the repository by walking up from the script to the
first `pyproject.toml`. If the extension is copied elsewhere, set
`repo_dir` in the config file (see below).

## Configuration

On first use the button asks for the Enscape output folder and stores
it in `%APPDATA%\render_director\config.json`:

```json
{
  "enscape_output_dir": "C:\\Users\\<user>\\Documents\\Enscape",
  "repo_dir": "C:\\path\\to\\repo"
}
```

`repo_dir` is optional. Delete the file to be asked again.

In Enscape, enable the ID and depth channels once:
**Visual Settings → Output → Image → export object ID, material ID,
depth and alpha channels**.

## Launcher

`tools/launcher/launch.py` is the single entry point used by the button
and the desktop shortcut. It checks `GET /health` on
`localhost:8765`, starts uvicorn in the background if needed (logs in
`logs/webapp.log`) and opens Chrome in `--app` mode on the right half
of the primary screen. `Render Director beenden.cmd` stops the server.

## Known limits

- The Enscape render itself is not triggered automatically. Light and
  camera settings stay under the user's control.
- Multi-monitor setups use the primary screen.
- The script runs in pyRevit's default engine and avoids Python 3-only
  syntax.

## Troubleshooting

- **Tab missing after reload:** check the path in pyRevit settings,
  restart Revit.
- **Button shows an error:** open the pyRevit output window and copy
  the stack trace.
- **"Project not saved" dialog:** save the Revit project first; the
  export needs a project folder.
