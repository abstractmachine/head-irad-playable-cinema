"""Backward-compatibility shim — Silhouette Visualizer renamed to Illustration Visualizer.

Existing importers (book_visualizer, project_visualizer, cli.py) continue to work.
"""
from visualizers.illustration_visualizer import (  # noqa: F401
    IllustrationWindow   as SilhouetteWindow,
    IllustrationPane     as CatalogBrowser,
    open_at_illustration as open_at_silhouette,
    run_visualizer,
    SAMExplorer,
)
