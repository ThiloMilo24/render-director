"""render_director — Orchestrierungs-Kern der Render-Director-Pipeline.

Director → Generator → Validator → Advisor. Die FastAPI-Webapp (`app/`),
die CLI (`python -m render_director.pipeline`) und das pyRevit-Plugin
(`tools/pyrevit_export/`) nutzen dieselben Funktionen.
"""

__all__ = ["IterationResult", "run_iteration"]


def __getattr__(name):
    # Lazy-Import, damit `import render_director` keine API-SDKs lädt
    # (utils.py allein hat nur PIL/Pydantic-Dependencies).
    if name in {"IterationResult", "run_iteration"}:
        from render_director.pipeline import IterationResult, run_iteration

        return {"IterationResult": IterationResult, "run_iteration": run_iteration}[name]
    raise AttributeError(f"module 'render_director' has no attribute {name!r}")
