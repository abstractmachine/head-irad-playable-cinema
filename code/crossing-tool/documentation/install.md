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

To work on the source code directly rather than installing as a tool:

```bash
git clone https://github.com/abstractmachine/head-irad-playable-cinema.git
cd head-irad-playable-cinema/code/crossing-tool
uv sync              # creates .venv, installs core deps
uv run crossing --help
```

Use `uv run crossing ...` instead of activating the venv. You can still activate manually if you prefer:

```bash
source .venv/bin/activate
crossing --help
```

## Installing optional extras (developer)

Optional features require their extra to be synced into the dev environment. If you run a command and see a missing-module error, install the relevant extra:

| Command that fails | Fix |
|---|---|
| `crossing annotate` | `uv sync --extra annotate` |
| `crossing ... --visualizer` | `uv sync --extra visualizer` |
| `crossing shotlist shot detect` | `uv sync --extra shot-detection` |
| `crossing index silhouette` | `uv sync --extra silhouette` |

```bash
# Install all extras at once
uv sync --extra all
```

After running `uv sync --extra <name>`, no restart is needed — just re-run your command.
