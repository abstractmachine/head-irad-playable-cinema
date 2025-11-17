# Playable Cinema Multi-Tool

A comprehensive desktop and command-line application for video analysis, annotation, and scene detection. Built with PyQt5, this tool provides both a GUI and CLI for annotating videos with AI-powered scene detection and metadata tagging.

## Features

- **Dual Interface**: Full-featured GUI and command-line interface
- **Video Playback**: VLC-based video player with precise timeline control
- **Scene Detection**: Automated shot boundary detection using PySceneDetect
- **AI Annotation**: OpenAI GPT-4 Vision integration for automated scene description
- **Project Management**: Organized workspace with movies, metadata, subtitles, and annotations
- **Multi-window Interface**: Tabbed interface with specialized tools for different workflows

## Installation
You can install the latest release version of this tool here:

[Playable-Cinema-Tool](https://github.com/abstractmachine/head-irad-playable-cinema/releases/latest)

### Prerequisites

- **Python**: 3.11.9+ (recommended with pyenv)
- **FFmpeg**: Required for video processing
- **VLC**: Required for video playback in GUI mode

### macOS Setup

1. **Install System Dependencies**
   ```bash
   brew install ffmpeg
   brew install --cask vlc
   ```

2. **Create Python Environment**
   ```bash
   cd /path/to/playable-tool
   pyenv virtualenv 3.11.9 playable-tool
   pyenv activate playable-tool
   ```

3. **Install Python Dependencies**
   ```bash
   pip install --upgrade -r requirements.txt
   ```

### Linux Setup

1. **Install System Dependencies**
   ```bash
   sudo apt install ffmpeg vlc
   ```

2. **Create Python Environment** (follow macOS step 2-3)

### Windows Setup

1. **Install FFmpeg and VLC** manually from their respective websites
2. **Create Python Environment** using venv or pyenv
3. **Install Python Dependencies** (follow macOS step 3)

## Quick Start

### GUI Mode

```bash
pyenv activate playable-tool
python app.py
```

### CLI Mode

```bash
pyenv activate playable-tool
python cli.py --help
```

Example CLI usage:
```bash
# Detect scenes in a video
python cli.py detect --video path/to/movie.mp4 --output shotlist.csv

# Annotate shots with AI
python cli.py annotate --video path/to/movie.mp4 --shotlist shotlist.csv
```

## Project Structure

Click the `Folder` button in the `Cinemathèque` window to set your project folder. The application will automatically create and verify the required folder structure:

```
project-folder/
├── movies/           # Video files (.mp4)
├── metadata/         # Movie metadata (metadata.csv)
├── posters/          # Movie poster images
├── shotlists/        # Scene detection results (.csv)
├── prompts/          # Movie-specific AI prompts (.txt)
├── subtitles/        # Subtitle files (.srt)
├── datasets/         # Training datasets
├── gameplay/         # Gameplay recordings
└── preferences/      # API keys and configuration
```

## GUI Components

### 1. Cinemathèque Window
- Project and movie management
- Automatic metadata retrieval from TMDB
- Poster and subtitle downloading

### 2. Player Window
- Frame-accurate video playback
- Timeline scrubbing and seeking
- Keyboard shortcuts (Space: play/pause, Arrow keys: seek)

### 3. Shotlist Window
- Scene/shot detection and management
- Shot-by-shot captions and annotations
- Configurable detection algorithms

### 4. Annotate Window
- OpenAI GPT-4o Vision integration
- Manual and automated captioning
- Bot mode for batch processing

### 5. Prompt Management
- Default system prompts
- Movie-specific prompt overrides
- Template tags for dynamic content

#### Prompt Tags
- Movie Info: `{title}`, `{year}`, `{director}`, `{description}`, `{tagline}`
- Subtitle content: `{shot-subtitles}`, `{full-subtitles}`
- Images: `{image-count}`

## Scene Detection

Powered by [PySceneDetect](https://www.scenedetect.com) with multiple detection algorithms:

### Detection Methods

#### detect-adaptive (Recommended)
Best for variable content with mixed shot types. Default threshold: `-t 3.0`

#### detect-content
General content-based detection. Default threshold: `-t 27.0`

#### detect-hist
Color-based scene changes using histogram analysis.

#### detect-threshold
Simple brightness-based detection for fades and cuts.

See the [PySceneDetect documentation](https://www.scenedetect.com/docs/latest/cli.html#detectors) for detailed options.

## AI Configuration

### API Keys Required

Create a project folder, then add API keys to `preferences/` folder:

- `openai_api_key.txt` - [OpenAI API key](https://platform.openai.com/api-keys)
- `tmdb_api_key.txt` - [TMDB API key](https://developer.themoviedb.org/docs/getting-started)
- `opensubtitles_api_key.txt` - [OpenSubtitles API key](https://forum.opensubtitles.com/)

### Bot Mode

Automated batch processing:
1. Calls OpenAI API for current shot
2. Submits generated caption
3. Moves to next shot
4. Repeats until complete

## Keyboard Shortcuts

### Global
- **Space**: Play/Pause
- **L** or **V**: Load video
- **Left/Right Arrow**: Seek (hold Shift for fast seek)

### Annotate Window
- **A**: Submit caption
- **O**: Send to OpenAI
- **B**: Toggle bot mode
- **N**: Next shot

## System Requirements

- **Python**: 3.11.9+
- **OS**: macOS, Linux, Windows
- **RAM**: 4GB+ recommended
- **Storage**: Varies by project size

## Troubleshooting

### FFmpeg Not Found
```bash
# macOS
brew install ffmpeg
# Linux
sudo apt install ffmpeg
```

### API Errors
- Verify API keys in `preferences/` folder
- Check internet connection
- Ensure proper file permissions

## Acknowledgments

- [PySceneDetect](https://www.scenedetect.com) for scene detection
- [OpenAI](https://openai.com) for GPT-4 Vision API
- [TMDB](https://www.themoviedb.org) for movie metadata
- [OpenSubtitles](https://www.opensubtitles.org) for subtitle data