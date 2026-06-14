# Crossing

A CLI + GUI tool for relating moving images across media — connecting gameplay sequences to cinema, live gameplay input to archived material. Manages a local project folder as its database. All models run locally; no external services required.

---

## Commands at a Glance

| Command | What it does |
|---------|-------------|
| `crossing tool` | Configure project path, name, models, and API keys |
| `crossing media import` | Import video files into the project (movies or gameplay) |
| `crossing media remove` | Remove a film and all its associated data |
| `crossing media audit` | Report missing metadata, thumbnails, shotlists, and subtitles |
| `crossing media update` | Fetch and save metadata/thumbnails for entries missing key fields |
| `crossing media normalize` | Measure loudness and save one playback gain (`audio_gain_db`) per asset |
| `crossing media subtitle` | Fetch and list subtitles via OpenSubtitles |
| `crossing metadata` | List, get, update, and audit metadata |
| `crossing shotlist` | Manage shot/scene CSV files and run shot/scene detection |
| `crossing annotate` | Run LLM annotation on shots and scenes |
| `crossing annotate frame` | Find the best matching frame per shot using CLIP (requires prior `annotate shot` pass) |
| `crossing search` | Search shot annotations by query, field, or vocabulary |
| `crossing index` | Build and maintain the semantic search index (txt + embeddings) |
| `crossing index vocabulary` | Build a cached per-field vocabulary index from annotation JSON |
| `crossing generate mosaic` | Generate contact-sheet grids from thumbnails or search results |
| `crossing generate mosaic search` | Mosaic of frames matching a shot annotation search query |
| `crossing generate mosaic export` | Export individual JPEG frames for each search result |
| `crossing generate mosaic shots` | PDF contact sheet of every shot in a movie (one frame per shot) |
| `crossing generate mosaic scenes` | PDF contact sheet of shots grouped by scene, with title and scene-number intertitles |
| `crossing generate composition` | Build a single tableau image from a semantic search result |
| `crossing generate cloud` | Generate a word-cloud PDF from annotation text |
| `crossing visualizer` | Open the project launcher — configure path, models, and open any visualizer |
| `crossing visualizer project` | Same as above (explicit subcommand) |
| `crossing visualizer metadata` | Browse all movies and gameplay as card tiles — click to open in Shotlist Visualizer |
| `crossing visualizer shotlist` | Inspect and edit shot boundaries, and review LLM annotations alongside video frames |
| `crossing visualizer mosaic` | Interactive search-driven mosaic explorer |
| `crossing visualizer cloud` | Interactive word-cloud explorer with Save PDF button |
| `crossing visualizer composition` | Interactive composition search GUI |

---

## Quickstart

```bash
# 1. Install (see Install section below for details)
uv tool install "crossing[all] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"

# 2. Point crossing at your project folder (created automatically if doesn't exist)
crossing tool path ~/my-project

# 3. Set your API keys (TMDb required for metadata; others optional)
crossing tool api_key set tmdb <key>
crossing tool api_key set opensubtitles <key>

# 4. Import a film (transcodes to H.264/AAC, fetches metadata + subtitles)
crossing media import /path/to/film.mkv

# 5. Detect shot boundaries
crossing shotlist shot detect "Film Title"

# 6. Open the project launcher (configure path, models, open any visualizer)
crossing visualizer

# 7. Annotate shots with an LLM
crossing annotate shot "Film Title"

# 8. Search annotations
crossing search "close-up of a gun"
```

---

## Documentation

- [Install](documentation/install.md) — system dependencies, uv setup, optional extras, developer workflow
- [Commands](documentation/commands.md) — full CLI reference for all commands and flags
- [Keyboard Shortcuts](documentation/shortcuts.md) — keyboard navigation for all visualizers
- [Project Structure](documentation/project.md) — project folder layout, metadata fields, notes, requirements
- [Visualizers](documentation/visualizers.md) — GUI windows for browsing and editing project data.
    - [Project](documentation/visualizer-project.md) (launcher + settings)
    - [Metadata](documentation/visualizer-metadata.md) (card browser for all movies and gameplay)
    - [Shotlist](documentation/visualizer-shotlist.md) (frame-accurate shot editor + annotation reviewer)
    - [Mosaic](documentation/visualizer-mosaic.md) (live search-driven frame grid).
    - [Book](documentation/visualizer-book.md) (page-spread composition tool with silhouette and engraving assets)
    - [Cloud](documentation/visualizer-cloud.md) (interactive word-cloud explorer)
    - [Flipbook](documentation/visualizer-flipbook.md) (per-shot motif + palette color grid)
    - [Palette](documentation/visualizer-palette.md) (per-shot foreground/background color swatch grid)
    - [Silhouette](documentation/visualizer-silhouette.md) (catalog browser for extracted object cutouts + SAM-3 explorer)
- [Source Code](documentation/source.md) — source file tree and key functions per module
- [MCP Server](documentation/mcp.md) — Claude Desktop integration via Model Context Protocol
