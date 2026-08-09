# Commands

## Tool Setup

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

## Media

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

## Metadata Management

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

## Shotlist Management

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

# Detect scene boundaries automatically from shot embeddings
crossing shotlist scene detect <filename_substring>
crossing shotlist scene detect --tmdb 391
  --media {movies,gameplay}         # media type (default: movies)
  --force                           # overwrite existing Scene values
  --dry-run                         # show proposed boundaries without writing
  --verbose                         # print boundary positions (shot indices)
  --all                             # process all available shotlists

# Examples:
crossing shotlist scene detect Django
crossing shotlist scene detect --tmdb 10772
crossing shotlist scene detect "Fistful" --dry-run --verbose
crossing shotlist scene detect --all
crossing shotlist scene detect --all --force
```

## Keyboard Shortcuts

See [shortcuts.md](shortcuts.md) for the full keyboard reference for all visualizers.

## Visualizers

All visualizer GUIs share the same theme and keyboard navigation keys, which are defined centrally in `tool/shortcuts.py`. They all support **Ctrl+Q** / **Ctrl+W** to close. See [shortcuts.md](shortcuts.md) for the full reference.

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
```

The **project** visualizer lets you:
- Set (via folder picker) and display the current project path
- Adjust annotation defaults (frames-per-shot, min-frame-interval, max-frames-per-shot)
- Select models for each role (annotate, segmentation, embed) from installed local models
- Launch any of the other visualizers from a grid (Metadata, Shotlist, Mosaic, and more)

The **metadata** visualizer shows all movies and gameplay as scrollable card tiles in two columns. Each card displays a thumbnail, title, year/director (movies) or game (gameplay), and a short overview. Cards highlight yellow on hover; clicking a card opens that film in the Shotlist Visualizer. If the Shotlist Visualizer is already open it receives the film via an IPC socket (no second window is opened); if not, a new window is launched.

The **shotlist** visualizer always populates its film selector with the full list of all films in the project for the active media type, regardless of how many were specified on the command line.

## Annotate

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

## Search

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

## Index

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

# Rebuild both compact browse indexes used by the Illustration visualizer
crossing index illustration
crossing index illustration --media gameplay
```

## Generate

Generate content from project data.

### Mosaic

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

# PDF contact sheet of every shot in a movie (one frame per shot)
crossing generate mosaic shots --movie "Film Title"
crossing generate mosaic shots --all               # one PDF per movie
  --best                            # use precomputed CLIP best-frame PNGs
  --media {movies,gameplay}         # media type (default: movies)
  --output <path>                   # override output path (single movie only)
  --no-open                         # do not open result in desktop viewer
  --verbose                         # print per-frame progress
  --notify                          # Discord notification when batch finishes
  --notify-each                     # Discord notification after each movie (--all)

# PDF contact sheet grouped by scene, with title card + scene-number intertitles
crossing generate mosaic scenes --movie "Film Title"
crossing generate mosaic scenes --all              # one PDF per movie
  --best                            # use precomputed CLIP best-frame PNGs
  --media {movies,gameplay}         # media type (default: movies)
  --output <path>                   # override output path (single movie only)
  --no-open                         # do not open result in desktop viewer
  --verbose                         # print per-frame progress
  --notify                          # Discord notification when batch finishes
  --notify-each                     # Discord notification after each movie (--all)
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

Output is saved to `<project>/output/mosaics/` (`searches/` sub-folder for search results, `shots/` for shot PDFs, `scenes/` for scene PDFs).

#### Mosaic flags (shots / scenes):

| Flag | Default | Description |
|------|---------|-------------|
| `--movie` | — | Fuzzy movie title (required unless `--all` is used) |
| `--all` | — | Generate a PDF for every movie in the project |
| `--best` | — | Use precomputed CLIP best-frame PNGs instead of raw first frames |
| `--media` | `movies` | `movies` or `gameplay` |
| `--output` | auto | Full save path override (single-movie only) |
| `--no-open` | — | Skip opening the result in the desktop viewer |
| `--verbose` | — | Print per-frame loading progress |
| `--notify` | — | Discord notification when the batch finishes |
| `--notify-each` | — | Discord notification after each movie (batch only) |

### Composition

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
```

**Flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--orientation` | `portrait` | `portrait` (1240×1754) or `landscape` (1920×1080) |
| `--output` | auto | Full save path override |
| `--no-open` | — | Skip opening the result in the desktop viewer |
| `--notify` | — | Discord notification when done |

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

## API Keys

```bash
# Get stored API key
crossing tool api_key get {discord,opensubtitles,tmdb}

# Set API key
crossing tool api_key set {discord,opensubtitles,tmdb} <key>
```

## Models

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

## Discord Notifications

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

## MCP Server

`mcp_server/mcp_server.py` exposes crossing data to LLMs via the [Model Context Protocol](https://modelcontextprotocol.io). This lets you query your film library directly from Claude Desktop (or any MCP-compatible client) without leaving the chat.

See [mcp.md](mcp.md) for tool reference, server setup, and client configuration.
