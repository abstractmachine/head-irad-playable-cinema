# MCP Server (Claude Desktop integration)

`mcp_server/mcp_server.py` exposes crossing data to LLMs via the [Model Context Protocol](https://modelcontextprotocol.io). This lets you query your film library directly from Claude Desktop (or any MCP-compatible client) without leaving the chat.

---

## Tool reference

**Access policy**
- All tools may read freely from `data/`, `media/`, and `preferences/`.
- Generation tools write only to `outputs/` subdirectories.
- No tool may write to source data (`data/annotations/`, `data/shotlists/`, `data/metadata/`, or `preferences/`).

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
| `list_silhouettes` | Cached CLIP+SAM polygon masks by word | `word`, `field`, `scope` |
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
Lists cached silhouette JSON files for a term. `scope` can be `"all"` or `"movie-<media_id>"`.

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
