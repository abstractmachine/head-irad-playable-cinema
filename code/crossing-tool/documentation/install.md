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

If you want one shared runtime that works from any directory, sync the repo venv and point your PATH at the repo launcher:

```bash
cd head-irad-playable-cinema/code/crossing-tool
uv sync --extra all
ln -sf "$PWD/scripts/crossing" ~/.local/bin/crossing
```

This reuses the repo-managed `.venv` instead of creating a separate uv tool environment.

## Optional components

`uv sync --extra all` installs everything into the shared runtime. You can instead pick only what you need:

| Extra | What it adds | Required for |
|---|---|---|
| `annotate` | transformers, huggingface-hub | `crossing annotate` (LLM vision models) |
| `visualizer` | PyQt5, opencv | `--visualizer` flag on any command |
| `shot-detection` | TransNetV2, TensorFlow | `crossing shotlist shot detect` |
| `silhouette` | torch, transformers, safetensors, opencv | `crossing index silhouette`, `crossing index palette` |

```bash
# Install with a specific extra
uv sync --extra annotate
uv sync --extra visualizer
uv sync --extra shot-detection
uv sync --extra silhouette
```

To change extras on the shared runtime, rerun `uv sync` with the extras you want:
```bash
uv sync --extra all
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

Use the repo-managed venv and launcher script so `crossing` from anywhere reuses the same runtime:

```bash
uv sync --extra all
ln -sf "$PWD/scripts/crossing" ~/.local/bin/crossing
```

After adding dependencies to `pyproject.toml`, rerun `uv sync` in the repo instead of reinstalling a separate tool environment.

## Dev environment only (no global command)

If you only need to run crossing inside the project venv:

```bash
uv sync              # must be run from crossing-tool directory
uv run crossing --help
```

`uv sync` installs the base dependencies into the same project venv. To also get optional extras in that runtime:

```bash
# Run from the crossing-tool directory
uv sync --extra annotate --extra silhouette --extra visualizer
```

| Extra | What it adds | Required for |
|---|---|---|
| `annotate` | transformers, huggingface-hub | CLIP / frame matching |
| `visualizer` | PyQt5, opencv | `--visualizer` flag |
| `shot-detection` | TransNetV2, TensorFlow | `crossing shotlist shot detect` |
| `silhouette` | torch, transformers, safetensors, opencv | `crossing index silhouette`, `crossing index palette` |
| `crossing index silhouette` | `uv sync --extra silhouette` |

```bash
# Install all extras at once
uv sync --extra all
```

After running `uv sync --extra <name>`, no restart is needed — just re-run your command.
