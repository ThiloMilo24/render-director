"""Geo-Referenz-Modul (Phase 5, 2026-05-27).

Holt Standort-Kontext aus offenen Quellen (Wikimedia Commons + OSM
Overpass) und lässt Claude eine regionale Charakteristik daraus
ableiten. Ergebnis wird pro Snapshot als `site_analysis.md` gecacht
und vom Director als zusätzlicher GEO-KONTEXT-Block genutzt.

Lizenz-bewusst:
- Wikimedia-Bilder werden nur intern (Claude-Vision-Analyse) verarbeitet,
  nicht an den Generator weitergegeben. Bild-Sources werden geloggt.
- OSM-Daten sind unter ODbL frei nutzbar.
- Kein Mapillary / Google Street View (Lizenz- bzw. ToS-Probleme).
"""
