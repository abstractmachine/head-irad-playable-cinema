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

Select **<Media>** to search both `movie` and `gameplay`, or select one media
type and optionally restrict results to a single title. Use **<all>** to search
all titles in the selected media scope.

#### Field

The annotation field to search against (e.g. `objects`, `wearing`, `action`, `humans`).

#### Search

Type a search term and click one of the search mode buttons:

| Button | Description |
|---|---|
| **Search** | Full-text search across the selected field |
| **Clear** | Remove current browser results; available only when results are shown |
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

A worker-loaded list of indexed terms. The selected annotation field determines the
source automatically: structured fields use canonical vocabulary, while
`description` and `text` use derived free-text vocabulary. Selecting **<all>**
merges both indexed families. Derived terms retain their free-text
provenance and never enter the canonical artifact. The list defaults to **<all>**
when Mosaic opens and whenever the selected browse field changes. Use the Vocabulary
sort dropdown to order terms by count, alphabetically, or by count with alphabetical
ordering inside each equal-count group. Use the following dropdown to show **<all>**
terms or jump to one initial letter. Click any term to place it in the Search
field.

Large **<all>** lists are built in background-sized GUI batches: the dropdown and Vocabulary loading indicator remain visible, while the completed table is laid out once to avoid repeatedly resizing the Inspector. If the vocabulary index is missing or stale, Mosaic shows that state instead of scanning annotations; use **Rebuild Vocabulary** in the Tools section to rebuild it through the canonical CLI index command.

Canonical and derived browse vocabulary are distinct from the typed free-text
**Search** workflow and from semantic retrieval. Searching a selected term still uses
the normal Mosaic annotation search; choosing an annotation field does not trigger
embedding retrieval.

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `Home` | Previous title in scope |
| `End` | Next title in scope |
| `PgUp` / `PgDn` | Previous / next annotation field |
| `Escape` / `Ctrl+Q` / `Ctrl+W` | Close |

## Requirements

Video files must be present under `media/videos/<media_type>/`. Annotation data is read from `data/annotations/`.
