# Project Folder Structure

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
├── outputs/
│   ├── mosaics/
│   │   ├── scenes/                 # output from `crossing generate mosaic scenes`
│   │   ├── shots/                  # output from `crossing generate mosaic shots`
│   │   ├── searches/               # output from `crossing generate mosaic search`
│   │   ├── images/                 # output from `crossing generate mosaic export`
│   │   └── videos/                 # output from `crossing generate mosaic video`
│   ├── clouds/                     # output from `crossing generate cloud`
│   ├── compositions/               # output from `crossing generate composition`
│   └── audits/                     # timestamped reports from `crossing index untyped`
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

---

# Metadata Fields

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

---

# Notes

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
- `crossing index process` checks for changes in annotation files before re-serializing or re-embedding, making it safe to run repeatedly
- Annotation IDs use a stable `<media_id>@fSTART-fEND` format; use `crossing annotate migrate` to upgrade existing projects from legacy integer IDs
- The **metadata visualizer** communicates with a running Shotlist Visualizer via a Unix domain socket (`/tmp/crossing_shotlist_<hash>.sock`); the socket is created when the Shotlist Visualizer opens and removed when it closes

---

# Requirements

- **ffmpeg** (system): `sudo apt install ffmpeg` — required for video transcoding

Python 3.11+ and all Python packages are managed automatically by `uv` — no manual installation needed.
