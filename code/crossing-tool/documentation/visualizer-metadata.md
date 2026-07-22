# Metadata

![Metadata Visualizer screenshot](./images/visualizers/visualizer-metadata.png)

The `Metadata` visualizer is a canonical two-pane browser:

- left: tabbed thumbnail browser
- right: single `Info` inspector

The browser has two tabs: **Movies** and **Gameplay**. Selecting a thumbnail updates the inspector.

Open it with:

```
crossing visualizer metadata
```

## Layout

The browser shows a grid of thumbnails using the canonical Crossing thumbnail cell styling. Movies use poster images; gameplay uses first-frame or existing gameplay thumbnails.

The inspector contains one collapsible section, `Info`, using the shared inspector table contract.

## Navigation

Use the `Movies` and `Gameplay` tabs to switch between datasets. Arrow keys move selection within the active grid, and the window keeps the canonical `TAB` / `SHIFT+TAB` shortcuts used by the other visualizers.

## Requirements

Thumbnails are read from `media/thumbnails/<media_type>/`. Metadata is read from `data/metadata/`.
