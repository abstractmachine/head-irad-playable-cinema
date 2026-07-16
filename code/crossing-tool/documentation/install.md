# Install

## 1. System dependencies

```bash
# Linux
sudo apt install curl ffmpeg

# macOS
brew install ffmpeg
```

## 2. Install uv

`uv` is a fast Python package manager that handles everything — including downloading Python itself. Install it once, system-wide:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then restart your shell (or run `source ~/.local/bin/env`) so the `uv` command is available.

## 3. Install crossing

```bash
uv tool install "crossing[all] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"
```

The `crossing` command is now available globally. No virtual environment activation needed.

> **Note:** `crossing` lives inside a larger monorepo. The `#subdirectory=` part tells `uv` where to find it.

## Optional components

`[all]` in the command above installs everything. You can instead pick only what you need:

| Extra | What it adds | Required for |
|---|---|---|
| `annotate` | transformers, huggingface-hub | `crossing annotate` (LLM vision models) |
| `visualizer` | PyQt5, opencv | `--visualizer` flag on any command |
| `shot-detection` | TransNetV2, TensorFlow | `crossing shotlist shot detect` |
| `silhouette` | sam2, opencv | `crossing index silhouette` |

```bash
# Install with a specific extra
uv tool install "crossing[annotate] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"
uv tool install "crossing[visualizer] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"
uv tool install "crossing[shot-detection] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"
uv tool install "crossing[silhouette] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"
```

To change extras on an already-installed tool, add `--reinstall`:
```bash
uv tool install --reinstall "crossing[all] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool"
```

## 4. Configure API keys

```bash
crossing tool api_key set tmdb <key>              # required for metadata fetch
crossing tool api_key set opensubtitles <key>     # required for subtitle fetch
crossing tool api_key set discord <webhook-url>   # optional — batch notifications
```

## 5. Verify

```bash
crossing tool version
```

---

# Developer Setup

Clone the repo and enter the project:

```bash
git clone https://github.com/abstractmachine/head-irad-playable-cinema.git
cd head-irad-playable-cinema/code/crossing-tool
```

## Global `crossing` command (recommended)

Install as a uv-managed tool with all extras. This gives you a `crossing` command that works from any directory, survives `uv sync`, and reflects source edits immediately:

```bash
uv tool install --editable ".[all]"
```

To update after adding dependencies to `pyproject.toml`:

```bash
cd head-irad-playable-cinema/code/crossing-tool
uv tool install --force --editable ".[all]"
```

## Dev environment only (no global command)

If you only need to run crossing inside the project venv:

```bash
uv sync              # must be run from crossing-tool directory
uv run crossing --help
```

`uv sync` only installs base dependencies. To also get optional extras in the dev venv:

```bash
# Run from the crossing-tool directory
uv sync --extra annotate --extra silhouette --extra visualizer
```

| Extra | What it adds | Required for |
|---|---|---|
| `annotate` | transformers, huggingface-hub | CLIP / frame matching |
| `visualizer` | PyQt5, opencv | `--visualizer` flag |
| `shot-detection` | TransNetV2, TensorFlow | `crossing shotlist shot detect` |
| `silhouette` | sam2, opencv | `crossing index silhouette` |
| `crossing index silhouette` | `uv sync --extra silhouette` |

```bash
# Install all extras at once
uv sync --extra all
```

After running `uv sync --extra <name>`, no restart is needed — just re-run your command.
