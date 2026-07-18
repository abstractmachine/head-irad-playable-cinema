"""Reusable Visualizer Framework building blocks.

This package defines the shared UI language used by Crossing visualizers.
Visualizers are expected to compose these components rather than implementing
their own browsing infrastructure.

Architecture
------------
State ownership is intentionally single-source:

- Browser owns selection, filtering, pagination, keyboard navigation,
  drag-and-drop, and collection browsing.
- Source owns records and data access.
- Inspector owns presentation of the selected item.
- Services own business logic.
- CLI commands remain the canonical project operations.
- Metadata files own persistent project state.

Visualizers should invoke existing services/CLI operations from inspector
controls rather than duplicating business logic in UI code.

Shared components
-----------------
- IllustrationBrowser
- IllustrationSource (and source subclasses)
- IllustrationInspector
- ThumbnailCell
- ThumbnailLoader
- MetadataBlock
- CollapsibleSection
- IpcServer
- styles.theme tokens/helpers
"""
