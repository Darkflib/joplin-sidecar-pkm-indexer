"""pkm_sidecar — local-first read-only Joplin PKM indexer and dashboard.

``__version__`` is the single source of truth for the service version, surfaced
by ``GET /health`` and ``GET /api/status`` (PRD §13.1, §13.2).
"""

__version__ = "0.2.2"

__all__ = ["__version__"]
