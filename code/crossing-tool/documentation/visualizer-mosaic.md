# Mosaic

![Mosaic Visualizer screenshot](./images/visualizers/visualizer-mosaic.png)

The `Mosaic` visualizer is a live visual search browser. Enter a query and it displays a scrollable grid of matching shot frames extracted directly from the video files.

Open it with:

```
crossing visualizer mosaic
```

## Layout

### Left — Image Grid

A scrollable, zoomable mosaic of video frame thumbnails. Each tile is captioned with the film title and year. Tiles reflow automatically when the window is resized.

- **Zoom**: `Ctrl` + scroll wheel
- **Pan**: scroll wheel or scrollbars

### Right — Control Panel

#### Scope

Select the media type (`movie`, `gameplay`) and optionally restrict results to a single title. Use **<all>** to search across the entire library.

#### Annotation Field

The annotation field to search against (e.g. `objects`, `wearing`, `action`, `humans`).

#### Search Query

Type a search term and click one of the search mode buttons:

| Button | Description |
|---|---|
| **Search** | Full-text search across the selected field |
| **Best** | Return only the single best-matching shot per film |
| **Shots** | Search at shot granularity (default) |
| **Scenes** | Group results by scene |
| **PDF** | Generate and save a mosaic PDF |
| **Video** | Export a video montage of matched shots |

#### Options

| Option | Description |
|---|---|
| Limit | Maximum number of results (default 50) |
| Limit per movie | Cap results to this many shots per film |
| FPS | Frame rate for video export |
| Dur (s) | Duration per shot in video export |

#### Vocabulary

A worker-loaded list of the most frequent indexed terms in the selected annotation field. The list defaults to **<all>** when Mosaic opens and whenever the Annotation Field changes. Use the Vocabulary dropdown to show **<all>** terms or jump to one initial letter. Click any term to place it in the Search Query field.

Large **<all>** lists are built in background-sized GUI batches: the dropdown and Vocabulary loading indicator remain visible, while the completed table is laid out once to avoid repeatedly resizing the Inspector. If the vocabulary index is missing or stale, Mosaic shows that state instead of scanning annotations; use **Rebuild Vocabulary** in the Tools section to rebuild it through the canonical CLI index command.

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `Home` | Previous title in scope |
| `End` | Next title in scope |
| `PgUp` / `PgDn` | Previous / next annotation field |
| `Escape` / `Ctrl+Q` / `Ctrl+W` | Close |

## Requirements

Video files must be present under `media/videos/<media_type>/`. Annotation data is read from `data/annotations/`.
