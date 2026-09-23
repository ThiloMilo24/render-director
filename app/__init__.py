"""Lokale FastAPI-Webapp — das Herzstück der Phase-4-Hybrid-Architektur.

Läuft auf localhost, wird vom pyRevit-`[Render Tool]`-Button via
`chrome --app`-Modus auf der rechten Bildschirmhälfte geöffnet. Bietet
die conversational Render-Iteration als Chat-UI; ruft intern
`render_director.pipeline.run_iteration` auf.
"""
