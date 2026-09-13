# MCP Server (Claude Desktop integration)

`mcp_server/mcp_server.py` exposes crossing data to LLMs via the [Model Context Protocol](https://modelcontextprotocol.io). This lets you query your film library directly from Claude Desktop (or any MCP-compatible client) without leaving the chat.

---

## Tool reference

**Access policy**
- All tools may read freely from `data/`, `media/`, and `preferences/`.
- Generation tools write only to `outputs/` subdirectories.
- No tool may write to source data (`data/annotations/`, `data/shotlists/`, `data/metadata/`, or `preferences/`).

**Architecture: one canonical implementation**

Crossing services define project semantics. The CLI, local Python scripts, visualizers,
and MCP are interfaces over those shared services; MCP augments the CLI/service ecosystem
and does not replace it. Domain operations such as search, source and shot resolution,
catalog/lifecycle semantics, indexing, extraction, statistics, ranking, and clustering
live in canonical Crossing services and have CLI surfaces when useful to local workflows.

MCP owns only protocol-specific presentation: tool argument validation, structured MCP
errors, text/image content blocks, transport cursors, and compact agent-oriented response
shapes. An MCP-only convenience packet may compose existing services, but it must not
create a different definition of the catalog, candidate identity, lifecycle, or source.

Normal silhouette MCP access is a concrete example: it always queries the ready
Illustration index with `assignment_state=active`. Historical inactive/superseded JSON and
PNG assets can remain on disk for provenance, but they are not ordinary MCP candidates and
MCP never falls back to raw catalog traversal when the index is missing or stale.

**Normal silhouette MCP access**

Normal silhouette discovery, counts, summaries, ranking, clustering, and booklet selection use **only** the ready Illustration index with `assignment_state=active`. Catalog JSON and PNG files for inactive or superseded records remain on disk for lifecycle history and provenance, but are deliberately invisible to ordinary MCP catalog operations. A missing or stale Illustration index is reported as unavailable; MCP never falls back to scanning `data/silhouettes/catalog/`.

`get_silhouette_reference_packet` also requires an active indexed candidate before it reads that candidate's existing PNG. `get_best_frame`, by contrast, remains an explicit shot/frame retrieval tool: it can retrieve a source frame from a caller-supplied film and shot identity without discovering a silhouette candidate.

---

### Tier 1 — Read-only tools

| Tool | Purpose | Key inputs |
|---|---|---|
| `list_movies` | List all films with metadata and data-availability flags | `media_type` |
| `get_metadata` | Full metadata for one film | `film` (title/filename/tmdb id) |
| `get_poster` | Existing local poster as inline JPEG image content | `film`, `year` / `tmdb_id` (optional) |
| `get_shotlist` | Shot list with timecodes and captions | `film`, `scene` (optional) |
| `get_subtitles` | Subtitle cues, optional time window | `film`, `start_secs`, `end_secs` |
| `list_motifs` | Per-shot motif sequence + film semantic title | `film` |
| `list_palettes` | Per-shot fg/bg dominant colours (RGB/LAB) | `film` |
| `list_silhouettes` | Bounded active silhouette listing | `word`, `field`, `scope` |
| `list_silhouette_candidates` | Paginated active candidate listing | `word`, `field`, `sort_by`, `limit`, `cursor` |
| `summarize_silhouette_catalog` | Active per-term counts, area statistics, and largest candidates | `term`, `field`, `top_n` |
| `rank_silhouette_candidates` | Deterministic ranking over active candidates | `term`, `field`, `limit` |
| `cluster_silhouette_variants` | Groups active candidates by persisted semantic variant metadata | `term`, `field`, `limit` |
| `get_best_silhouette` | Largest active silhouette plus representative frame | `word`, `field`, `scope` |
| `get_silhouette_reference_packet` | Exact active silhouette, its exact source frame, and canonical same-shot context | `media_id`, `shot_id`, `frame_index`, `word`, `field` |
| `search_shots` | Full-text search across shot annotations | `query`, `films`, `field`, `limit` |
| `search_vocabulary` | Vocabulary index by field, sorted by frequency | `field`, `top`, `sort` |

**`list_movies`**
```json
{ "media_type": "movies" }
```
Returns: `{ "ok": true, "count": N, "movies": [...] }` — each entry includes `title`, `year`, `tmdb`, `director`, `runtime`, `filename`, `media_id`, `has_shotlist`, `has_annotations`, `has_motifs`, and `has_poster`. `has_poster` means a local JPEG thumbnail is present; it does not make a network request.

**`get_metadata`**
```json
{ "film": "Searchers", "media_type": "movies" }
```
Accepts a title substring, exact filename, or numeric TMDb ID. Returns the full metadata record plus `has_poster` and, when available, project-relative `poster_path` such as `media/thumbnails/movie/Searchers.jpg`.

**`get_poster`**
```json
{ "film": "Searchers", "year": 1956 }
```
Returns a metadata JSON item followed by the existing local JPEG poster as inline image content. Use `tmdb_id` instead of (or with) `year` to disambiguate titles. This tool is read-only: it does not contact TMDb or create a thumbnail when one is missing.

**`get_shotlist`**
```json
{ "film": "Searchers", "scene": "3" }
```
Returns all shots (or one scene's shots) with `start_time`, `end_time`, `start_frame`, `end_frame`, `shot_id`, captions.

**`get_subtitles`**
```json
{ "film": "Searchers", "start_secs": 120.0, "end_secs": 240.0 }
```
Returns parsed SRT cues as `{ "start_secs", "end_secs", "text" }`. Omit time bounds to get the full file.

**`list_motifs`**
```json
{ "film": "Searchers" }
```
Returns the motif word sequence (`motifs: ["crossing", "waiting", ...]`), per-shot detail, and the film-level semantic title if generated.

**`list_palettes`**
```json
{ "film": "Searchers" }
```
Returns per-shot `fg_rgb`, `bg_rgb`, luminance, and chroma. Requires palette cache (`crossing palette build`).

**`list_silhouettes`**
```json
{ "word": "horse", "field": "animals", "scope": "all" }
```
Lists up to 100 active Illustration-index candidates for a term. `scope` can be `"all"` or `"movie-<media_id>"`. Its `count` is the active population, never a raw catalog or physical SQLite row count.

**`list_silhouette_candidates`**
```json
{
  "word": "saddle",
  "field": "objects",
  "sort_by": "pixel_area",
  "descending": true,
  "min_pixel_area": 2000,
  "min_score": 0.25,
  "limit": 50,
  "cursor": "0"
}
```
Returns one deterministic page from the active Illustration population. `sort_by` accepts `catalog_order`, `alphabetical`, `score`/`confidence`, `pixel_area`, and persisted quality metrics such as `usefulness`, `fullness`, `size`, `completeness`, and `isolation`. `next_cursor` is either a deterministic offset for the same query or `null`. Default `limit` is 100 and the maximum is 250.

**`summarize_silhouette_catalog`**
```json
{ "term": "saddle", "field": "objects", "top_n": 10 }
```
Returns active candidate count, distinct active films and shots, min/mean/max and 25th/50th/75th percentile mask areas, plus the largest active candidate references.

**`rank_silhouette_candidates`**
```json
{ "term": "saddle", "field": "objects", "limit": 20 }
```
Ranks only active candidates using already-persisted quality metadata: usefulness, confidence, isolation, completeness, and occlusion. It does not load CLIP, SAM, embeddings, or another model.

**`cluster_silhouette_variants`**
```json
{ "term": "saddle", "field": "objects", "limit": 20 }
```
Groups only active candidates by their persisted `viewpoint`, `completeness`, `occlusion`, and `isolation` values. Each cluster has a stable ID, active film/shot/candidate counts, a deterministic representative, and bounded candidate references. It is analysis only and never changes the catalog.

**`get_best_silhouette`**

Selects the largest candidate from the active Illustration index only. It must not be used as an exact-candidate API; use `get_silhouette_reference_packet` with the returned active `candidate_id` when the specific existing object matters.

**`get_archive_stats` silhouette counts**

`silhouette_entries` means the active Illustration-index population. `silhouette_physical_index_records` is a separate diagnostic count of all SQLite rows, including inactive and superseded history. The two are expected to differ after rehabilitation. `silhouette_index_status` reports whether normal active MCP access is currently available; a stale or missing index never triggers a raw-catalog fallback.

**`get_silhouette_reference_packet`**

Returns a read-only visual-evidence packet for one **exact existing** silhouette catalog object. It is for downstream inspection of the selected object, not for choosing a better candidate and not for engraving generation.

```json
{
  "media_id": "tmdb_83831",
  "shot_id": "tmdb_83831@f047842-f047878",
  "frame_index": 47854,
  "word": "gun",
  "field": "objects",
  "media_type": "movie",
  "candidate_id": "data/silhouettes/catalog/movie/Charley One-Eye/gun/object_0001.json"
}
```

Required selectors are `media_id`, `shot_id`, `frame_index`, `word`, and `field`; `media_type` defaults to `"movie"`. The tool applies every supplied selector exactly. There is no substring matching, score sorting, area sorting, newest-record preference, or fallback candidate selection.

`candidate_id` is the project-relative canonical catalog JSON path returned in a previous packet's `candidate_ref.candidate_id`. It must identify a record that is currently active in the Illustration index. Crossing catalog `object_id` values such as `object_0001` are only unique within their existing canonical catalog reference `(media_type, filename_stem, label, object_id)`, so they are not used as globally unique selectors. Supplying `candidate_id` still validates all required composite selectors against that exact active object. Without it, the complete composite identity must resolve to exactly one active object. `png_path` is an optional project-relative catalog PNG discriminator; it must resolve beneath `data/silhouettes/catalog/<media_type>/`, otherwise the request is rejected. Neither parameter can read arbitrary filesystem paths or make a historical object current.

The result content blocks are always ordered as:

1. JSON metadata
2. exact cached silhouette PNG
3. exact photographic source frame from the catalog's `frame_index`
4. canonical same-shot context frames in chronological order

The silhouette PNG is returned byte-for-byte from the catalog, preserving its original dimensions, alpha channel, and pixel data. The exact source frame is independently extracted from the authorized original video at the catalog's recorded frame index; it is never replaced by a cached best frame, midpoint, representative frame, or first context frame. Photographic frames use native stored-video dimensions and JPEG encoding without resizing.

Context uses `data.annotate.canonical_context_frame_indices`, the same adaptive frame-count and temporal sampling policy used by Crossing's shot annotator. The tool exposes no new context count or spacing parameter. It returns the available in-shot canonical set, reports the actual indices, preserves chronological ordering, and does not emit a duplicate context image when the canonical sequence already includes the exact extraction frame.

Example metadata block (the following JSON block is the first returned content block):

```json
{
  "ok": true,
  "candidate_ref": {
    "candidate_id": "data/silhouettes/catalog/movie/Charley One-Eye/gun/object_0001.json",
    "object_id": "object_0001",
    "media_type": "movie",
    "media_id": "tmdb_83831",
    "shot_id": "tmdb_83831@f047842-f047878",
    "field": "objects",
    "word": "gun"
  },
  "candidate": {
    "candidate_id": "data/silhouettes/catalog/movie/Charley One-Eye/gun/object_0001.json",
    "object_id": "object_0001",
    "media_type": "movie",
    "media_id": "tmdb_83831",
    "shot_id": "tmdb_83831@f047842-f047878",
    "field": "objects",
    "word": "gun",
    "frame_index": 47854,
    "bbox": [122, 142, 589, 919],
    "score": 0.260776,
    "mask_area": 369771,
    "silhouette_path": "data/silhouettes/catalog/movie/Charley One-Eye/gun/object_0001.png",
    "scope": "movie-tmdb_83831",
    "film_title": "Charley One-Eye",
    "film_filename": "Charley One-Eye.mp4",
    "shot_start_frame": 47842,
    "shot_end_frame": 47878,
    "timestamp_seconds": 1595.133333,
    "fps": 30.0,
    "silhouette_width": 601,
    "silhouette_height": 931,
    "silhouette_mode": "RGBA",
    "silhouette_sha256": "..."
  },
  "source_frame": {
    "frame_index": 47854,
    "timestamp_seconds": 1595.133333,
    "width": 1920,
    "height": 1080,
    "mime_type": "image/jpeg",
    "sha256": "...",
    "is_exact_extraction_frame": true,
    "content_id": "exact_source_frame"
  },
  "context": {
    "selection_method": "canonical_shot_frame_selection",
    "frames": [
      {
        "frame_index": 47848,
        "timestamp_seconds": 1595.0,
        "relative_frame": -6,
        "is_source_frame": false,
        "width": 1920,
        "height": 1080,
        "mime_type": "image/jpeg",
        "sha256": "...",
        "content_id": "context_frame_01"
      },
      {
        "frame_index": 47854,
        "timestamp_seconds": 1595.133333,
        "relative_frame": 0,
        "is_source_frame": true,
        "width": 1920,
        "height": 1080,
        "mime_type": "image/jpeg",
        "sha256": "...",
        "content_id": "exact_source_frame"
      }
    ]
  },
  "shot": {
    "scene": "12",
    "start_time": "00:26:34.733",
    "end_time": "00:26:35.933",
    "caption": null,
    "scene_caption": null,
    "subtitles": []
  },
  "provenance": {
    "silhouette_is_existing_cache": true,
    "source_frame_is_exact": true,
    "context_selection_method": "canonical_shot_frame_selection",
    "new_inference_performed": false,
    "selection_changed": false
  },
  "content_order": [
    "metadata",
    "silhouette",
    "exact_source_frame",
    "context_frame_01"
  ],
  "warnings": [
    "Canonical context included the extraction frame; it is emitted only as exact_source_frame."
  ]
}
```

Failures return one JSON metadata block and no images:

```json
{
  "ok": false,
  "error": {
    "code": "CANDIDATE_AMBIGUOUS",
    "stage": "CANDIDATE_RESOLUTION",
    "message": "Multiple catalog objects match every supplied exact selector.",
    "details": {
      "candidates": []
    }
  }
}
```

Stages are `FILM_RESOLUTION`, `SHOT_RESOLUTION`, `CANDIDATE_RESOLUTION`, `SILHOUETTE_DECODING`, `EXACT_FRAME_EXTRACTION`, `CONTEXT_FRAME_EXTRACTION`, `MCP_IMAGE_ENCODING`, and `PAYLOAD_SIZE_VALIDATION`. The native-resolution packet has a conservative 900 KB total image-byte budget matching Crossing's existing MCP transport budget. If the exact evidence exceeds it, the tool returns `PAYLOAD_TOO_LARGE` with per-content byte counts rather than silently resizing, omitting, or substituting any image.

`get_silhouette_reference_packet` is deliberately different from `get_best_silhouette`: `get_best_silhouette` ranks a term's candidates by area, while this tool retrieves only the exact catalog object identified by the caller. It never loads CLIP, SAM/SAM3, embeddings, or any other model; `provenance.new_inference_performed` is always `false`.

**`search_shots`**
```json
{
  "query": "sunset",
  "films": ["The Searchers"],
  "field": "setting",
  "limit": 20
}
```
Omit `films` to search the full archive. `field` is optional. Returns scored results with timecodes, annotation text, and film metadata.

**`search_vocabulary`**
```json
{ "field": "objects", "top": 30, "sort": "count" }
```
Returns `[{ "value": "gun", "count": 412 }, ...]`. `sort` is `"count"` or `"alphabetical"`.

---

### Tier 2 — Generation tools (write to `outputs/` only)

| Tool | Output | Key inputs |
|---|---|---|
| `generate_flipbook` | `outputs/flipbooks/<stem>-flipbook.pdf` | `film`, `force` |
| `generate_mosaic` | `outputs/mosaics/search-mosaic-<timestamp>.png` | `query`, `films`, `limit`, `layout` |
| `generate_cloud` | `outputs/clouds/<scope>-<field>-cloud-<timestamp>.pdf` | `film`, `field`, `style` |
| `generate_composition` | `outputs/compositions/<query>+<date>.jpg` | `query`, `orientation`, `seed` |
| `generate_catalog` | `outputs/catalogs/catalog-<media_type>-<timestamp>.json` | `films`, `include_motifs` |

**`generate_flipbook`**
```json
{ "film": "Searchers", "force": false }
```
Requires motif data (`data/motifs/`) and palette data (`data/palettes/`). Generates a 16:9 per-shot color+word PDF.

**`generate_mosaic`**
```json
{ "query": "gun", "limit": 40, "layout": "landscape" }
```
Searches annotations, extracts one frame per matched shot, assembles into a grid PNG. Requires video files on disk.

**`generate_cloud`**
```json
{ "film": "Searchers", "field": "objects", "style": "western" }
```
Generates a word-frequency cloud PDF. Omit `film` for the full corpus. Available styles: `"default"`, `"western"`.

**`generate_composition`**
```json
{ "query": "dust", "orientation": "portrait", "seed": 42 }
```
Picks one matching shot at random (seeded for reproducibility), extracts and fits the frame to the canvas.

**`generate_catalog`**
```json
{ "include_motifs": true }
```
Writes a structured JSON index of all films to `outputs/catalogs/`. Pass `include_annotations: true` for full shot annotation data (large).

---

### Output folder convention

```
<project>/outputs/
  flipbooks/       ← generate_flipbook
  mosaics/         ← generate_mosaic
  clouds/          ← generate_cloud
  compositions/    ← generate_composition
  catalogs/        ← generate_catalog
  agent/           ← scratch or working derived artifacts
  review/          ← reviewable or provisional derived artifacts
```

MCP reads canonical project data but does not write back to it. All MCP-generated artifacts are derived outputs under `outputs/`. When scratch or working artifacts are needed, they belong in `outputs/agent/`; reviewable or provisional artifacts belong in `outputs/review/`. These folders are workspace conventions, not review queues or promotion workflows.

---

### What stays out of this MCP

The following are intentionally excluded and must remain CLI-only:

- **Shot annotation** (`crossing annotate`) — modifies `data/annotations/`. Too destructive for agent use without explicit human oversight.
- **Motif generation** (`crossing motif`) — requires LLM inference, long-running, writes to `data/motifs/`.
- **Palette building** (`crossing palette build`) — heavy CV pipeline, writes to `data/palettes/`.
- **Shotlist editing** (`crossing shot`) — writes to `data/shotlists/`.
- **Metadata editing** (`crossing metadata set`) — writes to `data/metadata/`.
- **Subtitle download** (`crossing subtitle fetch`) — network + file write.
- **Model management** (`crossing tool model`) — writes to preferences.
- **Vocabulary index rebuild** (`crossing vocabulary build`) — writes to `data/index/`.
- **Silhouette extraction** (`crossing silhouette`) — heavy CLIP+SAM pipeline.
- **Embedding index** (`crossing search index`) — heavy pipeline.

---

### Future experimental tools (not yet implemented)

- `generate_poster` — compose a typographic poster from palette + motif + silhouette
- `generate_book_spread` — lay out a two-page spread with annotations and frame
- `generate_sequence_reel` — stitch selected frames into a silent video
- `compare_films` — side-by-side motif / palette / vocabulary comparison
- `cluster_shots` — group shots by annotation similarity across the archive

---

## Server setup (Ubuntu)

**1. Install the MCP library into the project environment:**

```bash
uv add "mcp[cli]"
```

**2. Set the project path** (if not already configured):

```bash
crossing tool path /path/to/your/project
```

The server reads this saved preference automatically — no environment variable needed.

**3. Make the launcher script executable** (one-time, after cloning):

```bash
chmod +x run_mcp.sh
```

`run_mcp.sh` is a thin wrapper that locates itself at runtime using `$SCRIPT_DIR` and `$HOME` — no hardcoded paths, safe to commit to git.

**4. Test the server locally:**

```bash
uv run python mcp_server/mcp_server.py
```

The server speaks stdio (no port, no HTTP). It hangs silently waiting for input — that means it is working. Press `Ctrl+C` to exit.

## Client setup (Claude Desktop on macOS or Windows)

Edit `claude_desktop_config.json`:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add a `crossing` entry under `mcpServers`. The config file may already contain a `preferences` key — add `mcpServers` alongside it:

```json
{
  "preferences": { ... },
  "mcpServers": {
    "crossing": {
      "command": "ssh",
      "args": [
        "-T",
        "playable-cinema",
        "bash -lc 'cd /path/to/crossing-tool && uv run python mcp_server/mcp_server.py'"
      ]
    }
  }
}
```

Replace `playable-cinema` with your SSH host alias (or `user@hostname`), and update the path to `run_mcp.sh`. The `-T` flag disables pseudo-TTY allocation, which prevents SSH from sending terminal control codes that would corrupt the JSON-RPC stream.

> **Tip — SSH host alias:** Define `playable-cinema` in `~/.ssh/config` on the Mac so you do not have to repeat connection details:
> ```
> Host playable-cinema
>     HostName <ip-or-hostname>
>     User <your-username>
>     IdentityFile ~/.ssh/id_ed25519
>     AddKeysToAgent yes
>     UseKeychain yes
> ```
>
> The `UseKeychain yes` / `AddKeysToAgent yes` lines are important: Claude Desktop is a GUI app and does not inherit your terminal's SSH agent. Without these, the key is not available to Claude Desktop and every connection attempt will fail with `Permission denied`.
>
> Add your key to the macOS keychain once:
> ```bash
> ssh-add --apple-use-keychain ~/.ssh/id_ed25519
> ```

**Test the connection before opening Claude Desktop:**

```bash
ssh -T playable-cinema bash -lc "'cd /path/to/crossing-tool && uv run python mcp_server/mcp_server.py'"
```

It should hang silently. Press `Ctrl+C`, then restart Claude Desktop.

**Using the tools in Claude Desktop:**

After restarting, click the `+` button in the chat input area, choose **Connectors**, and select **crossing** to attach it to your conversation. Then ask naturally — e.g. "List my movies" — and Claude will call the tool automatically.
