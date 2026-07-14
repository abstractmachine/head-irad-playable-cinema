"""Reusable visualizer framework components.

This package provides the shared building blocks for Crossing Tool visualizers.

Components
----------
IllustrationBrowser
    Primary reusable component.  Owns the item collection, filter state,
    selection, pagination, and keyboard navigation.  Contains an
    IllustrationView for rendering.

IllustrationInspector
    Inspects the currently selected illustration.  Extensible via Qt layouts;
    each visualizer adds its own sections.

MetadataBlock
    Compact key-value display widget used as a building block inside
    inspector panels.

ThumbnailLoader
    Background QThread that loads PNG thumbnails and emits QImages to the
    GUI thread, keeping the UI responsive during large catalog scans.

ThumbnailCell
    Single thumbnail cell widget — fixed size, selectable, draggable.

IpcServer
    Base QThread for Unix-domain socket IPC servers used for single-instance
    window management.

Usage
-----
All components import cleanly from this package::

    from visualizers.components.illustration_browser import IllustrationBrowser
    from visualizers.components.illustration_inspector import IllustrationInspector
    from visualizers.components.metadata_block import MetadataBlock
    from visualizers.components.thumbnail_loader import ThumbnailLoader
    from visualizers.components.thumbnail_cell import ThumbnailCell
    from visualizers.components.ipc_server import IpcServer
"""
