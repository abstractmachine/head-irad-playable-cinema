# Crossing

A CLI + GUI tool for relating moving images across media — connecting gameplay sequences to cinema, live gameplay input to archived material. Manages a local project folder as its database. All models run locally; no external services required.

---

## Commands at a Glance

| Command | What it does |
|---------|-------------|
| `crossing tool` | Configure project path, name, models, and API keys |
| `crossing media import` | Import and transcode video files into the project |
| `crossing media remove` | Remove a film and all its associated data |
| `crossing media subtitle` | Fetch and list subtitles via OpenSubtitles |
| `crossing metadata` | List, get, update, validate, and audit metadata |
| `crossing shotlist` | Manage shot/scene CSV files and run shot detection |
| `crossing annotate` | Run LLM annotation on shots and scenes |
| `crossing search` | Search shot annotations by query, field, or vocabulary |
| `crossing index` | Build and maintain the semantic search index (txt + embeddings) |
| `crossing generate mosaic` | Generate contact-sheet grids from thumbnails or search results |
| `crossing generate compose` | Generate experimental poster compositions (SAM 2 + text patches) |

---

## Quickstart

```bash
# 1. Activate the virtual environment
source ~/venvs/crossing-tool/bin/activate

# 2. Point crossing at your project folder (created automatically)
crossing tool path ~/my-project

# 3. Set your API keys (TMDb required for metadata; others optional)
crossing tool api_key set tmdb <key>
crossing tool api_key set opensubtitles <key>

# 4. Import a film (transcodes to H.264/AAC, fetches metadata + subtitles)
crossing media import /path/to/film.mkv

# 5. Detect shot boundaries
crossing shotlist shot detect "Film Title"

# 6. Open the shot visualizer GUI (optional)
crossing shotlist --visualizer

# 7. Annotate shots with an LLM
crossing annotate shot "Film Title"

# 8. Search annotations
crossing search "close-up of a gun"
```

---

## Install

### System dependencies

```bash
sudo apt install ffmpeg
sudo apt install python3-tk   # optional — GUI file picker
```

### Create the virtual environment

```bash
python3 -m venv ~/venvs/crossing-tool
source ~/venvs/crossing-tool/bin/activate
cd /path/to/crossing-tool
pip install -e .
```

### Optional components

**Shot detection** (TransNetV2):
```bash
pip install git+https://github.com/soCzech/TransNetV2.git
pip install "tensorflow>=2.5" ffmpeg-python
```

**Shot visualizer GUI**:
```bash
pip install PyQt5 opencv-python-headless
```
> Use `opencv-python-headless` (not `opencv-python`) to avoid Qt conflicts with PyQt5.

**Compose generator** (SAM 2):
```bash
pip install ultralytics
```
> Place `sam2.1_b.pt` in `<project>/models/` before running.

### API keys

```bash
crossing tool api_key set tmdb <key>
crossing tool api_key set opensubtitles <key>
crossing tool api_key set discord <webhook-url>   # optional — batch notifications
```

### Verify

```bash
crossing tool version
```

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
crossing tool model get [annotate|segmentation|embed]        # show current model (all or one role)
crossing tool model set {annotate,segmentation,embed} <name> # set model for a role
crossing tool model list                                     # list models in project models/ folder
crossing tool model download <hf-repo-id> [--name <folder>] # download a model from HuggingFace
crossing tool model size [<name>]                            # show model disk usage
crossing tool model remove <name> [--confirm]                # delete a model folder

# Get or set persistent defaults (e.g. frames-per-shot for annotate)
crossing tool default get [key]
crossing tool default set <key> <value>

# Send a test notification to verify a service is configured
crossing tool notify discord
```

### Media

Manage content: videos, subtitles, posters, and thumbnails.

```bash
# Import video files (supports individual files, multiple files, or folders)
crossing media import <file(s)|folder>
crossing media import --pick              # open GUI file/folder picker
  --media {movie,gameplay}                # destination (default: movie)
  --platform {universal,pi5}              # encoding profile (default: universal)
  --skip-metadata                         # skip automatic metadata fetch

# Examples:
crossing media import /path/to/video.mp4
crossing media import /path/to/movies/
crossing media import --pick              # GUI picker for single/multiple files or folder

# Remove a film and all its associated files
crossing media remove [query]             # match by filename or title words
crossing media remove --tmdb 391         # match by TMDb ID
  --media {movies,gameplay}              # media type (default: movies)
  --confirm                              # actually delete (default is a dry run)

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

# Set a metadata field manually
crossing metadata set <filename_substring> <field> <value>
crossing metadata set --tmdb 391 director "Sergio Leone"
  --media {movies,gameplay}

# Update metadata (fetch from TMDb/OpenSubtitles)
crossing metadata update
  --file <filename>                 # update single file
  --force                           # re-fetch all entries (including duration)
  --media {movies}

# Validate metadata
crossing metadata validate
  --check-thumbnails                # verify thumbnails exist
  --check-subtitles                 # verify subtitles exist
  --media {movies,gameplay}

# Fix filenames (normalize to standard format)
crossing metadata fixname
  --media {movies,gameplay}

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
  --notify-items                                     # Discord notification after each item (batch only)

# Examples:
crossing shotlist shot detect Django                 # find by filename substring
crossing shotlist shot detect --tmdb 10772           # find by TMDb ID
crossing shotlist shot detect "Fistful" --force      # overwrite existing
crossing shotlist shot detect --all                  # detect shots for all movies without a shotlist
crossing shotlist shot detect --all --media gameplay # detect shots for all gameplay entries
crossing shotlist shot detect --all --force          # reprocess everything
crossing shotlist shot detect --all --notify         # notify when the whole batch finishes

# Output CSV format:
# Ignore,Scene,Start,End,Start_Frame,End_Frame,Shot_Caption,Scene_Caption,Shot_Source,Shot_Confidence
# No,0,00:00:00.000,00:00:05.123,0,123,"","",auto,0.876

# Open the shot visualizer GUI
crossing shotlist --visualizer
  --media {movies,gameplay}                     # media type (default: movies)

# Keyboard shortcuts in shot visualizer (OpenCV-based frame-precise):
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
  --limit N                         # process only first N shots
  --prompt-file <file>              # system prompt file
  --prompt-text <text>              # inline system prompt
  --verbose                         # print per-shot progress
  --log                             # write debug log file
  --notify                          # Discord notification on finish
  --notify-items                    # Discord notification after each film (batch)

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

# Open the annotation visualizer GUI
crossing annotate --visualizer
  --media {movies,gameplay}
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
```

### Generate

Generate content from project data.

#### Mosaic

Generates a contact-sheet grid image from thumbnails or representative text frames.

```bash
# Mosaic of all movie thumbnails
crossing generate mosaic thumbnails --media movies --all

# Mosaic of representative frames for all text events in one film
crossing generate mosaic text --tmdb 40575 --all

# Mosaic of persona appearances for a film
crossing generate mosaic personas "Young Guns"
crossing generate mosaic personas --tmdb 10772 --max-per-persona 4
```

**Flags (thumbnails / text):**

| Flag | Default | Description |
|------|---------|-------------|
| `--media` | `movies` | `movies` or `gameplay` |
| `--all` | — | Include all items (required for now) |
| `--layout` | `landscape` | `landscape` (wider grid) or `portrait` (taller grid) |
| `--caption` | `short` | `short` (title + year / text) or `none` |
| `--output` | auto | Full save path override |
| `--notify` | — | Discord notification when finished |

**Additional flags (personas):**

| Flag | Default | Description |
|------|---------|-------------|
| `--max-per-persona` | `6` | Maximum appearance frames per persona row |
| `--no-open` | — | Do not open the result in the desktop viewer |

Output is saved to `<project>/output/mosaics/`.

- Thumbnails mosaic: `movies-thumbnails-mosaic.png`
- Text mosaic: `tmdb-<id>-text-mosaic.png`

#### Compose

Generates an experimental poster or landscape canvas by compositing SAM-masked text
patches from one or more films on top of a randomly sampled background frame.
SAM 2 (via `ultralytics`) is required — see the install checklist.

```bash
# Single film — random everything
crossing generate compose "Sunrise"

# Multiple films via query (substring match returns >1 result)
crossing generate compose "Chaplin"

# All films that have text CSVs
crossing generate compose --all

# Landscape canvas, 12 elements, fixed seed for reproducibility
crossing generate compose --all --orientation landscape --count 12 --seed 42

# Darken the background, output as PDF, don't open automatically
crossing generate compose --all --bg-treatment darken --format pdf --no-open

# Pin the background to a specific frame number
crossing generate compose "10 000 Dollari" --bg-frame 9420

# Override canvas size (pixels)
crossing generate compose --all --width 2480 --height 3508
```

**Flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--orientation` | `portrait` | `portrait` (1240×1754) or `landscape` (1920×1080) |
| `--width` / `--height` | — | Override canvas dimensions in pixels |
| `--count` | random 6–18 | Number of text patches to composite |
| `--bg-frame` | random | Specific source frame number for the background |
| `--bg-treatment` | random | `desaturate`, `tint`, `darken`, or `original` |
| `--seed` | random | Integer seed for fully reproducible output |
| `--format` | `jpg` | `jpg` or `pdf` |
| `--output` | auto | Full save path override |
| `--no-open` | — | Skip opening the result in the desktop viewer |
| `--verbose` | — | Print per-patch progress |

Output is saved to `<project>/media/compositions/`.

### API Keys

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
| `--notify-items` | Send a message after each individual film in a batch, including elapsed time |

```bash
# Notify on batch completion
crossing shotlist shot detect --all --notify

# Notify after every film + on completion
crossing annotate shot --all --notify --notify-items
crossing shotlist shot detect --all --notify --notify-items
```

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
│   │   ├── movies.csv              # movie metadata
│   │   └── gameplay.csv            # gameplay metadata
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
│   └── compositions/               # output from `crossing generate compose`
├── output/
│   └── mosaics/                    # output from `crossing generate mosaic`
├── models/
│   └── sam2.1_b.pt                 # SAM 2 model (required for `crossing generate compose`)
└── preferences/
    ├── keys/                       # API keys
    │   ├── discord_api_key.txt
    │   ├── tmdb_api_key.txt
    │   └── opensubtitles_api_key.txt
    └── version.txt                 # data structure version
```

## Metadata Fields

Movies and gameplay metadata includes:
- `title`, `year`, `director`, `tmdb`, `imdb`
- `filename`, `duration` (actual file duration in minutes)
- `overview`, `tagline`

## Notes

- All video files are transcoded to H.264/AAC MP4 format on import
- Metadata is automatically fetched from TMDb during import
- Thumbnails and subtitles are automatically downloaded when available
- Shotlist commands accept either a full filename substring or `--tmdb <id>` for convenience
- The `--pick` flag on import opens a native GUI file picker (requires `python3-tk`)
- Use `--field` with `shotlist show` commands to extract specific fields from caption JSON (table or JSON output)
- Use `--json` flag for raw JSON output (full shot data or filtered fields with `--field`)
- Shot detection uses TransNetV2 and creates CSV files with `Shot_Source="auto"`, confidence scores, and exact frame numbers (`Start_Frame`/`End_Frame`)
- Shot visualizer GUI (`crossing shotlist --visualizer`) uses OpenCV for frame-precise display — each frame is seeked by exact integer frame index, not timecode
- `crossing index update` checks for changes in annotation files before re-serializing or re-embedding, making it safe to run repeatedly

## Requirements

- Python 3.11+
- ffmpeg: `sudo apt install ffmpeg`
- python3-tk (optional, for GUI file picker): `sudo apt install python3-tk`
- TransNetV2 (optional, for shot detection) — see Install section above
- PyQt5 + opencv-python-headless (optional, for shot validation UI) — see Install section above
