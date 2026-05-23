"""Model management utilities for crossing-tool.

Three public operations:
- list_models(project_path)          →  pretty-print all downloaded models
- download_model(project_path, ...)  →  snapshot_download a HF repo
- model_size_report(...)             →  estimate VRAM and compare to GPU

Model directories live under  <project>/models/<name>/
Single-file weights (.pt, .pth, .gguf, ...) live under  <project>/models/<file>
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Bytes per element for each safetensors dtype string
_DTYPE_BYTES: Dict[str, float] = {
    "F64": 8, "F32": 4, "F16": 2, "BF16": 2,
    "I64": 8, "I32": 4, "I16": 2, "I8": 1, "U8": 1,
    "F8_E4M3": 1, "F8_E5M2": 1, "F4": 0.5, "BOOL": 0.125,
}

# KV-cache + activation overhead fraction applied on top of weight VRAM
_INFERENCE_OVERHEAD = 0.20

# Extensions we treat as single-file weight blobs (not HF transformer dirs)
_WEIGHT_FILE_EXTS = {".pt", ".pth", ".gguf", ".bin", ".onnx", ".engine"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.0f} MB"
    return f"{n / (1 << 10):.0f} KB"


def _fmt_params(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1e9:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.0f}M"
    return f"{n:,}"


def _models_dir(project_path: str) -> Path:
    return Path(project_path) / "models"


def _dir_size(p: Path) -> int:
    """Recursive disk usage of a directory in bytes."""
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _parse_hf_input(value: str) -> str:
    """Normalise a HF URL or repo-id to a plain 'owner/repo' string."""
    m = re.match(r"https?://huggingface\.co/([^/\s]+/[^/?\s]+)", value)
    if m:
        return m.group(1)
    return value


def _local_name_from_repo(repo_id: str) -> str:
    """Default local directory name derived from a repo-id (model part only)."""
    return repo_id.split("/")[-1]


# ---------------------------------------------------------------------------
# Safetensors parameter counting (no heavy dependencies)
# ---------------------------------------------------------------------------

def _read_safetensors_header(path: Path) -> dict:
    """Return the parsed JSON header of a .safetensors file."""
    with path.open("rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        return json.loads(f.read(n).decode("utf-8"))


def _params_from_safetensors_dir(model_dir: Path) -> Tuple[Dict[str, int], int]:
    """Return (dtype→param_count dict, total_bytes) from all .safetensors shards."""
    dtype_counts: Dict[str, int] = {}
    total_bytes = 0
    shards = sorted(model_dir.glob("*.safetensors"))
    for shard in shards:
        header = _read_safetensors_header(shard)
        for key, meta in header.items():
            if key == "__metadata__":
                continue
            offsets = meta.get("data_offsets", [0, 0])
            size_bytes = offsets[1] - offsets[0]
            total_bytes += size_bytes
            dtype = meta.get("dtype", "F32")
            bpe = _DTYPE_BYTES.get(dtype, 4)
            if bpe > 0:
                dtype_counts[dtype] = dtype_counts.get(dtype, 0) + int(size_bytes / bpe)
    return dtype_counts, total_bytes


def _dominant_dtype(dtype_counts: Dict[str, int]) -> str:
    if not dtype_counts:
        return "unknown"
    return max(dtype_counts, key=dtype_counts.__getitem__)


def _vram_from_bytes(total_bytes: int, overhead: float = _INFERENCE_OVERHEAD) -> int:
    return int(total_bytes * (1 + overhead))


# ---------------------------------------------------------------------------
# GPU info
# ---------------------------------------------------------------------------

def _gpu_info() -> Optional[dict]:
    """Return GPU stats dict or None if CUDA is unavailable."""
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        props = torch.cuda.get_device_properties(0)
        free, total = torch.cuda.mem_get_info(0)
        return {
            "name": props.name,
            "total": total,
            "free": free,
            "used": total - free,
            "device_count": torch.cuda.device_count(),
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Model entry data
# ---------------------------------------------------------------------------

def _model_entry(path: Path) -> dict:
    """Build a metadata dict for a single model (dir or file)."""
    entry: dict = {"path": path, "name": path.name}
    if path.is_file():
        entry["kind"] = "file"
        entry["disk_bytes"] = path.stat().st_size
        entry["dtype_counts"] = {}
        entry["total_param_bytes"] = 0
    else:
        entry["kind"] = "dir"
        entry["disk_bytes"] = _dir_size(path)
        has_safetensors = any(path.glob("*.safetensors"))
        if has_safetensors:
            try:
                dtype_counts, total_bytes = _params_from_safetensors_dir(path)
                entry["dtype_counts"] = dtype_counts
                entry["total_param_bytes"] = total_bytes
            except Exception:
                entry["dtype_counts"] = {}
                entry["total_param_bytes"] = 0
        else:
            entry["dtype_counts"] = {}
            entry["total_param_bytes"] = 0
    return entry


def list_models(project_path: str) -> None:
    """Print a summary table of all models in <project>/models/."""
    from tool.prefs import get as pget  # imported lazily to avoid circular

    mdir = _models_dir(project_path)
    if not mdir.exists():
        print("  (no models directory found)")
        return

    # Load configured roles to annotate which model is assigned to each
    configured: Dict[str, str] = {}
    for role, key_or_pair in (
        ("annotate",     ("model_annotate",     None)),
        ("segmentation", ("model_segmentation", None)),
        ("embed",        ("model_embed",        "BAAI/bge-small-en-v1.5")),
        ("frame_match",  ("model_frame_match",  "clip-vit-base-patch32")),
    ):
        key, default = key_or_pair
        val = pget(key, default)
        if val:
            configured[val] = role
            # Also index by basename so "BAAI/bge-small-en-v1.5" matches folder "bge-small-en-v1.5"
            basename = val.split("/")[-1]
            if basename != val:
                configured.setdefault(basename, role)

    items = sorted(mdir.iterdir(), key=lambda p: p.name.lower())
    if not items:
        print("  (no models found)")
        return

    dirs = [p for p in items if p.is_dir()]
    files = [p for p in items if p.is_file() and p.suffix.lower() in _WEIGHT_FILE_EXTS]
    ungrouped = [p for p in items if p not in dirs and p not in files]

    gpu = _gpu_info()

    # Header
    print(f"Models  ·  {mdir}")
    if gpu:
        pct = (gpu["used"] / gpu["total"] * 100) if gpu["total"] else 0
        print(f"GPU     ·  {gpu['name']}  —  {_fmt_bytes(gpu['free'])} free / {_fmt_bytes(gpu['total'])} total  ({pct:.0f}% used)")
    print()

    if dirs:
        print("  Transformer / HF models:")
        print(f"  {'Name':<36}  {'Params':>8}  {'Dtype':<8}  {'Disk':>8}  Role")
        print("  " + "─" * 72)
        for p in dirs:
            entry = _model_entry(p)
            total_params = sum(entry["dtype_counts"].values())
            dom_dtype = _dominant_dtype(entry["dtype_counts"])
            params_str = _fmt_params(total_params) if total_params else "—"
            dtype_str = dom_dtype if entry["dtype_counts"] else "—"
            disk_str = _fmt_bytes(entry["disk_bytes"])
            role = configured.get(p.name, "")
            print(f"  {p.name:<36}  {params_str:>8}  {dtype_str:<8}  {disk_str:>8}  {role}")
        print()

    if files:
        print("  Weight files (.pt / .gguf / ...):")
        print(f"  {'Name':<44}  {'Disk':>8}  Role")
        print("  " + "─" * 60)
        for p in files:
            disk_str = _fmt_bytes(p.stat().st_size)
            role = configured.get(p.name, "")
            print(f"  {p.name:<44}  {disk_str:>8}  {role}")
        print()

    if ungrouped:
        print("  Other:")
        for p in ungrouped:
            print(f"  {p.name}")
        print()


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_model(
    project_path: str,
    repo_or_url: str,
    local_name: Optional[str] = None,
    ignore_non_safetensors: bool = True,
) -> Path:
    """Download a HF model snapshot into <project>/models/<local_name>/.

    Parameters
    ----------
    project_path:
        Root of the crossing-tool project.
    repo_or_url:
        Hugging Face repo-id (``owner/model``) or a huggingface.co URL.
    local_name:
        Override for the local directory name.  Defaults to the model part
        of the repo-id (the text after the last ``/``).
    ignore_non_safetensors:
        When True, skip legacy weight formats (``*.bin``, ``*.h5``,
        ``flax_*``, ``tf_*``, ``*.msgpack``) to save disk space.

    Returns
    -------
    Path to the downloaded model directory.
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise ImportError("huggingface_hub is required for model downloads") from exc

    repo_id = _parse_hf_input(repo_or_url)
    if local_name is None:
        local_name = _local_name_from_repo(repo_id)

    target = _models_dir(project_path) / local_name
    target.mkdir(parents=True, exist_ok=True)

    ignore_patterns: List[str] = []
    if ignore_non_safetensors:
        ignore_patterns = ["*.bin", "*.h5", "flax_*", "tf_*", "*.msgpack", "*.pt"]

    print(f"  Downloading  {repo_id}")
    print(f"  Destination  {target}")
    if ignore_non_safetensors:
        print("  Skipping legacy weight formats (bin / h5 / msgpack / tf / flax)")
    print()

    snapshot_download(
        repo_id=repo_id,
        local_dir=str(target),
        ignore_patterns=ignore_patterns or None,
    )

    print()
    print(f"✓ Downloaded to  {target}")
    disk = _dir_size(target)
    print(f"  Disk size:   {_fmt_bytes(disk)}")

    # Try to show param count
    if any(target.glob("*.safetensors")):
        try:
            dtype_counts, total_bytes = _params_from_safetensors_dir(target)
            total_params = sum(dtype_counts.values())
            dom = _dominant_dtype(dtype_counts)
            print(f"  Parameters:  {_fmt_params(total_params)}  ({dom})")
        except Exception:
            pass

    return target


# ---------------------------------------------------------------------------
# Size report
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------

def remove_model(project_path: str, name: str, confirm: bool = False) -> None:
    """Delete a model from <project>/models/<name>.

    Without *confirm* this is a dry run — it prints what would be removed and
    exits without touching anything, matching the pattern used by
    ``crossing remove`` and ``crossing meta prune``.
    """
    import shutil

    target = _models_dir(project_path) / name
    if not target.exists():
        raise FileNotFoundError(f"No model found at {target}")

    if target.is_file():
        size = target.stat().st_size
        print(f"  {name}  ({_fmt_bytes(size)})")
    else:
        size = _dir_size(target)
        n_files = sum(1 for _ in target.rglob("*") if Path(_).is_file())
        print(f"  {name}/  ({_fmt_bytes(size)}, {n_files} files)")

    if not confirm:
        print(f"\nDry run. Pass --confirm to delete.")
        return

    if target.is_file():
        target.unlink()
    else:
        shutil.rmtree(target)
    print(f"\n✓ Removed {name}  ({_fmt_bytes(size)} freed).")


def _vram_estimate_local(model_dir: Path) -> Optional[Tuple[int, Dict[str, int]]]:
    """Return (vram_bytes_with_overhead, dtype_counts) from local safetensors."""
    if not any(model_dir.glob("*.safetensors")):
        return None
    dtype_counts, total_bytes = _params_from_safetensors_dir(model_dir)
    return _vram_from_bytes(total_bytes), dtype_counts


def _vram_estimate_remote(repo_id: str) -> Optional[Tuple[int, Dict[str, int]]]:
    """Return (vram_bytes_with_overhead, dtype_counts) using the HF API."""
    try:
        from huggingface_hub import model_info
        info = model_info(repo_id)
        st = getattr(info, "safetensors", None)
        if st is None:
            return None
        # st.parameters is {dtype_str: param_count}
        params: dict = st.parameters or {}
        # Convert param counts to byte counts and back
        total_bytes = 0
        dtype_counts: Dict[str, int] = {}
        for dtype, count in params.items():
            bpe = _DTYPE_BYTES.get(dtype.upper(), 4)
            total_bytes += int(count * bpe)
            dtype_counts[dtype.upper()] = int(count)
        if not total_bytes:
            return None
        return _vram_from_bytes(total_bytes), dtype_counts
    except Exception:
        return None


def model_size_report(project_path: str, name: str) -> None:
    """Print a VRAM estimate for *name* and compare it to the available GPU.

    *name* can be:
    - A local model folder name under ``<project>/models/``
    - A Hugging Face repo-id  (``owner/model``)
    - A huggingface.co URL
    """
    # GPU stats first
    gpu = _gpu_info()
    if gpu:
        used_pct = gpu["used"] / gpu["total"] * 100 if gpu["total"] else 0
        print(f"  GPU:  {gpu['name']}")
        print(f"  Total VRAM:  {_fmt_bytes(gpu['total'])}")
        print(f"  Free  VRAM:  {_fmt_bytes(gpu['free'])}  ({100 - used_pct:.0f}% free)")
        print(f"  Used  VRAM:  {_fmt_bytes(gpu['used'])}")
    else:
        print("  GPU:  (no CUDA GPU detected)")
    print()

    # Resolve local path first
    repo_id = _parse_hf_input(name)
    local_path = _models_dir(project_path) / name

    vram_with_overhead: Optional[int] = None
    dtype_counts: Dict[str, int] = {}
    disk_bytes = 0
    source_label = ""

    if local_path.is_dir():
        result = _vram_estimate_local(local_path)
        disk_bytes = _dir_size(local_path)
        source_label = f"local  ({local_path})"
        if result:
            vram_with_overhead, dtype_counts = result
    elif "/" in repo_id:
        # Looks like a HF repo-id — try remote
        print(f"  Fetching metadata from Hugging Face for  {repo_id} ...")
        result = _vram_estimate_remote(repo_id)
        source_label = f"remote HF repo  {repo_id}"
        if result:
            vram_with_overhead, dtype_counts = result
    else:
        print(f"  ✗ Local model '{name}' not found and does not look like a HF repo-id.")
        return

    print(f"  Model:  {name}  [{source_label}]")
    if disk_bytes:
        print(f"  Disk size:   {_fmt_bytes(disk_bytes)}")

    if vram_with_overhead is None:
        print("  ✗ Could not estimate VRAM (no safetensors metadata found).")
        return

    total_params = sum(dtype_counts.values())
    dom = _dominant_dtype(dtype_counts)
    # Weight-only bytes = vram / (1 + overhead)
    weight_bytes = int(vram_with_overhead / (1 + _INFERENCE_OVERHEAD))

    print(f"  Parameters:  {_fmt_params(total_params)}  ({dom})")
    print(f"  Est. VRAM (weights only):    {_fmt_bytes(weight_bytes)}")
    print(f"  Est. VRAM (with {_INFERENCE_OVERHEAD*100:.0f}% overhead): {_fmt_bytes(vram_with_overhead)}")

    if gpu:
        free = gpu["free"]
        total = gpu["total"]
        fits_free = vram_with_overhead <= free
        fits_total = vram_with_overhead <= total
        print()
        if fits_free:
            margin = free - vram_with_overhead
            print(f"  ✓ Fits in free VRAM  ({_fmt_bytes(free)} free, need ~{_fmt_bytes(vram_with_overhead)})")
            print(f"    Headroom after load: ~{_fmt_bytes(margin)}")
        elif fits_total:
            need = vram_with_overhead - free
            print(f"  ⚠ Fits in total VRAM but GPU is partially occupied")
            print(f"    Need {_fmt_bytes(need)} more free VRAM  (free: {_fmt_bytes(free)}, need: {_fmt_bytes(vram_with_overhead)})")
        else:
            over = vram_with_overhead - total
            print(f"  ✗ Does NOT fit in GPU  (total: {_fmt_bytes(total)}, need: {_fmt_bytes(vram_with_overhead)}, over by {_fmt_bytes(over)})")
