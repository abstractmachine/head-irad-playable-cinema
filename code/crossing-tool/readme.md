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
| `crossing shotlist` | Manage shot/scene CSV files and run shot detection |
| `crossing annotate` | Run LLM annotation on shots and scenes |
| `crossing annotate frame` | Find the best matching frame per shot using CLIP (requires prior `annotate shot` pass) |
| `crossing search` | Search shot annotations by query, field, or vocabulary |
| `crossing index` | Build and maintain the semantic search index (txt + embeddings) |
| `crossing index vocabulary` | Build a cached per-field vocabulary index from annotation JSON |
| `crossing generate mosaic` | Generate contact-sheet grids from thumbnails or search results |
| `crossing generate mosaic search` | Mosaic of frames matching a shot annotation search query |
| `crossing generate mosaic export` | Export individual JPEG frames for each search result |
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

# 2. Point crossing at your project folder (created automatically)
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

# 6b. Or open the metadata browser to browse all films
crossing visualizer metadata

# 6c. Or open the shotlist visualizer directly
crossing visualizer shotlist

# 7. Annotate shots with an LLM
crossing annotate shot "Film Title"

# 8. Search annotations
crossing search "close-up of a gun"
```

---

## Install

### 1. System dependencies

```bash
# Linux
sudo apt install curl ffmpeg

# macOS
brew install ffmpeg
```

### 2. Install uv

`uv` is a fast Python package manager that handles everything — including downloading Python itself. Install it once, system-wide:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then restart your shell (or run `source ~/.local/bin/env`) so the `uv` command is available.

### 3. Install crossing

```bash
uv tool install "crossing[all] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"
```

The `crossing` command is now available globally. No virtual environment activation needed.

> **Note:** `crossing` lives inside a larger monorepo. The `#subdirectory=` part tells `uv` where to find it.

### Optional components

`[all]` in the command above installs everything. You can instead pick only what you need:

| Extra | What it adds | Required for |
|---|---|---|
| `annotate` | transformers, huggingface-hub | `crossing annotate` (LLM vision models) |
| `visualizer` | PyQt5, opencv | `--visualizer` flag on any command |
| `shot-detection` | TransNetV2, TensorFlow | `crossing shotlist shot detect` |
| `silhouette` | sam2, opencv | `crossing index silhouette` |

```bash
# Install with a specific extra
uv tool install "crossing[annotate] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"
uv tool install "crossing[visualizer] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"
uv tool install "crossing[shot-detection] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"
uv tool install "crossing[silhouette] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"
```

To change extras on an already-installed tool, add `--reinstall`:
```bash
uv tool install --reinstall "crossing[all] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"
```

### 4. Configure API keys

```bash
crossing tool api_key set tmdb <key>              # required for metadata fetch
crossing tool api_key set opensubtitles <key>     # required for subtitle fetch
crossing tool api_key set discord <webhook-url>   # optional — batch notifications
```

### 5. Verify

```bash
crossing tool version
```

---

## Developer setup

To work on the source code directly rather than installing as a tool:

```bash
git clone https://github.com/abstractmachine/head-irad-playable-cinema.git
cd head-irad-playable-cinema/code/crossing-tool
uv sync              # creates .venv, installs core deps
uv run crossing --help
```

Use `uv run crossing ...` instead of activating the venv. You can still activate manually if you prefer:

```bash
source .venv/bin/activate
crossing --help
```

### Installing optional extras (developer)

Optional features require their extra to be synced into the dev environment. If you run a command and see a missing-module error, install the relevant extra:

| Command that fails | Fix |
|---|---|
| `crossing annotate` | `uv sync --extra annotate` |
| `crossing ... --visualizer` | `uv sync --extra visualizer` |
| `crossing shotlist shot detect` | `uv sync --extra shot-detection` |
| `crossing index silhouette` | `uv sync --extra silhouette` |

```bash
# Install all extras at once
uv sync --extra all
```

After running `uv sync --extra <name>`, no restart is needed — just re-run your command.

---

## Source Code Structure

```
crossing-tool/
├── cli.py                          # CLI entry point — argument parsing and command dispatch
├── crossing_mcp.py                 # MCP server for Claude Desktop integration
├── prefs.py                        # User preferences (~/.crossing/prefs.json)
├── data/
│   ├── annotate.py                 # LLM annotation pipeline (shot and scene)
│   ├── index.py                    # Text serialization and embedding index
│   ├── media_id.py                 # Stable media_id and shot_id utilities
│   ├── metadata.py                 # Metadata I/O and TMDb/OpenSubtitles fetching
│   ├── shot_detection.py           # TransNetV2 shot boundary detection
│   └── shotlist.py                 # Shotlist CSV read/write utilities
├── generators/
│   ├── cloud.py                    # Word-cloud PDF generator
│   ├── composition.py              # Tableau image generator
│   └── mosaic.py                   # Contact-sheet mosaic generator
├── services/
│   ├── annotation_timer.py         # Per-shot ETA estimation during annotation
│   ├── audio_channels.py           # ffprobe channel count inspection
│   ├── audio_normalize.py          # ffmpeg loudnorm measurement and gain computation
│   ├── frame_match.py              # CLIP-based best-frame matching for shot annotations
│   ├── import_media.py             # File copy and transcode pipeline
│   ├── models.py                   # HuggingFace model download and management
│   ├── normalize.py                # Filename normalisation (dash-separated → Title Case)
│   ├── notify.py                   # Discord webhook notifications
│   ├── search.py                   # Semantic and keyword annotation search
│   └── transcode.py                # ffmpeg transcoding and thumbnail extraction
├── styles/
│   ├── theme.py                    # Qt stylesheet and colour constants
│   ├── fonts/                      # Bundled variable fonts (Hanken Grotesk, Roboto family)
│   └── icons/                      # UI icons
├── tests/
│   ├── test_audio_channels.py
│   └── test_audio_normalize.py
└── visualizers/
    ├── annotation_visualizer.py    # Backward-compat shim → shot_visualizer
    ├── cloud_visualizer.py         # Qt word-cloud explorer with Save PDF
    ├── composition_visualizer.py   # Qt composition explorer GUI
    ├── metadata_visualizer.py      # Qt metadata card browser
    ├── mosaic_visualizer.py        # Qt mosaic search explorer
    ├── project_visualizer.py       # Qt launcher and configuration hub
    └── shot_visualizer.py          # Qt shotlist editor with frame-precise video player
```

### Key Functions by Module

#### `data/annotate.py`

| Function | Description |
|---|---|
| `annotate_file_shots(project_path, filename, ...)` | Runs VLM annotation over all shots in one video, writing canonical JSON output |
| `annotate_all_files(project_path, media_type, ...)` | Batch-annotates every registered file in a media library |
| `sample_frames_for_shot(video_path, start_time, end_time, ...)` | Extracts representative JPEG frames from a shot's time range via ffmpeg |
| `get_annotation_json_path(project_path, filename, media_type)` | Returns the canonical annotation JSON path for a given video |
| `reindex_annotations_for_merge(...)` | Removes annotation entries invalidated by a shot merge |
| `reindex_annotations_for_split(...)` | Removes annotation entries invalidated by a shot split |

#### `data/index.py`

| Function | Description |
|---|---|
| `serialize_annotation_item(item, mapping)` | Converts one annotation entry into a pipe-separated text line for indexing |
| `embed_texts(texts, model_name, project_path, ...)` | Generates mean-pooled transformer embeddings for a list of text strings |
| `load_mapping(project_path)` | Loads the field → serialization config from `preferences/data/mapping.yaml` |
| `load_fields(project_path)` / `save_fields(...)` | Reads/writes the ordered display field list from `preferences/data/fields.yaml` |
| `write_text_file(...)` / `write_embeddings(...)` | Persists serialized text and embedding arrays to disk |

#### `data/media_id.py`

| Function | Description |
|---|---|
| `compute_media_id(record, media_type)` | Stable deterministic ID preferring TMDb/YouTube/Vimeo IDs, falling back to SHA-256 |
| `build_shot_id(media_id, start_frame, end_frame)` | Returns `<media_id>@fSTART-fEND` canonical shot identifier |
| `parse_shot_id(shot_id)` | Parses a shot_id string back into `(media_id, start_frame, end_frame)` |

#### `data/metadata.py`

| Function | Description |
|---|---|
| `get_metadata(project_path, query, media_type)` | Returns entries matching a query (all, by index, or filename/title substring) |
| `set_metadata(project_path, data, match_filename)` | Writes or updates a metadata record, assigning a `media_id` |
| `fetch_metadata(filename, project_path)` | Fetches movie metadata from TMDb by parsing the filename |
| `fetch_thumbnail(...)` / `fetch_subtitle(...)` | Downloads a TMDb poster or OpenSubtitles subtitle file |
| `load_json_metadata(project_path, media_type)` | Loads metadata from `data/metadata/<type>.json` |
| `save_json_metadata(project_path, media_type, records)` | Writes sorted metadata records to JSON |
| `upsert_json_record(project_path, record, media_type, match_key)` | Inserts or replaces a single record matched by key field |
| `migrate_csv_to_json(project_path, media_type)` | Migrates legacy CSV metadata to JSON format |
| `prune_metadata(project_path, media_type)` | Deletes records whose video files no longer exist on disk |
| `ingest_gameplay(src_path, project_path, title, game)` | Copies a gameplay clip, extracts a thumbnail, and writes a metadata record |

#### `data/shot_detection.py`

| Function | Description |
|---|---|
| `detect_shots_transnet(video_path)` | Detects shot boundaries with TransNetV2, returning dicts with start/end time, frame numbers, and confidence |
| `write_shotlist_csv(project_path, filename, shots, media_type, force)` | Writes detected shots to a CSV at the canonical shotlist path |

#### `data/shotlist.py`

| Function | Description |
|---|---|
| `read_shotlist(project_path, filename, media_type)` | Reads a shotlist CSV and returns normalized shot dicts |
| `write_shotlist(project_path, filename, media_type, shots)` | Writes shot dicts back to the shotlist CSV |
| `attach_shot_ids(shots, media_id)` | Attaches stable `shot_id` strings to each shot dict in-place |
| `resolve_filename(project_path, tmdb_id, filename, media_type)` | Resolves a TMDb ID or partial filename to the exact on-disk filename |
| `normalize_shot_fields(shot)` | Renames legacy CSV column names to canonical equivalents |

#### `generators/mosaic.py`

| Function | Description |
|---|---|
| `mosaic_from_search_results(results, project_path, output_path, ...)` | Builds a mosaic grid PNG from `search_shots()` results |
| `export_frames_from_search_results(results, project_path, query, ...)` | Exports each search result as a numbered JPEG with a metadata info bar |
| `render_mosaic(items, output_path, layout, ...)` | Renders a list of `MosaicItem` objects into a single contact-sheet PNG |
| `extract_frame_pil(video_path, frame_index)` | Extracts a single video frame by index as an RGB PIL Image |

#### `generators/cloud.py`

| Function | Description |
|---|---|
| `cloud_from_annotations(project_path, scope, field, media_type, ...)` | Shared entry point: counts words from annotation JSON(s) and calls `render_cloud()` to produce a PDF |
| `extract_annotation_words(project_path, scope, field, media_type, min_count)` | Loads annotation JSON files, tokenises all text, strips stopwords, and returns a word-frequency `Counter` |
| `render_cloud(words, output_path, width, height, max_words, ...)` | Lays out words on an Archimedean spiral with log-frequency font sizes and saves as PDF or PNG |

#### `generators/composition.py`

| Function | Description |
|---|---|
| `build_tableau(result, project_path, orientation)` | Extracts the mid-shot frame for a result and scale-fills it onto a canvas, returning a PIL Image |
| `save_tableau(img, criteria, output_dir)` | Saves the image as `<criteria>+<date>+<time>.jpg` |
| `choose_background(results, seed)` | Picks one random result from a `search_shots()` results list |

#### `services/search.py`

| Function | Description |
|---|---|
| `search_shots(query, scopes, field, limit, ...)` | Searches annotation JSONs by keyword with per-field filtering, relevance scoring, and scope resolution |
| `vocabulary_from_field(field, scopes, ...)` | Enumerates distinct values in an annotation field across matching films |

#### `services/transcode.py`

| Function | Description |
|---|---|
| `transcode_file(src, project_path, media_type, platform)` | Transcodes a video to H.264/AAC MP4 with a named platform profile |
| `extract_video_thumbnail(video_path, thumb_path)` | Extracts a JPEG thumbnail from ~5% into the video via ffmpeg |

#### `services/audio_normalize.py`

| Function | Description |
|---|---|
| `measure_audio_gain_db(video_path, target_lufs)` | Runs ffmpeg loudnorm and returns `(gain_db, integrated_lufs)` |
| `compute_audio_gain_db(integrated_lufs, target_lufs)` | Computes the dB gain offset to reach a target LUFS |

#### `services/frame_match.py`

| Function | Description |
|---|---|
| `annotate_best_frames(project_path, filename, media_type, model_name, force, verbose)` | Iterates all shots in a film's annotation JSON, finds the best-matching frame for each via CLIP, saves it as a PNG, and stores `best_frame` metadata in the JSON |
| `find_query_best_frame_for_shot(video_path, start_time, end_time, query, model, processor, device)` | Samples frames from a shot's time range and returns `(frame_index, score)` for the frame most similar to the query text |
| `best_frame_path(project_path, media_type, filename, shot_id)` | Returns the deterministic output path for a best-frame PNG |
| `load_best_frame_lookup(project_path, filename, media_type)` | Returns a `{shot_id: best_frame_dict}` mapping loaded from the annotation JSON |
| `compute_blur_score(image)` | Returns a normalized sharpness score (0–1) used to penalize blurry frame candidates |
| `compute_description_hash(description)` | Returns a short stable hash of a description string for change detection |

#### `services/models.py`

| Function | Description |
|---|---|
| `download_model(project_path, repo_or_url, local_name, ...)` | Downloads a HuggingFace model snapshot into `models/` |
| `list_models(project_path)` | Prints a table of local models with parameter counts, dtype, disk size, and role assignments |
| `model_size_report(project_path, name)` | Estimates VRAM requirements and compares to available GPU memory |
| `remove_model(project_path, name, confirm)` | Deletes a model directory (dry run unless `--confirm`) |

#### `visualizers/shot_visualizer.py`

| Function / Class | Description |
|---|---|
| `ipc_send_load(project_path, filename, media_type)` | Sends a "load film" message over Unix domain socket to a running `ShotlistVisualizer` |
| `AudioPlayer` | Streams audio in a background thread (PyAV + sounddevice) with gain and channel-map support |
| `ShotlistVisualizer` | Qt main window — frame-precise video player, shot/scene editor, annotation panel, IPC server |

---

## Commands

### Tool Setup

```bash
# Show tool and data structure versions
crossing tool version
crossing tool version --init         # initialize/update data version

# Get or set the active project folder
crossing tool path [folder]

# Get or set the project name
crossing tool name [name]

# Manage models
crossing tool model get [annotate|segmentation|embed|frame_match]        # show current model (all or one role)
crossing tool model set {annotate,segmentation,embed,frame_match} <name> # set model for a role
crossing tool model list                                     # list models in project models/ folder
crossing tool model download <hf-repo-id> [--name <folder>] # download a model from HuggingFace
crossing tool model size [<name>]                            # show model disk usage
crossing tool model remove <name> [--confirm]                # delete a model folder

# Get or set persistent defaults (e.g. frames-per-shot for annotate)
crossing tool default get [key]
crossing tool default set <key> <value>

# Audio normalization baseline (global target LUFS, default: -23.0)
crossing tool default get audio-target-lufs
crossing tool default set audio-target-lufs -23.0

# Annotation field list (which fields appear in the index / search)
crossing tool default get fields
crossing tool default set fields "type, spatial, time_of_day, camera, shot, setting, description, humans, wearing, animals, objects, action, text"

# Atomic label fields (comma-joined items within these fields are split during annotation)
# Configure per-project in preferences/data/fields.yaml under the 'atomic' key.
crossing tool default get atomic-fields
crossing tool default set atomic-fields "humans, wearing, animals, objects, action"

# Send a test notification to verify a service is configured
crossing tool notify discord
```

### Media

Manage content: videos, subtitles, posters, and thumbnails.

```bash
# Import video files (supports individual files, multiple files, or folders)
crossing media import <file(s)|folder>
crossing media import --pick              # open GUI file/folder picker
  --media {movie,gameplay}                # destination (required)
  --optimize {universal,pi5}             # re-encode for a target platform (movie only; omit to copy as-is)
  --skip-metadata                         # skip automatic metadata fetch (movie only)
  --title <title>                         # display title (gameplay only; default: derived from filename)
  --game <slug>                           # game identifier for media_id prefix (gameplay only, required; e.g. rdr2)
  --verbose                               # print a message as each file import begins

# Movie examples:
crossing media import /path/to/video.mp4 --media movie
crossing media import /path/to/movies/ --media movie
crossing media import --pick --media movie    # GUI picker for single/multiple files or folder

# Gameplay examples:
crossing media import clip.mp4 --media gameplay --game rdr2
crossing media import /path/to/clips/ --media gameplay --game rdr2 --title "Red Dead Redemption 2"

# Remove a film and all its associated files
crossing media remove [query]             # match by filename or title words
crossing media remove --tmdb 391         # match by TMDb ID
  --media {movies,gameplay}              # media type (required)
  --confirm                              # actually delete (default is a dry run)

# Audit — report missing metadata, thumbnails, shotlists, and subtitles
crossing media audit
  --media {movies,gameplay}              # media type (default: movies)

# Update — fetch and save metadata/thumbnails for entries missing key fields
crossing media update
  --file <filename>                      # update a single file by filename
  --force                                # force re-fetch for all entries
  --media {movies,gameplay}              # media type (default: movies)

# Normalize audio — writes one scalar field (audio_gain_db) to metadata
crossing media normalize movie "Film Title"
crossing media normalize gameplay "rdr2-clip-id"
crossing media normalize --all

# Scan audio channel layouts — writes audio_channels mapping to metadata
crossing media channels movie "Film Title"
crossing media channels gameplay "rdr2-clip-id"
crossing media channels --all

# Read-only channel distribution (does not save metadata)
crossing media channels --count
crossing media channels --count --format json

# Download and manage subtitles
crossing media subtitle fetch [query]    # fetch missing subtitles from OpenSubtitles
crossing media subtitle fetch --tmdb 391
crossing media subtitle fetch --all      # fetch for all entries without a subtitle
crossing media subtitle fetch --force    # re-download even if one already exists
  --media {movies,gameplay}

crossing media subtitle list             # show subtitle status for all entries
  --media {movies,gameplay}
```

### Metadata Management

```bash
# List all metadata entries
crossing metadata list
  --year 1966                       # filter by year
  --director "Sergio Leone"         # filter by director (substring)
  --fields title,year,director      # show specific fields only
  --sort year                       # sort by field
  --reverse                         # reverse sort order
  --media {movies,gameplay}         # media type (default: movies)

# Get metadata (all, by index, or by filename substring)
crossing metadata get [query]
  --media {movies,gameplay}
  --markdown                        # save output as Markdown to <project>/data/markdown/
  --open                            # open the saved Markdown file (implies --markdown)

# Set a metadata field manually
crossing metadata set '<json>'
# Example: crossing metadata set '{"filename": "Django (1966).mp4", "director": "Sergio Corbucci"}'

# Update metadata (fetch from TMDb/OpenSubtitles)
crossing metadata update
  --file <filename>                 # update single file
  --force                           # re-fetch all entries (including duration)
  --media {movies}

# Count entries
crossing metadata count
  --media {movies,gameplay}

# Remove orphaned entries (no matching video file)
crossing metadata prune
  --confirm                         # actually remove (default: dry run)
  --media {movies,gameplay}

# Audit metadata — show entries missing shotlists, subtitles, or annotations
crossing metadata audit
  --media {movies,gameplay}
```

### Shotlist Management

```bash
# List all available shotlists
crossing shotlist list
  --media {movies,gameplay}         # filter by media type
  --json                            # output as JSON

# Get shotlist data for a file
crossing shotlist get <filename>
crossing shotlist get --tmdb 391     # use TMDb ID instead of filename
  --scene 0                         # filter by scene number
  --media {movies,gameplay}

# Show specific shot data
crossing shotlist show shot <filename> <shot_index>
crossing shotlist show shot --tmdb 391 52
  --media {movies,gameplay}
  --field protagonists place actions        # extract specific fields (table output)
  --json                                    # output as JSON

# Show all shots in a scene
crossing shotlist show scene <filename> <scene_number>
crossing shotlist show scene --tmdb 391 1
  --media {movies,gameplay}
  --field protagonists actions
  --json

# Detect shot boundaries automatically using TransNetV2
crossing shotlist shot detect <filename_substring>
crossing shotlist shot detect --tmdb 391             # use TMDb ID
  --media {movies,gameplay}                          # media type (default: movies)
  --force                                            # overwrite existing shotlist
  --all                                              # process all entries in project (skips existing)
  --notify                                           # Discord notification when finished
  --notify-each                                      # Discord notification after each item (batch only)

# Examples:
crossing shotlist shot detect Django                 # find by filename substring
crossing shotlist shot detect --tmdb 10772           # find by TMDb ID
crossing shotlist shot detect "Fistful" --force      # overwrite existing
crossing shotlist shot detect --all                  # detect shots for all movies without a shotlist
crossing shotlist shot detect --all --media gameplay # detect shots for all gameplay entries
crossing shotlist shot detect --all --force          # reprocess everything
crossing shotlist shot detect --all --notify         # notify when the whole batch finishes
crossing shotlist shot detect --all --notify-each    # notify after every film in the batch

# Output CSV format:
# Ignore,Scene,Start,End,Start_Frame,End_Frame,Shot_Caption,Scene_Caption,Shot_Source,Shot_Confidence
# No,0,00:00:00.000,00:00:05.123,0,123,"","",auto,0.876

# Open the shotlist visualizer GUI
crossing visualizer shotlist
  --media {movies,gameplay}                     # media type (default: movies)

# Migrate shotlist CSVs from legacy column names to the canonical naming scheme
crossing shotlist migrate
  --media {movies,gameplay}         # limit to one media type (default: both)
  --dry-run                         # report changes without writing files

# Keyboard shortcuts in shotlist visualizer (OpenCV-based frame-precise):
# Space      - Play/Pause
# ↑/↓        - Previous/Next shot (resumes playback if was playing)
# ←/→        - Step one frame backward/forward
# Shift+←/→  - Step one second backward/forward
# PgUp/PgDn  - Previous/Next scene
# Home       - Switch to previous movie in list
# End        - Switch to next movie in list
# E          - Jump to end frame of current shot
# F          - Toggle Ignore flag on current shot
# M          - Merge current shot with previous
# N          - Split current shot at current frame (creates new shot boundary)
# Ctrl+S     - Save changes
# Continue button - toggle playback past shot boundaries (ON/OFF)
```

### Visualizers

All visualizer GUIs share the same theme and support **Ctrl+Q** / **Ctrl+W** to close.

```bash
# Open the project launcher (default — no subcommand needed)
crossing visualizer
crossing visualizer project

# Open individual visualizers directly
crossing visualizer metadata              # browse all movies and gameplay as card tiles
crossing visualizer shotlist              # inspect / edit shot boundaries and review LLM annotations
  --media {movies,gameplay}              # media type (default: movies)
  --filename <filename>                  # jump to (or load) a specific film; sends to a running
                                         # instance via IPC first, falls back to opening a new window
crossing visualizer mosaic               # interactive search-driven mosaic explorer
crossing visualizer composition          # interactive composition search GUI
```

The **project** visualizer lets you:
- Set (via folder picker) and display the current project path
- Adjust annotation defaults (frames-per-shot, min-frame-interval, max-frames-per-shot)
- Select models for each role (annotate, segmentation, embed) from installed local models
- Launch any of the four other visualizers from a 2×2 grid (Metadata, Shotlist, Mosaic, Composition)

The **metadata** visualizer shows all movies and gameplay as scrollable card tiles in two columns. Each card displays a thumbnail, title, year/director (movies) or game (gameplay), and a short overview. Cards highlight fuchsia on hover; clicking a card opens that film in the Shotlist Visualizer. If the Shotlist Visualizer is already open it receives the film via an IPC socket (no second window is opened); if not, a new window is launched.

The **shotlist** visualizer always populates its film selector with the full list of all films in the project for the active media type, regardless of how many were specified on the command line.

### Annotate

Use an LLM to annotate shots or scenes. Requires a vision model configured via `crossing tool model set annotate <model-folder>`.

```bash
# Annotate all shots in a single film (uses configured model)
crossing annotate shot <filename_substring>
crossing annotate shot --tmdb 391
  --media {movies,gameplay}
  --model <model-folder-name>       # override configured model
  --frames-per-shot 3               # baseline frames to sample per shot (fast default)
  --min-frame-interval 4            # for long shots, sample at least 1 frame every N seconds
  --max-frames-per-shot 16          # hard cap on adaptive long-shot sampling
  --sample-mode {center,start,end}  # frame sampling position (default: center)
  --force                           # overwrite existing annotations
  --skip-existing                   # skip already-annotated shots (default: true)
  --no-skip-existing                # process all shots including already-annotated ones
  --limit N                         # process only first N shots
  --prompt-file <file>              # system prompt file
  --prompt-text <text>              # inline system prompt
  --user-prompt-file <file>         # user prompt file
  --export-csv <path>               # export annotations as CSV
  --export-md <path>                # export annotations as Markdown
  --verbose                         # print per-shot progress
  --log                             # write debug log file
  --reload-every N                  # reload model every N shots to prevent drift (default: 25)
  --notify                          # Discord notification on finish
  --notify-each                     # Discord notification after each film (batch)
  --with-frame                      # also run 'annotate frame' after each annotated movie (--all mode)
  --frame-model <model-name>        # CLIP model for best-frame detection (default: clip-vit-base-patch32)
  --no-best                         # skip automatic best-frame detection after shot annotation

# Annotate all films in project
crossing annotate shot --all
crossing annotate shot --all --force
crossing annotate shot --all --notify

# Manual annotation (provide caption text directly)
crossing annotate shot <filename> <shot_index> "caption text"
crossing annotate shot --tmdb 391 5 "Close-up of revolver"

# Annotate a specific scene with LLM
crossing annotate scene <filename> <scene_number>
crossing annotate scene --tmdb 391 2
  --model <model-folder-name>
  --frames-per-shot 3
  --min-frame-interval 4
  --max-frames-per-shot 16
  --force

# Manual scene annotation
crossing annotate scene <filename> <scene_number> "caption text"

# Remove annotations for a film
crossing annotate remove <filename_substring>
crossing annotate remove --tmdb 391
crossing annotate remove --all

# Audit annotation status across all films
crossing annotate audit
  --media {movies,gameplay}

# Validate and repair annotation JSON (fix comma-separated values in array fields)
crossing annotate validate <filename_substring>
crossing annotate validate --tmdb 391
crossing annotate validate --all
  --media {movies,gameplay}
  --dry-run                         # report issues without writing any changes

# Migrate annotation JSON from legacy integer shot_ids to stable <media_id>@fSTART-fEND IDs
crossing annotate migrate <filename_substring>
crossing annotate migrate --tmdb 391
crossing annotate migrate --all
  --media {movies,gameplay}

# Find the best matching frame per shot using CLIP
# Reads the 'description' field from each shot's existing annotation and selects the
# video frame that best matches it. Saves frames to media/frames/best/ and stores
# best_frame metadata in the annotation JSON. Requires a prior 'annotate shot' pass.
crossing annotate frame <filename_substring>
crossing annotate frame --tmdb 391
crossing annotate frame --all
  --media {movies,gameplay}
  --model <model-name>              # CLIP model (default: clip-vit-base-patch32)
  --force                           # re-process shots even when best_frame already exists
  --verbose                         # print per-shot progress
  --notify                          # Discord notification when batch finishes (--all)
  --notify-each                     # Discord notification after each film (--all)
```

Adaptive frame sampling notes:

- `--frames-per-shot` remains the baseline target (recommended default: `3` for speed).
- Short shots are still downsampled aggressively for performance.
- Long shots use interval-based sampling: at least one frame every `--min-frame-interval` seconds.
- Long-shot sampling is bounded by `--max-frames-per-shot`.

### Search

Search shot annotations by semantic query, field value, or vocabulary.

`crossing search` is a single flat command. `vocabulary` and `text` are special values for the positional `query` argument, not subcommands.

```bash
crossing search <query> [scope ...] [options]
```

```bash
# Semantic search across all annotation fields
crossing search "close-up of a gun"
crossing search "close-up of a gun" --all
crossing search "close-up of a gun" Django "Fistful"   # restrict to specific films
  --media {movies,gameplay}
  --field <field>                   # restrict to one annotation field (e.g. objects)
  --limit N                         # max total results
  --limit-per-item N                # max results per film

# Search within the annotation text field specifically (query = "text")
crossing search text "WANTED"
crossing search text "WANTED" --all

# List all distinct values for a given annotation field (query = "vocabulary")
crossing search vocabulary place
crossing search vocabulary place --all
crossing search vocabulary place Django              # restrict to one film
  --sort {alphabetical,count}
  --show_count                      # include occurrence counts
  --media {movies,gameplay}

# Vocabulary across all fields (outputs JSON)
crossing search vocabulary --all-fields
crossing search vocabulary --all-fields --exclude description humans

# Save vocabulary output as Markdown
crossing search vocabulary place --markdown --open
crossing search vocabulary --all-fields --markdown
```

### Index

Build and maintain the semantic search index (serialized text + embeddings) that powers `crossing search`.

```bash
# Serialize annotation JSON to per-shot text lines
crossing index serialize <filename_substring>
crossing index serialize --tmdb 391
  --shot N                          # serialize a single shot
  --save                            # write to .txt file
  --print                           # also print when --save is used
  --force                           # overwrite existing .txt
  --verbose
  --media {movies,gameplay}

# Embed serialized text into a .npy vector file
crossing index embed <filename_substring>
crossing index embed --tmdb 391
  --model <model-name>              # embedding model (default: BAAI/bge-small-en-v1.5)
  --force
  --verbose
  --media {movies,gameplay}

# Reconcile .txt, .npy, and manifest (re-serializes/re-embeds only when source changed)
crossing index update <filename_substring>
crossing index update --tmdb 391
crossing index update --all
  --model <model-name>
  --force                           # rebuild even if up to date
  --verbose
  --media {movies,gameplay}

# Inspect index status without modifying files
crossing index audit
crossing index audit <filename_substring>
crossing index audit --tmdb 391
  --verbose
  --media {movies,gameplay}

# Build a per-field vocabulary index (token → shot-count) from annotation JSON.
# Stores result in data/index/vocabulary_<media_type>.json.
# Used to speed up `crossing search vocabulary` (future integration).
crossing index vocabulary
crossing index vocabulary --media gameplay
crossing index vocabulary --all              # both movies and gameplay
  --force                                    # rebuild even if cache exists
```

### Generate

Generate content from project data.

#### Mosaic

Generates a contact-sheet grid image from thumbnails or shot frames.

```bash
# Mosaic of all movie thumbnails
crossing generate mosaic thumbnails --media movies --all

# Mosaic of frames matching a shot annotation search query
crossing generate mosaic search "close-up gun" --all
crossing generate mosaic search "close-up gun" Django    # restrict to one film
crossing generate mosaic search text "WANTED" --all      # restrict to annotation text field
  --field <field>                   # restrict to one annotation field
  --limit N                         # max results / tiles
  --layout {landscape,portrait}     # grid orientation (default: landscape)
  --frame_pct 0.5                   # frame position in shot (0.0=start, 1.0=end)
  --no-open                         # do not open result
  --notify                          # Discord notification when finished

# Export individual JPEG frames for each search result
crossing generate mosaic export "close-up gun" --all
  --limit N                         # max results to export
  --frame_pct 0.5
  --no-open
  --notify

# Open the interactive mosaic explorer GUI
crossing generate mosaic --visualizer
```

**Flags (thumbnails):**

| Flag | Default | Description |
|------|---------|-------------|
| `--media` | `movies` | `movies` or `gameplay` |
| `--all` | — | Include all entries (required for now) |
| `--layout` | `landscape` | `landscape` (wider grid) or `portrait` (taller grid) |
| `--caption` | `short` | `short` (title + year) or `none` |
| `--output` | auto | Full save path override |
| `--notify` | — | Discord notification when finished |

**Flags (search / export):**

| Flag | Default | Description |
|------|---------|-------------|
| `--media` | `movies` | `movies` or `gameplay` |
| `--all` | — | Search all movies (overrides positional scopes) |
| `--field` | — | Restrict search to one annotation field |
| `--limit` | — | Max search results / tiles |
| `--layout` | `landscape` | Grid orientation: `landscape` or `portrait` |
| `--frame_pct` | `0.5` | Frame position within shot (0.0=start, 0.5=middle, 1.0=end) |
| `--output` | auto | Override output file path |
| `--no-open` | — | Do not open the result in the desktop viewer |
| `--notify` | — | Discord notification when finished |

Output is saved to `<project>/output/mosaics/`.

#### Composition

Builds a single tableau image by compositing a randomly sampled background frame against
a search-driven foreground. Uses the semantic search index.

```bash
# Build a composition with a background matching a search query
crossing generate composition "Sunrise"
crossing generate composition "close-up gun"
  --orientation {portrait,landscape}  # canvas preset (default: portrait)
  --output <path>                     # override output file path
  --no-open                           # do not open result in desktop viewer
  --notify                            # Discord notification when done

# Open the interactive composition visualizer
crossing generate composition --visualizer
```

**Flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--orientation` | `portrait` | `portrait` (1240×1754) or `landscape` (1920×1080) |
| `--output` | auto | Full save path override |
| `--no-open` | — | Skip opening the result in the desktop viewer |
| `--notify` | — | Discord notification when done |
| `--visualizer` | — | Open the interactive GUI instead of saving |

Output is saved to `<project>/output/compositions/`.

### Cloud

Generate a word-cloud PDF from annotation text across one or all movies.

```bash
# Generate cloud for all movies, all annotation fields
crossing generate cloud

# Generate cloud for a specific movie (fuzzy match on title)
crossing generate cloud --scope "Film Title"

# Restrict to a specific annotation field
crossing generate cloud --field description
crossing generate cloud --field objects --scope "Film Title"

# Adjust word count and minimum frequency
crossing generate cloud --max-words 200 --min-count 3

# Specify output path
crossing generate cloud --output ~/Desktop/my-cloud.pdf

# Open the interactive visualizer
crossing generate cloud --visualizer
crossing visualizer cloud
```

**Flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--scope` | all | Film title (fuzzy match) to restrict the cloud to a single movie |
| `--field` | all fields | Annotation field to use: `setting`, `description`, `objects`, `action`, `humans`, `wearing`, `animals`, `text` |
| `--media` | `movies` | Media type: `movies` or `gameplay` |
| `--max-words` | `150` | Maximum number of words to render |
| `--min-count` | `2` | Minimum word frequency to include |
| `--output` | auto | Full save path override (PDF or PNG by extension) |
| `--no-open` | — | Skip opening the result in the desktop viewer |
| `--notify` | — | Discord notification when done |
| `--visualizer` | — | Open the interactive GUI instead of saving |

Output is saved to `<project>/output/clouds/`.



```bash
# Get stored API key
crossing tool api_key get {discord,opensubtitles,tmdb}

# Set API key
crossing tool api_key set {discord,opensubtitles,tmdb} <key>
```

### Models

Download models from HuggingFace using the built-in downloader:

```bash
crossing tool model download <hf-repo-id>
crossing tool model download <hf-repo-id> --name <folder-name>  # custom folder name
```

Example:

```bash
crossing tool model download Qwen/Qwen3-VL-8B-Thinking --name quen3-vl-8b-thinking
```

Models are saved to `<project>/models/`. After downloading, set the model for a role:

```bash
crossing tool model set annotate quen3-vl-8b-thinking
```

### Discord Notifications

Long-running batch commands (`shotlist shot detect`, `annotate shot`) support optional
Discord notifications via a webhook URL.

**One-time setup:**
1. In Discord, open **Server Settings → Integrations → Webhooks → New Webhook**.
2. Name it, pick a channel, then click **Copy Webhook URL** — it will look like:
   `https://discord.com/api/webhooks/1234567890/xxxxxxxxxxxx`
3. Paste it into the CLI (this saves it to `preferences/keys/discord_api_key.txt`):
```bash
crossing tool api_key set discord https://discord.com/api/webhooks/1234567890/xxxxxxxxxxxx
```
4. Verify it was saved:
```bash
crossing tool api_key get discord
```

**Flags:**

| Flag | Behaviour |
|------|-----------|
| `--notify` | Send one message when the entire process finishes (single film or full batch) |
| `--notify-each` | Send a message after each individual film in a batch, including elapsed time |

```bash
# Notify on batch completion
crossing shotlist shot detect --all --notify

# Notify after every film + on completion
crossing annotate shot --all --notify --notify-each
crossing shotlist shot detect --all --notify --notify-each
```

### MCP Server (Claude Desktop integration)

`crossing_mcp.py` exposes crossing data to LLMs via the [Model Context Protocol](https://modelcontextprotocol.io). This lets you query your film library directly from Claude Desktop (or any MCP-compatible client) without leaving the chat.

See [mcp](./mcp.md) for more information on setup and usage.

## Metadata Fields

Movies metadata includes:
- `media_id`, `title`, `year`, `director`, `tmdb`, `imdb`
- `filename`, `original_filename`, `duration` (actual file duration in minutes)
- `overview`, `tagline`
- `audio_gain_db` (loudnorm gain offset in dB to reach target LUFS; added by `crossing media normalize`)
- `audio_channels` (channel layout mapping used by the audio player; added by `crossing media channels`)

Gameplay metadata includes:
- `media_id`, `title`, `game` (game slug, e.g. `rdr2`)
- `filename`, `original_filename`, `duration`
- `overview`, `tagline`

## Notes

- All movie files are transcoded to H.264/AAC MP4 format on import (use `--optimize` to target a platform; omit to copy as-is)
- Gameplay files are copied as-is with a stable `<media_id> - <title>` filename and a thumbnail extracted automatically
- Metadata is automatically fetched from TMDb during movie import; gameplay imports are metadata-free (no TMDb lookup)
- Thumbnails and subtitles are automatically downloaded when available during movie import
- Shotlist commands accept either a full filename substring or `--tmdb <id>` for convenience
- `crossing media audit` and `crossing metadata audit` are equivalent; both report on metadata, thumbnails, shotlists, and subtitles
- The `--pick` flag on import opens a native GUI file picker — uses PyQt5 (installed with `[visualizer]`), or falls back to `zenity`/`kdialog` (Linux), `osascript` (macOS), or PowerShell (Windows)
- Use `--field` with `shotlist show` commands to extract specific fields from caption JSON (table or JSON output)
- Use `--json` flag for raw JSON output (full shot data or filtered fields with `--field`)
- Shot detection uses TransNetV2 and creates CSV files with `Shot_Source="auto"`, confidence scores, and exact frame numbers (`Start_Frame`/`End_Frame`)
- Shotlist visualizer GUI (`crossing visualizer shotlist`) uses OpenCV for frame-precise display — each frame is seeked by exact integer frame index, not timecode
- `crossing index update` checks for changes in annotation files before re-serializing or re-embedding, making it safe to run repeatedly
- Annotation IDs use a stable `<media_id>@fSTART-fEND` format; use `crossing annotate migrate` to upgrade existing projects from legacy integer IDs
- The **metadata visualizer** communicates with a running Shotlist Visualizer via a Unix domain socket (`/tmp/crossing_shotlist_<hash>.sock`); the socket is created when the Shotlist Visualizer opens and removed when it closes

## Requirements

- **ffmpeg** (system): `sudo apt install ffmpeg` — required for video transcoding

Python 3.11+ and all Python packages are managed automatically by `uv` — no manual installation needed.

### Project Folder Structure

```
<project>/
├── data/
│   ├── annotations/
│   │   ├── scenes/
│   │   │   ├── movies/             # scene-level annotation JSON (when generated)
│   │   │   └── gameplay/
│   │   └── shots/
│   │       ├── movies/
│   │       │   ├── <filename>.json          # shot annotations (aggregated)
│   │       │   ├── <filename>.log           # annotation run log (optional)
│   │       │   ├── <filename>.txt           # serialized text for indexing
│   │       │   ├── <filename>.npy           # embedding vectors
│   │       │   └── <filename>.manifest.json # index manifest/state
│   │       └── gameplay/
│   ├── markdown/                  # markdown exports (e.g., vocabulary output)
│   ├── metadata/
│   │   ├── movies.json             # movie metadata (JSON array under "media" key)
│   │   └── gameplay.json           # gameplay metadata
│   ├── shotlists/
│   │   ├── movies/                 # shot-level data for movies
│   │   │   └── <filename>.csv      # shot boundaries and timing data
│   │   └── gameplay/               # shot-level data for gameplay
│   │       └── <filename>.csv
├── media/
│   ├── videos/
│   │   ├── movies/                 # imported movie files
│   │   └── gameplay/               # imported gameplay footage
│   ├── thumbnails/
│   │   ├── movies/                 # movie posters from TMDb
│   │   └── gameplay/               # gameplay thumbnails
│   ├── subtitles/
│   │   ├── movies/                 # English subtitles from OpenSubtitles
│   │   └── gameplay/               # gameplay subtitles
│   ├── frames/
│   │   └── best/
│   │       ├── movies/             # best-frame PNGs per shot (from `crossing annotate frame`)
│   │       └── gameplay/
├── output/
│   ├── mosaics/                    # output from `crossing generate mosaic`
│   ├── clouds/                     # output from `crossing generate cloud`
│   └── compositions/               # output from `crossing generate composition`
├── models/
│   └── <model-folder>/             # local HuggingFace models (annotate, embed, segmentation)
└── preferences/
    ├── data/
    │   ├── mapping.yaml            # field → index serialization config (for crossing index)
    │   └── fields.yaml             # ordered display field list (for crossing search vocabulary)
    ├── keys/                       # API keys
    │   ├── discord_api_key.txt
    │   ├── tmdb_api_key.txt
    │   └── opensubtitles_api_key.txt
    └── version.txt                 # data structure version
```

