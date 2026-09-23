"""Referenzbibliothek — Retrieval-Layer für den Render-Director.

Tier 0 (dieses Modul, `library.py`): **Metadaten-Match**, kein Embedding,
kein Vektor-Store. Nutzt die strukturierten Filename-Tags kuratierter
Projektfotos (`RENDER_REFERENCE_LIBRARY_ROOT`) — Material + Tageszeit —
und liefert die ähnlichsten Fotos als Stil-Referenzen, die der
Generator via `forwarded_attachments` sieht.

Tier 1 (geplant): CLIP-Embeddings + Vektor-Store für
„sieht-aus-wie"-Ähnlichkeit statt nur Tag-Match. Kommt hier als
`search.py`/`store.py` dazu, ohne Tier 0 zu ersetzen.
"""
from render_director.rag.library import (
    LibraryImage,
    build_index,
    find_similar_for_snapshot,
)

__all__ = ["LibraryImage", "build_index", "find_similar_for_snapshot"]
