"""Index services: serialization of annotation items for embedding pipelines.

This module provides utilities for converting annotation JSON items into
plain-text representations suitable for downstream indexing and embedding.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_mapping(project_path: str) -> dict:
    """Load the serialization mapping from <project>/preferences/data/mapping.yaml.

    Returns the contents of the top-level ``mapping`` key in the YAML file.

    Raises:
        FileNotFoundError: If the YAML file does not exist at the expected path.
        ValueError: If the YAML structure is missing the required ``mapping`` key.
    """
    mapping_path = Path(project_path) / "preferences" / "data" / "mapping.yaml"
    if not mapping_path.exists():
        raise FileNotFoundError(f"Mapping file not found: {mapping_path}")

    try:
        import yaml
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required for mapping support. Install with: pip install pyyaml"
        ) from exc

    with mapping_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "mapping" not in raw:
        raise ValueError(
            f"Invalid mapping YAML at {mapping_path}: "
            "expected a top-level 'mapping' key"
        )

    return raw["mapping"]


def load_fields(project_path: str) -> list[str]:
    """Load the display field list from <project>/preferences/data/fields.yaml.

    Returns the ordered list of field names under the top-level ``fields`` key.

    Raises:
        FileNotFoundError: If the YAML file does not exist.
        ValueError: If the YAML structure is missing the required ``fields`` key.
    """
    fields_path = Path(project_path) / "preferences" / "data" / "fields.yaml"
    if not fields_path.exists():
        raise FileNotFoundError(f"Fields file not found: {fields_path}")

    try:
        import yaml
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required for fields support. Install with: pip install pyyaml"
        ) from exc

    with fields_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "fields" not in raw:
        raise ValueError(
            f"Invalid fields YAML at {fields_path}: "
            "expected a top-level 'fields' key"
        )

    return list(raw["fields"])


def save_fields(project_path: str, fields: list[str]) -> None:
    """Write an ordered field list to <project>/preferences/data/fields.yaml."""
    try:
        import yaml
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required for fields support. Install with: pip install pyyaml"
        ) from exc

    fields_path = Path(project_path) / "preferences" / "data" / "fields.yaml"
    fields_path.parent.mkdir(parents=True, exist_ok=True)
    with fields_path.open("w", encoding="utf-8") as f:
        yaml.dump({"fields": fields}, f, default_flow_style=False, allow_unicode=True)


def serialize_annotation_item(item: dict, mapping: dict) -> str:
    """Serialize one annotation item to a single line of text.

    Each field value is stringified according to its type:
    - str  → used as-is (stripped)
    - list → joined with ", "
    - None / missing → skipped when skip_empty is True, otherwise ""

    Args:
        item:    One entry from the annotation JSON list.  Expected shape:
                 ``{ movie: {...}, annotation: {...},
                     shot: { shot_id: int, annotation: { field: value, ... } } }``
        mapping: Parsed mapping config dict with keys:
                 ``fields``, ``include_labels``, ``separator``, ``skip_empty``.

    Returns:
        A single-line string with the configured fields joined by the separator.
        Returns an empty string if all fields are empty/missing and skip_empty is True.
    """
    fields: list[str] = mapping.get("fields", [])
    include_labels: bool = mapping.get("include_labels", True)
    separator: str = mapping.get("separator", " | ")
    skip_empty: bool = mapping.get("skip_empty", True)

    shot_annotation: dict = item.get("shot", {}).get("annotation", {})

    parts: list[str] = []
    for field in fields:
        value: Any = shot_annotation.get(field)

        if value is None:
            if skip_empty:
                continue
            value_str = ""
        elif isinstance(value, list):
            if not value and skip_empty:
                continue
            value_str = ", ".join(str(v) for v in value)
        else:
            value_str = str(value).strip()
            if not value_str and skip_empty:
                continue

        if include_labels:
            parts.append(f"{field}: {value_str}")
        else:
            parts.append(value_str)

    return separator.join(parts)


def get_text_path(project_path: str, filename: str, media_type: str) -> Path:
    """Return the canonical path for the serialized `.txt` file.

    Sits alongside the annotation JSON:
    ``<project>/data/annotations/shots/<media_type>/<stem>.txt``
    """
    stem = Path(filename).stem
    return Path(project_path) / "data" / "annotations" / "shots" / media_type / f"{stem}.txt"


def write_text_file(
    project_path: str,
    filename: str,
    media_type: str,
    lines: list[str],
    *,
    force: bool = False,
) -> Path:
    """Write serialized text lines to ``<project>/data/index/text/<media_type>/<stem>.txt``.

    Args:
        project_path: Project root path.
        filename:     Source video filename (used to derive the stem).
        media_type:   ``"movies"`` or ``"gameplay"``.
        lines:        Pure serialized payload lines — no display indices.
        force:        Overwrite the file if it already exists.

    Returns:
        The Path where the file was written.

    Raises:
        FileExistsError: If the file already exists and ``force`` is False.
    """
    dest = get_text_path(project_path, filename, media_type)
    if dest.exists() and not force:
        raise FileExistsError(
            f"Output already exists: {dest}\n  Pass --force to overwrite."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def load_annotation_items(project_path: str, filename: str, media_type: str) -> list[dict]:
    """Load annotation JSON items for a given film.

    Args:
        project_path: Project root path.
        filename:     Video filename (e.g. ``"7th Cavalry (1956) {tmdb-5678}.mp4"``).
        media_type:   ``"movies"`` or ``"gameplay"``.

    Returns:
        List of annotation item dicts as stored in the shot annotation JSON.

    Raises:
        FileNotFoundError: If the annotation JSON does not exist.
    """
    from data.annotate import get_annotation_json_path

    json_path = get_annotation_json_path(project_path, filename, media_type)
    if not json_path.exists():
        raise FileNotFoundError(
            f"No annotation JSON found: {json_path}\n"
            f"  Run: crossing annotate shot {filename}"
        )

    with json_path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def get_embeddings_path(project_path: str, filename: str, media_type: str) -> Path:
    """Return the canonical path for the embeddings ``.npy`` file.

    Sits alongside the annotation JSON and serialized text:
    ``<project>/data/annotations/shots/<media_type>/<stem>.npy``

    Row order in the array matches:
    - annotation JSON item order
    - serialized text line order
    """
    stem = Path(filename).stem
    return Path(project_path) / "data" / "annotations" / "shots" / media_type / f"{stem}.npy"


def write_embeddings(
    project_path: str,
    filename: str,
    media_type: str,
    embeddings,
    *,
    force: bool = False,
) -> Path:
    """Save embeddings array to the canonical ``.npy`` path.

    Args:
        project_path: Project root path.
        filename:     Source video filename.
        media_type:   ``"movies"`` or ``"gameplay"``.
        embeddings:   ``np.ndarray`` of shape ``(N, dim)`` in float32.
        force:        Overwrite existing file when True.

    Returns:
        The Path where the file was written.

    Raises:
        FileExistsError: If the file already exists and *force* is False.
    """
    import numpy as np

    dest = get_embeddings_path(project_path, filename, media_type)
    if dest.exists() and not force:
        raise FileExistsError(
            f"Embeddings already exist: {dest}\n  Pass --force to overwrite."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    np.save(dest, embeddings)
    return dest


def load_embeddings(project_path: str, filename: str, media_type: str):
    """Load the embeddings array from ``<stem>.npy``, or return ``None`` if absent.

    Returns:
        ``np.ndarray`` of shape ``(N, dim)`` or ``None``.
    """
    import numpy as np

    path = get_embeddings_path(project_path, filename, media_type)
    if not path.exists():
        return None
    return np.load(path)


def embed_texts(
    texts: list,
    model_name: str,
    project_path: str,
    *,
    device: str | None = None,
    batch_size: int = 32,
) -> "np.ndarray":
    """Generate embeddings for a list of texts using mean-pooled transformer features.

    Model resolution order:
    1. ``<project>/models/<model_name>``  — project-local directory (preferred)
    2. ``model_name`` treated as an absolute/relative path on disk

    If neither resolves, raises ``RuntimeError`` with instructions to download
    the model via ``crossing tool model download``.

    Args:
        texts:        List of strings to embed.
        model_name:   HF repo-id or local model name/path.
        project_path: Project root (used for model directory resolution).
        device:       ``"cuda"`` or ``"cpu"``; auto-detected when ``None``.
        batch_size:   Tokenizer/model batch size.

    Returns:
        ``np.ndarray`` of shape ``(len(texts), embedding_dim)`` in float32.

    Raises:
        ImportError: If ``torch`` or ``transformers`` are unavailable.
        RuntimeError: If the model cannot be loaded.
    """
    import numpy as np
    try:
        import torch
        from transformers import AutoTokenizer, AutoModel
    except ImportError as exc:
        raise ImportError(
            "torch and transformers are required for embedding.\n"
            "Install with: pip install torch transformers"
        ) from exc

    if not texts:
        # Return empty array with unknown dim (0 rows).  Callers should guard.
        return np.zeros((0, 0), dtype="float32")

    # ---- Resolve model path (local only — no auto-download) ----
    # Try in the same order that `crossing tool model download` stores models:
    #   1. <project>/models/<model_name>           e.g. models/my-model
    #   2. <project>/models/<basename(model_name)> e.g. models/bge-small-en-v1.5
    #      (download strips the org prefix, so BAAI/bge-… lands as bge-…)
    #   3. model_name as an explicit absolute/relative path on disk
    project_local = Path(project_path) / "models" / model_name
    project_local_short = Path(project_path) / "models" / model_name.split("/")[-1]
    explicit_path = Path(model_name).expanduser()

    if project_local.exists():
        model_src = str(project_local)
        load_kwargs: dict = {"local_files_only": True}
    elif project_local_short.exists():
        model_src = str(project_local_short)
        load_kwargs = {"local_files_only": True}
    elif explicit_path.exists():
        model_src = str(explicit_path)
        load_kwargs = {"local_files_only": True}
    else:
        raise RuntimeError(
            f"Embedding model '{model_name}' not found.\n"
            f"  Expected at: {project_local_short}\n"
            f"  Download it first:\n"
            f"    crossing tool model download {model_name}\n"
            f"  Or set a different model:\n"
            f"    crossing tool model set embed <name>"
        )

    try:
        tokenizer = AutoTokenizer.from_pretrained(model_src, **load_kwargs)
        model = AutoModel.from_pretrained(model_src, **load_kwargs)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load embedding model from '{model_src}':\n  {exc}"
        ) from exc

    # ---- Device ----
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device)
    model.eval()

    # ---- Embed in batches ----
    all_embeddings: list = []
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            encoded = tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            encoded = {k: v.to(device) for k, v in encoded.items()}
            outputs = model(**encoded)
            # Mean pooling over non-padding tokens
            token_embeddings = outputs.last_hidden_state  # (B, seq, dim)
            attention_mask = encoded["attention_mask"].unsqueeze(-1).float()
            summed = (token_embeddings * attention_mask).sum(dim=1)
            counts = attention_mask.sum(dim=1).clamp(min=1e-9)
            pooled = (summed / counts).cpu().numpy().astype("float32")
            all_embeddings.append(pooled)

    return np.vstack(all_embeddings)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

MANIFEST_VERSION = 1


def get_manifest_path(project_path: str, filename: str, media_type: str) -> Path:
    """Return the canonical path for the sidecar manifest.

    ``<project>/data/annotations/shots/<media_type>/<stem>.manifest.json``
    """
    stem = Path(filename).stem
    return (
        Path(project_path)
        / "data" / "annotations" / "shots" / media_type
        / f"{stem}.manifest.json"
    )


def hash_file(path: "Path | str") -> str:
    """Return ``sha256:<hexdigest>`` of *path*'s raw byte content."""
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def load_manifest(project_path: str, filename: str, media_type: str) -> "dict | None":
    """Load and return the manifest dict, or ``None`` if absent."""
    path = get_manifest_path(project_path, filename, media_type)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_manifest(
    project_path: str, filename: str, media_type: str, manifest: dict
) -> Path:
    """Serialise *manifest* to the canonical manifest path and return it."""
    path = get_manifest_path(project_path, filename, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    return path


def build_manifest(
    project_path: str,
    filename: str,
    media_type: str,
    *,
    embed_model: str,
) -> dict:
    """Build a manifest dict from the current on-disk state of all derived files.

    All four artefacts must already exist: annotation JSON, mapping YAML,
    serialised ``.txt``, and embeddings ``.npy``.
    """
    import numpy as np
    from datetime import datetime, timezone
    from data.annotate import get_annotation_json_path

    json_path = get_annotation_json_path(project_path, filename, media_type)
    mapping_path = Path(project_path) / "preferences" / "data" / "mapping.yaml"
    txt_path = get_text_path(project_path, filename, media_type)
    npy_path = get_embeddings_path(project_path, filename, media_type)

    with json_path.open("r", encoding="utf-8") as f:
        items = json.load(f)

    txt_lines = [
        ln for ln in txt_path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    arr = np.load(npy_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "version": MANIFEST_VERSION,
        "updated_at": now,
        "media_type": media_type,
        "json": {
            "filename": json_path.name,
            "path": str(json_path.relative_to(project_path)),
            "hash": hash_file(json_path),
            "item_count": len(items),
        },
        "mapping": {
            "path": str(mapping_path.relative_to(project_path)),
            "hash": hash_file(mapping_path) if mapping_path.exists() else None,
        },
        "txt": {
            "filename": txt_path.name,
            "path": str(txt_path.relative_to(project_path)),
            "hash": hash_file(txt_path),
            "line_count": len(txt_lines),
        },
        "npy": {
            "filename": npy_path.name,
            "path": str(npy_path.relative_to(project_path)),
            "hash": hash_file(npy_path),
            "shape": list(arr.shape),
            "dtype": str(arr.dtype),
            "embed_model": embed_model,
        },
    }
