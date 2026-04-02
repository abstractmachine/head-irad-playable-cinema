# Passage

Traverses image archives and produces associations between one media format to another using machine learning models.

Passage is a CLI tool for relating moving images across media — connecting gameplay sequences to cinema sequences, live gameplay input to archived material. It manages a local project folder as its database, with no external services required. All models are downloaded and then run locally.

## Commands

### Project Setup

```bash
# Show tool and data structure versions
passage version
passage version --init              # initialize/update data version

# Get or set the active project folder
passage path [folder]

# Get or set the project name
passage name [name]
```

### Import Media

```bash
# Import video files (supports individual files, multiple files, or folders)
passage import <file(s)|folder>
passage import --pick               # open GUI file/folder picker
  --media {movie,gameplay}          # destination (default: movie)
  --platform {universal,pi5}        # encoding profile (default: universal)
  --skip-metadata                   # skip automatic metadata fetch

# Examples:
passage import /path/to/video.mp4
passage import /path/to/movies/
passage import --pick               # GUI picker for single/multiple files or folder
```

### Metadata Management

```bash
# List all metadata entries
passage metadata list
  --year 1966                       # filter by year
  --director "Sergio Leone"         # filter by director (substring)
  --fields title,year,director      # show specific fields only
  --sort year                       # sort by field
  --reverse                         # reverse sort order
  --media {movies,gameplay}         # media type (default: movies)

# Get metadata (all, by index, or by filename substring)
passage metadata get [query]
  --media {movies,gameplay}

# Update metadata (fetch from TMDb/OpenSubtitles)
passage metadata update
  --file <filename>                 # update single file
  --force                           # re-fetch all entries (including duration)
  --media {movies,gameplay}

# Validate metadata
passage metadata validate
  --check-thumbnails                # verify thumbnails exist
  --check-subtitles                 # verify subtitles exist
  --media {movies,gameplay}

# Fix filenames (normalize to standard format)
passage metadata fixname
  --media {movies,gameplay}

# Count entries
passage metadata count
  --media {movies,gameplay}

# Remove orphaned entries (no matching video file)
passage metadata prune
  --confirm                         # actually remove (default: dry run)
  --media {movies,gameplay}
```

### Shot Detection

```bash
# Detect shot boundaries automatically using TransNetV2
passage shot detect <filename_substring>
passage shot detect --tmdb 391             # use TMDb ID
  --media {movies,gameplay}                # media type (default: movies)
  --force                                  # overwrite existing shotlist

# Examples:
passage shot detect Django                 # find by filename substring
passage shot detect --tmdb 10772           # find by TMDb ID
passage shot detect "Fistful" --force      # overwrite existing

# Output CSV format:
# Ignore,Scene,Start,End,Start_Frame,End_Frame,Shot_Caption,Scene_Caption,Shot_Source,Shot_Confidence
# No,0,00:00:00.000,00:00:05.123,0,123,"","",auto,0.876

# Validate and correct detected shots (launches GUI)
passage shot validate <filename_substring>
passage shot validate --tmdb 56966         # use TMDb ID
  --media {movies,gameplay}                # media type (default: movies)

# Keyboard shortcuts in validator (OpenCV frame-precise):
# Space      - Play/Pause
# ↑/↓        - Previous/Next shot (resumes playback if was playing)
# ←/→        - Step one frame backward/forward
# Shift+←/→  - Step one second backward/forward
# E          - Jump to end frame of current shot
# F          - Toggle Ignore flag on current shot
# M          - Merge current shot with previous
# N          - Split current shot at current frame (creates new shot boundary)
# Ctrl+S     - Save changes
# Continue button - toggle playback past shot boundaries (ON/OFF)
```

### Shotlist Management

```bash
# List all available shotlists
passage shotlist list
  --media {movies,gameplay}         # filter by media type
  --json                            # output as JSON

# Get shotlist data for a file
passage shotlist get <filename>
passage shotlist get --tmdb 391     # use TMDb ID instead of filename
  --scene 0                         # filter by scene number
  --media {movies,gameplay}

# Annotate a specific shot
passage shotlist annotate shot <filename> <shot_index> "caption"
passage shotlist annotate shot --tmdb 391 5 "Close-up of gun"
  --media {movies,gameplay}

# Annotate all shots in a scene
passage shotlist annotate scene <filename> <scene_number> "caption"
passage shotlist annotate scene --tmdb 391 0 "Opening sequence"
  --media {movies,gameplay}

# Show specific shot data
passage shotlist show shot <filename> <shot_index>
passage shotlist show shot --tmdb 391 52
  --media {movies,gameplay}
  --field protagonists place actions        # extract specific fields (table output)
  --json                                    # output as JSON

# Show all shots in a scene
passage shotlist show scene <filename> <scene_number>
passage shotlist show scene --tmdb 391 1
  --media {movies,gameplay}
  --field protagonists actions
  --json
```

### API Keys

```bash
# Get stored API key
passage api_key get {openai,opensubtitles,tmdb}

# Set API key
passage api_key set {openai,opensubtitles,tmdb} <key>
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
- `shotlist` (true/false) - whether shotlist CSV exists
- `encodings` (true/false) - whether .npy and .txt encodings exist

## Notes

- All video files are transcoded to H.264/AAC MP4 format on import
- Metadata is automatically fetched from TMDb during import
- Thumbnails and subtitles are automatically downloaded when available
- Shotlist commands accept either full filename or `--tmdb <id>` for convenience
- The `--pick` flag on import opens a native GUI file picker (requires python3-tk)
- Use `--field` with shotlist show commands to extract specific fields from caption JSON (table or JSON output)
- Use `--json` flag for raw JSON output (full shot data or filtered fields with `--field`)
- Shot detection uses TransNetV2 and creates CSV files with Shot_Source="auto", confidence scores, and exact frame numbers (Start_Frame/End_Frame)
- Shot validation GUI (`passage shot validate`) uses OpenCV for frame-precise display — each frame is seeked by exact integer frame index, not timecode

## Requirements

- Python 3.11+
- ffmpeg: `sudo apt install ffmpeg`
- python3-tk (optional, for GUI file picker): `sudo apt install python3-tk`
- TransNetV2 (optional, for shot detection) - see Install section below
- PyQt5 + opencv-python-headless (optional, for shot validation UI) - see Install section below

## Virtual Python Environment

**Create** (first time only):
```bash
python3 -m venv ~/venvs/playable-tool
source ~/venvs/playable-tool/bin/activate
```

**Load** (each new terminal session):
```bash
source ~/venvs/playable-tool/bin/activate
```

**Deactivate** when done:
```bash
deactivate
```

## Install

Navigate to the `passage` code directory:

```bash
pip install -e .
```

**For shot detection:**
```bash
pip install git+https://github.com/soCzech/TransNetV2.git
pip install tensorflow>=2.5 ffmpeg-python
```

**For shot validation UI:**
```bash
pip install PyQt5 opencv-python-headless
```

> Use `opencv-python-headless` (not `opencv-python`) to avoid Qt plugin conflicts with PyQt5.
