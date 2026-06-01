# Source Code Structure

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
│   ├── scene_detection.py          # Embedding-based scene boundary detection
│   ├── search.py                   # Semantic and keyword annotation search
│   └── transcode.py                # ffmpeg transcoding and thumbnail extraction
├── styles/
│   ├── theme.py                    # Qt stylesheet and colour constants
│   ├── fonts/                      # Bundled variable fonts (Hanken Grotesk, Roboto family)
│   └── icons/                      # UI icons
├── tool/
│   ├── helpers.py                  # Shared argparse helpers (_add_verbose_arg, etc.)
│   ├── prefs.py                    # JSON preferences store
│   └── shortcuts.py                # Central keyboard shortcut key constants (all visualizers)
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

## Key Functions by Module

### `data/annotate.py`

| Function | Description |
|---|---|
| `annotate_file_shots(project_path, filename, ...)` | Runs VLM annotation over all shots in one video, writing canonical JSON output |
| `annotate_all_files(project_path, media_type, ...)` | Batch-annotates every registered file in a media library |
| `sample_frames_for_shot(video_path, start_time, end_time, ...)` | Extracts representative JPEG frames from a shot's time range via ffmpeg |
| `get_annotation_json_path(project_path, filename, media_type)` | Returns the canonical annotation JSON path for a given video |
| `reindex_annotations_for_merge(...)` | Removes annotation entries invalidated by a shot merge |
| `reindex_annotations_for_split(...)` | Removes annotation entries invalidated by a shot split |

### `data/index.py`

| Function | Description |
|---|---|
| `serialize_annotation_item(item, mapping)` | Converts one annotation entry into a pipe-separated text line for indexing |
| `embed_texts(texts, model_name, project_path, ...)` | Generates mean-pooled transformer embeddings for a list of text strings |
| `load_mapping(project_path)` | Loads the field → serialization config from `preferences/data/mapping.yaml` |
| `load_fields(project_path)` / `save_fields(...)` | Reads/writes the ordered display field list from `preferences/data/fields.yaml` |
| `write_text_file(...)` / `write_embeddings(...)` | Persists serialized text and embedding arrays to disk |

### `data/media_id.py`

| Function | Description |
|---|---|
| `compute_media_id(record, media_type)` | Stable deterministic ID preferring TMDb/YouTube/Vimeo IDs, falling back to SHA-256 |
| `build_shot_id(media_id, start_frame, end_frame)` | Returns `<media_id>@fSTART-fEND` canonical shot identifier |
| `parse_shot_id(shot_id)` | Parses a shot_id string back into `(media_id, start_frame, end_frame)` |

### `data/metadata.py`

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

### `data/shot_detection.py`

| Function | Description |
|---|---|
| `detect_shots_transnet(video_path)` | Detects shot boundaries with TransNetV2, returning dicts with start/end time, frame numbers, and confidence |
| `write_shotlist_csv(project_path, filename, shots, media_type, force)` | Writes detected shots to a CSV at the canonical shotlist path |

### `data/shotlist.py`

| Function | Description |
|---|---|
| `read_shotlist(project_path, filename, media_type)` | Reads a shotlist CSV and returns normalized shot dicts |
| `write_shotlist(project_path, filename, media_type, shots)` | Writes shot dicts back to the shotlist CSV |
| `attach_shot_ids(shots, media_id)` | Attaches stable `shot_id` strings to each shot dict in-place |
| `resolve_filename(project_path, tmdb_id, filename, media_type)` | Resolves a TMDb ID or partial filename to the exact on-disk filename |
| `normalize_shot_fields(shot)` | Renames legacy CSV column names to canonical equivalents |

### `generators/mosaic.py`

| Function | Description |
|---|---|
| `mosaic_from_search_results(results, project_path, output_path, ...)` | Builds a mosaic grid PNG from `search_shots()` results |
| `export_frames_from_search_results(results, project_path, query, ...)` | Exports each search result as a numbered JPEG with a metadata info bar |
| `render_mosaic(items, output_path, layout, ...)` | Renders a list of `MosaicItem` objects into a single contact-sheet PDF or PNG |
| `extract_frame_pil(video_path, frame_index)` | Extracts a single video frame by index as an RGB PIL Image |
| `build_shots_results(project_path, filename, *, best_mode)` | Returns tile result dicts for every shot in a film — mirrors `AllShotsWorker` / `BestOnlyWorker` |
| `build_scenes_results(project_path, filename, *, best_mode)` | Returns tile result dicts grouped by scene, including title and scene-number intertitle entries |
| `results_to_mosaic_items(results, project_path)` | Converts a result dict list to `MosaicItem` objects: label tiles → `make_intertitle_item`, ignored frames skipped |
| `mosaic_pdf_from_shots(project_path, filename, *, best_mode, output_path, verbose, progress_cb)` | CLI entry point: builds a shots PDF via `build_shots_results` → `results_to_mosaic_items` → `render_mosaic` |
| `mosaic_pdf_from_scenes(project_path, filename, *, best_mode, output_path, verbose, progress_cb)` | CLI entry point: builds a scenes PDF via `build_scenes_results` → `results_to_mosaic_items` → `render_mosaic` |
| `make_intertitle_item(text, width, height, caption, *, is_title, movie_year)` | Renders a grey intertitle tile using Libre Clarendon fonts (title card or scene-number card) |

### `generators/cloud.py`

| Function | Description |
|---|---|
| `cloud_from_annotations(project_path, scope, field, media_type, ...)` | Shared entry point: counts words from annotation JSON(s) and calls `render_cloud()` to produce a PDF |
| `extract_annotation_words(project_path, scope, field, media_type, min_count)` | Loads annotation JSON files, tokenises all text, strips stopwords, and returns a word-frequency `Counter` |
| `render_cloud(words, output_path, width, height, max_words, ...)` | Lays out words on an Archimedean spiral with log-frequency font sizes and saves as PDF or PNG |

### `generators/composition.py`

| Function | Description |
|---|---|
| `build_tableau(result, project_path, orientation)` | Extracts the mid-shot frame for a result and scale-fills it onto a canvas, returning a PIL Image |
| `save_tableau(img, criteria, output_dir)` | Saves the image as `<criteria>+<date>+<time>.jpg` |
| `choose_background(results, seed)` | Picks one random result from a `search_shots()` results list |

### `services/search.py`

| Function | Description |
|---|---|
| `search_shots(query, scopes, field, limit, ...)` | Searches annotation JSONs by keyword with per-field filtering, relevance scoring, and scope resolution |
| `vocabulary_from_field(field, scopes, ...)` | Enumerates distinct values in an annotation field across matching films |

### `services/transcode.py`

| Function | Description |
|---|---|
| `transcode_file(src, project_path, media_type, platform)` | Transcodes a video to H.264/AAC MP4 with a named platform profile |
| `extract_video_thumbnail(video_path, thumb_path)` | Extracts a JPEG thumbnail from ~5% into the video via ffmpeg |

### `services/audio_normalize.py`

| Function | Description |
|---|---|
| `measure_audio_gain_db(video_path, target_lufs)` | Runs ffmpeg loudnorm and returns `(gain_db, integrated_lufs)` |
| `compute_audio_gain_db(integrated_lufs, target_lufs)` | Computes the dB gain offset to reach a target LUFS |

### `services/frame_match.py`

| Function | Description |
|---|---|
| `annotate_best_frames(project_path, filename, media_type, model_name, force, verbose)` | Iterates all shots in a film's annotation JSON, finds the best-matching frame for each via CLIP, saves it as a PNG, and stores `best_frame` metadata in the JSON |
| `find_query_best_frame_for_shot(video_path, start_time, end_time, query, model, processor, device)` | Samples frames from a shot's time range and returns `(frame_index, score)` for the frame most similar to the query text |
| `best_frame_path(project_path, media_type, filename, shot_id)` | Returns the deterministic output path for a best-frame PNG |
| `load_best_frame_lookup(project_path, filename, media_type)` | Returns a `{shot_id: best_frame_dict}` mapping loaded from the annotation JSON |
| `compute_blur_score(image)` | Returns a normalized sharpness score (0–1) used to penalize blurry frame candidates |
| `compute_description_hash(description)` | Returns a short stable hash of a description string for change detection |

### `services/models.py`

| Function | Description |
|---|---|
| `download_model(project_path, repo_or_url, local_name, ...)` | Downloads a HuggingFace model snapshot into `models/` |
| `list_models(project_path)` | Prints a table of local models with parameter counts, dtype, disk size, and role assignments |
| `model_size_report(project_path, name)` | Estimates VRAM requirements and compares to available GPU memory |
| `remove_model(project_path, name, confirm)` | Deletes a model directory (dry run unless `--confirm`) |

### `visualizers/shot_visualizer.py`

| Function / Class | Description |
|---|---|
| `ipc_send_load(project_path, filename, media_type)` | Sends a "load film" message over Unix domain socket to a running `ShotlistVisualizer` |
| `AudioPlayer` | Streams audio in a background thread (PyAV + sounddevice) with gain and channel-map support |
| `ShotlistVisualizer` | Qt main window — frame-precise video player, shot/scene editor, annotation panel, IPC server |
