# Crossing

Traverses image archives and produces associations between one media format to another using machine learning models.

Crossing is a CLI + GUI tool for relating moving images across media — connecting gameplay sequences to cinema sequences, live gameplay input to archived material. It manages a local project folder as its database, with no external services required. All models are downloaded and then run locally.

## Commands


### Load
```bash
source ~/venvs/crossing-tool/bin/activate
```

### Tool Setup

```bash
# Show tool and data structure versions
crossing tool version
crossing tool version --init         # initialize/update data version

# Get or set the active project folder
crossing tool path [folder]

# Get or set the project name
crossing tool name [name]
```

### Import Media

```bash
# Import video files (supports individual files, multiple files, or folders)
crossing import <file(s)|folder>
crossing import --pick              # open GUI file/folder picker
  --media {movie,gameplay}          # destination (default: movie)
  --platform {universal,pi5}        # encoding profile (default: universal)
  --skip-metadata                   # skip automatic metadata fetch

# Examples:
crossing import /path/to/video.mp4
crossing import /path/to/movies/
crossing import --pick               # GUI picker for single/multiple files or folder
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

# Annotate a specific shot
crossing shotlist annotate shot <filename> <shot_index> "caption"
crossing shotlist annotate shot --tmdb 391 5 "Close-up of gun"
  --media {movies,gameplay}

# Annotate all shots in a scene
crossing shotlist annotate scene <filename> <scene_number> "caption"
crossing shotlist annotate scene --tmdb 391 0 "Opening sequence"
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

# Examples:
crossing shotlist shot detect Django                 # find by filename substring
crossing shotlist shot detect --tmdb 10772           # find by TMDb ID
crossing shotlist shot detect "Fistful" --force      # overwrite existing
crossing shotlist shot detect --all                  # detect shots for all movies without a shotlist
crossing shotlist shot detect --all --media gameplay # detect shots for all gameplay entries
crossing shotlist shot detect --all --force          # reprocess everything

# Output CSV format:
# Ignore,Scene,Start,End,Start_Frame,End_Frame,Shot_Caption,Scene_Caption,Shot_Source,Shot_Confidence
# No,0,00:00:00.000,00:00:05.123,0,123,"","",auto,0.876

# Validate and correct shot/scene data (launches GUI)
crossing shotlist validate <filename_substring>
crossing shotlist validate --tmdb 56966         # use TMDb ID
crossing shotlist validate --all                # validate all movies with shotlists
  --media {movies,gameplay}                     # media type (default: movies)

# Keyboard shortcuts in validator (OpenCV-based frame-precise):
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

### Text Detection

```bash
# Detect on-screen text events for a single film
crossing text detect <filename_substring>
crossing text detect --tmdb 391             # use TMDb ID
  --media {movies,gameplay}                 # media type (default: movies)
  --force                                   # overwrite existing CSV
  --sample-fps 1.0                          # frames per second to sample (default: 1.0)
  --lang en                                 # PaddleOCR language code (default: en)
  --verbose                                 # print per-frame OCR output

# Detect text events for all films
crossing text detect --all
crossing text detect --all --force          # reprocess everything
crossing text detect --silent               # run on the six silent test-bed films only

# List all text CSVs
crossing text list
  --media {movies,gameplay}                 # filter by media type
  --json                                    # output as JSON

# Validate and edit text events (GUI)
crossing text validate <filename_substring>
crossing text validate --tmdb 391
crossing text validate --all                # validate all films with text CSVs
  --media {movies,gameplay}
```

### Audit

```bash
# Show which entries are missing metadata, shotlists, or subtitles
crossing audit
  --media {movies,gameplay}         # media type (default: movies)

# Examples:
crossing audit                       # report for all movies
crossing audit --media gameplay      # report for gameplay entries
```

### API Keys

```bash
# Get stored API key
crossing tool api_key get {openai,opensubtitles,tmdb}

# Set API key
crossing tool api_key set {openai,opensubtitles,tmdb} <key>
```

## Project Folder Structure

```
<project>/
├── data/
│   ├── metadata/
│   │   ├── movies.csv              # movie metadata
│   │   └── gameplay.csv            # gameplay metadata
│   └── shotlists/
│       ├── movies/                 # shot-level data for movies
│       │   ├── <filename>.csv      # shot timecodes and annotations
│       │   ├── <filename>.npy      # visual encodings
│       │   └── <filename>.txt      # encoding metadata
│       └── gameplay/               # shot-level data for gameplay
├── media/
│   ├── videos/
│   │   ├── movies/                 # imported movie files
│   │   └── gameplay/               # imported gameplay footage
│   ├── thumbnails/
│   │   ├── movies/                 # movie posters from TMDb
│   │   └── gameplay/               # gameplay thumbnails
│   └── subtitles/
│       ├── movies/                 # English subtitles from OpenSubtitles
│       └── gameplay/               # gameplay subtitles
└── preferences/
    ├── keys/                       # API keys
    │   ├── tmdb_api_key.txt
    │   ├── opensubtitles_api_key.txt
    │   └── openai_api_key.txt
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
- Shotlist commands accept either full filename or `--tmdb <id>` for convenience
- The `--pick` flag on import opens a native GUI file picker (requires python3-tk)
- Use `--field` with shotlist show commands to extract specific fields from caption JSON (table or JSON output)
- Use `--json` flag for raw JSON output (full shot data or filtered fields with `--field`)
- Shot detection uses TransNetV2 and creates CSV files with Shot_Source="auto", confidence scores, and exact frame numbers (Start_Frame/End_Frame)
- Shot validation GUI (`crossing shot validate`) uses OpenCV for frame-precise display — each frame is seeked by exact integer frame index, not timecode
- Text detection uses PaddleOCR 3.x with PP-OCRv5 models (GPU-accelerated via PaddlePaddle). Samples at 1 fps by default; each frame is upscaled 2× before OCR; adjacent frames with matching text are merged into a single timed event

## Requirements

- Python 3.11+
- ffmpeg: `sudo apt install ffmpeg`
- python3-tk (optional, for GUI file picker): `sudo apt install python3-tk`
- TransNetV2 (optional, for shot detection) - see Install section below
- PyQt5 + opencv-python-headless (optional, for shot validation UI) - see Install section below
- PaddleOCR 3.x + paddlepaddle-gpu (for text detection) — requires CUDA; GPU strongly recommended for batch processing

## Fresh Install Checklist

Follow these steps when setting up from scratch in a new environment.

### 1. System dependencies
- [ ] `sudo apt install ffmpeg`
- [ ] `sudo apt install python3-tk` *(optional — for GUI file picker)*

### 2. Create the virtual environment
- [ ] `python3 -m venv ~/venvs/crossing-tool`

### 3. Activate and install the package
- [ ] `source ~/venvs/crossing-tool/bin/activate`
- [ ] `cd /path/to/crossing-tool` *(your local clone of this repo)*
- [ ] `pip install -e .`

### 4. Shot detection *(optional)*
- [ ] `pip install git+https://github.com/soCzech/TransNetV2.git`
- [ ] `pip install tensorflow>=2.5 ffmpeg-python`

### 5. Shot validation UI *(optional)*
- [ ] `pip install PyQt5 opencv-python-headless`

> Use `opencv-python-headless` (not `opencv-python`) to avoid Qt plugin conflicts with PyQt5.

### 6. Text detection *(required for `crossing text detect`)*
- [ ] `pip install paddlepaddle-gpu==3.2.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu130/`
- [ ] `pip install "paddleocr>=3.2"`

> Requires CUDA 13.0 and a CUDA-capable GPU. The PP-OCRv5 models are downloaded automatically on first run to `~/.paddlex/official_models/`. CPU-only installs are not supported — use `paddlepaddle` (non-GPU) and remove `device="gpu"` from the engine if needed.

### 7. API keys *(as needed)*
- [ ] `crossing tool api_key set tmdb <key>`
- [ ] `crossing tool api_key set opensubtitles <key>`
- [ ] `crossing tool api_key set openai <key>`

### 8. Verify
- [ ] `crossing tool version`

## Virtual Python Environment

**Create** (first time only):
```bash
python3 -m venv ~/venvs/crossing-tool
source ~/venvs/crossing-tool/bin/activate
```

**Load** (each new terminal session):
```bash
source ~/venvs/crossing-tool/bin/activate
```

**Deactivate** when done:
```bash
deactivate
```
