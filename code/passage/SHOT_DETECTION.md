# Shot Detection with TransNetV2

This document describes the automatic shot boundary detection feature.

## Overview

The `passage shot detect` command automatically generates shotlists using **TransNetV2**, a deep learning model specifically trained for shot boundary detection.

## Installation

### Install TransNetV2 and Dependencies

TransNetV2 must be installed from GitHub, and requires TensorFlow and ffmpeg-python:

```bash
pip install git+https://github.com/soCzech/TransNetV2.git
pip install tensorflow>=2.5 ffmpeg-python
```

This installs:
- TransNetV2 model code
- TensorFlow (~500MB+ download)
- ffmpeg-python (Python wrapper for ffmpeg)
- Required dependencies

**Note:** The `ffmpeg` command-line tool must already be installed on your system (`sudo apt install ffmpeg`).

### Verify Installation

```bash
python -c "from transnetv2 import TransNetV2; print('TransNetV2 ready')"
```

## Usage

### Basic Command

```bash
passage shot detect <query>
```

Where `<query>` can be:
- A filename substring: `passage shot detect Django`
- A TMDb ID: `passage shot detect --tmdb 10772`

### Options

- `--media {movies,gameplay}` - Media type (default: movies)
- `--force` - Overwrite existing shotlist if it exists

### Examples

```bash
# Detect shots by filename
passage shot detect "Fistful of Dollars"

# Detect shots by TMDb ID
passage shot detect --tmdb 391

# Force overwrite existing shotlist
passage shot detect Django --force

# Detect shots for gameplay footage
passage shot detect --tmdb 12345 --media gameplay
```

## Output Format

The command creates a CSV file at:
```
data/shotlists/{media_type}/{filename}.csv
```

### CSV Structure

```csv
Ignore,Scene,Start,End,Shot_Caption,Scene_Caption,Shot_Source,Shot_Confidence
No,0,00:00:05.040,00:00:09.480,"","",auto,0.876
No,0,00:00:09.480,00:00:15.360,"","",auto,0.923
```

Fields:
- **Ignore**: "No" (default) or "Yes"  
- **Scene**: 0 (placeholder for future scene detection)
- **Start**: Timecode in HH:MM:SS.mmm format
- **End**: Timecode in HH:MM:SS.mmm format
- **Shot_Caption**: Empty (ready for manual annotation)
- **Scene_Caption**: Empty (ready for manual annotation)
- **Shot_Source**: "auto" (indicates automatic detection)
- **Shot_Confidence**: 0.000-1.000 (boundary detection confidence)

## How It Works

1. **Video Loading**: Loads video from `media/videos/{media_type}/`
2. **Frame Analysis**: TransNetV2 analyzes frames and predicts boundaries
3. **Threshold**: Applies 0.5 threshold to boundary probabilities
4. **Timecode Conversion**: Converts frame numbers to timecodes
5. **CSV Export**: Writes results to standardized CSV format

## Error Handling

The command will fail with clear messages if:
- Query matches no files or multiple files
- Video file not found
- Shotlist already exists (use `--force`)
- TransNetV2 not installed
- Video cannot be processed

## Integration with Existing Commands

After detection, you can:

### View shots
```bash
passage shotlist show shot --tmdb 391 0
passage shotlist show scene --tmdb 391 1
```

### Add annotations
```bash
passage shotlist annotate shot --tmdb 391 0 "Opening credits"
passage shotlist annotate scene --tmdb 391 1 "Opening sequence"
```

### Query with field filters
```bash
passage shotlist show shot --tmdb 391 52 --field protagonists place
```

## Performance Notes

- Processing time depends on video length and resolution
- Typical speed: ~30-50 fps on modern hardware  
- GPU acceleration available with TensorFlow-GPU
- First run downloads the TransNetV2 model weights (~20MB)

## Limitations

- Only detects hard cuts (not dissolves or fades)
- Scene numbers are placeholders (0) - manual grouping required
- No automatic caption generation
- Confidence scores are frame-level, not averaged over shots

## Next Steps

After automatic detection:
1. Review shots using `passage shotlist show`
2. Add manual annotations with `passage shotlist annotate`
3. Group shots into scenes (manual editing of CSV or future feature)
4. Use `--field` filters to query and analyze shot metadata
