"""Frame-image embedding index builder.

For each media item this service reads the shot annotation JSON, loads the
already-saved best-frame PNGs, encodes them with CLIP, and writes three
sidecar files alongside the existing annotation artifacts:

    <stem>.frames.npy           — float32 image embeddings, shape (N, dim)
    <stem>.frames.valid.npy     — bool validity mask, shape (N,)
    <stem>.frames.manifest.json — provenance / counts manifest

Row order exactly matches the annotation JSON item order.  Missing PNGs
produce a zero vector with ``valid=False``; the row is counted in the
manifest so consumers can skip invalid rows.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from data.index import (
    MANIFEST_VERSION,
    get_frame_embeddings_path,
    get_frame_manifest_path,
    get_frame_valid_path,
    hash_file,
    load_annotation_items,
)
from services.frame_match import best_frame_path, embed_frame_images, load_frame_embedding_model


# ---------------------------------------------------------------------------
# Up-to-date check
# ---------------------------------------------------------------------------

def is_frame_index_current(
    project_path: str,
    filename: str,
    media_type: str,
    model_name: str,
    *, registry=None,
) -> bool:
    """Return True when all three frame-embedding files exist and match the model.

    Existence check only (no hash comparison) for speed.  Pass ``--force`` to
    force a rebuild regardless.
    """
    from data.index import annotation_artifacts_for_file
    collection = annotation_artifacts_for_file(project_path, filename, media_type, registry=registry,
                                               required=(".annotations.json", ".frames.npy", ".frames.valid.npy", ".frames.manifest.json"))
    npy_path = collection["expected"][".frames.npy"]
    valid_path = collection["expected"][".frames.valid.npy"]
    manifest_path = collection["expected"][".frames.manifest.json"]
    if registry is not None:
        from data.resource_locations import resolve_annotation
        resolve_annotation(project_path, filename, media_type, ".annotations.json", registry=registry)
        commitment = collection.get("commitment")
        if commitment is not None and not {".frames.npy", ".frames.valid.npy", ".frames.manifest.json"}.issubset(commitment["artifacts"]):
            return False
        for suffix in (".annotations.json", ".frames.npy", ".frames.valid.npy", ".frames.manifest.json"):
            resolve_annotation(project_path, filename, media_type, suffix, registry=registry)

    if not (npy_path.exists() and valid_path.exists() and manifest_path.exists()):
        return False

    # Check that the stored manifest matches the current model
    try:
        with manifest_path.open("r", encoding="utf-8") as fh:
            m = json.load(fh)
        stored_model = m.get("model", {}).get("name")
        return stored_model == model_name
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def build_frame_embeddings(
    project_path: str,
    filename: str,
    media_type: str,
    model_name: str = "clip-vit-base-patch32",
    *,
    force: bool = False,
    verbose: bool = False,
    batch_size: int = 32,
    limit: int | None = None,
    registry=None, approved=False, expected_revision=None, expected_hashes=None,
) -> dict[str, Any]:
    """Embed best-frame PNGs for all shots in *filename* and save index files.

    Args:
        project_path: Project root directory.
        filename:     Video filename (e.g. ``"3 10 To Yuma (1957) {tmdb-14168}.mp4"``).
        media_type:   ``"movie"`` or ``"gameplay"``.
        model_name:   CLIP model name/path (resolved by ``load_frame_embedding_model``).
        force:        Overwrite existing index files.
        verbose:      Print per-batch / per-shot progress.
        batch_size:   Images per CLIP forward pass.
        limit:        Process only the first *N* annotation items (smoke testing).

    Returns:
        Summary dict with keys: ``filename``, ``shape``, ``valid_count``,
        ``missing_count``, ``model``, ``npy_path``.

    Raises:
        FileNotFoundError: If the annotation JSON does not exist.
        RuntimeError:      If all frames are missing (writing an all-zero index
                           would be useless).
    """
    from data.index import annotation_artifacts_for_file

    if registry is not None:
        from data.resource_storage import ResourceStore
        ResourceStore(project_path, registry, approved=approved)
        if type(expected_revision) is not int or expected_hashes is None:
            raise ValueError("Registered frame generation requires explicit collection revision/hashes")
        from data.resource_locations import annotation_commitment
        commitment = annotation_commitment(project_path, filename, media_type, registry)
        if commitment is None or commitment["revision"] != expected_revision or commitment["artifacts"] != expected_hashes:
            raise ValueError("Frame generation revision/hash conflict")
    stem = Path(filename).stem
    collection = annotation_artifacts_for_file(project_path, filename, media_type, registry=registry)
    json_path = collection["expected"][".annotations.json"]
    npy_path = collection["expected"][".frames.npy"]
    valid_path = collection["expected"][".frames.valid.npy"]
    manifest_path = collection["expected"][".frames.manifest.json"]

    if not json_path.exists():
        raise FileNotFoundError(
            f"No annotation JSON found for '{filename}'.\n"
            f"Expected: {json_path}"
        )

    # Up-to-date skip
    if not force and is_frame_index_current(project_path, filename, media_type, model_name, registry=registry):
        if verbose:
            print(f"  — {stem}  (frame-embeddings up to date, use --force to rebuild)")
        return {
            "filename": filename,
            "status": "skip",
            "shape": None,
            "valid_count": None,
            "missing_count": None,
            "model": model_name,
            "npy_path": npy_path,
        }

    # Load items
    items = load_annotation_items(project_path, filename, media_type, registry=registry)
    if registry is not None:
        raise ValueError("Registered frame-sidecar regeneration requires controlled artifact publication; committed collections are read-only")
    if limit is not None:
        items = items[:limit]

    n_items = len(items)
    if n_items == 0:
        raise RuntimeError(f"Annotation JSON is empty for '{filename}'.")

    # Load model (deferred to avoid loading when not needed)
    if verbose:
        print(f"  Loading model: {model_name}")
    model, processor, device = load_frame_embedding_model(project_path, model_name)

    # Collect paths in annotation order
    png_paths: list[Path | None] = []
    for item in items:
        shot = item.get("shot", {})
        shot_id = shot.get("shot_id")
        if shot_id:
            png_paths.append(best_frame_path(project_path, media_type, filename, shot_id))
        else:
            png_paths.append(None)

    # Embed in batches, preserving row order
    embeddings_rows: list[np.ndarray] = []
    valid_flags: list[bool] = []
    missing_count = 0
    embed_dim: int | None = None

    # We process row-by-row but batch CLIP calls for efficiency.
    # Build batches of (index, PIL.Image) to embed, then scatter back.
    from PIL import Image as PILImage

    batch_indices: list[int] = []
    batch_images: list[Any] = []

    # Pre-fill placeholders (None = not embedded yet, will be set after batches)
    result_vecs: list[np.ndarray | None] = [None] * n_items
    result_valid: list[bool] = [False] * n_items

    def _flush_batch():
        """Embed the current batch and scatter results into result_vecs."""
        nonlocal embed_dim
        if not batch_images:
            return
        vecs = embed_frame_images(batch_images, model, processor, device)
        for local_i, global_i in enumerate(batch_indices):
            result_vecs[global_i] = vecs[local_i]
            result_valid[global_i] = True
            if embed_dim is None:
                embed_dim = vecs.shape[1]
        batch_indices.clear()
        batch_images.clear()

    for i, png_path in enumerate(png_paths):
        loaded_img = None
        if png_path is not None and png_path.exists():
            try:
                img = PILImage.open(str(png_path)).convert("RGB")
                img.load()
                loaded_img = img
            except Exception as exc:
                if verbose:
                    print(f"  ! could not load PNG for row {i}: {png_path.name}  ({exc})")

        if loaded_img is not None:
            batch_indices.append(i)
            batch_images.append(loaded_img)
            if len(batch_images) >= batch_size:
                if verbose:
                    print(f"  embedding batch [{i - batch_size + 1}–{i}] …")
                _flush_batch()
        else:
            missing_count += 1
            if verbose:
                shot_id = items[i].get("shot", {}).get("shot_id", f"row {i}")
                print(f"  ! missing best-frame PNG for shot {shot_id}")

    # Flush any remaining partial batch
    if batch_images:
        if verbose:
            print(f"  embedding final batch ({len(batch_images)} image(s)) …")
        _flush_batch()

    # Guard: all frames missing → fail rather than writing useless zeros
    valid_count = n_items - missing_count
    if valid_count == 0:
        raise RuntimeError(
            f"All {n_items} best-frame PNGs are missing for '{filename}'. "
            "Nothing to embed — aborting. "
            "Run 'crossing index frame-match' first to generate best-frame images."
        )

    # Determine embedding dimension from any valid vector
    if embed_dim is None:
        for v in result_vecs:
            if v is not None:
                embed_dim = len(v)
                break
    assert embed_dim is not None

    # Build final arrays, using zero vectors for missing rows
    zero_vec = np.zeros(embed_dim, dtype="float32")
    embeddings = np.stack(
        [v if v is not None else zero_vec for v in result_vecs],
        axis=0,
    ).astype("float32")
    valid_mask = np.array(result_valid, dtype=bool)

    if registry is not None:
        commit_frame_artifacts(project_path, filename, media_type, embeddings, valid_mask, model_name=model_name,
                               registry=registry, approved=approved, expected_revision=expected_revision,
                               expected_hashes=expected_hashes)
        return {"filename": filename, "status": "ok", "shape": list(embeddings.shape),
                "valid_count": int(valid_count), "missing_count": int(missing_count),
                "model": model_name, "npy_path": npy_path}
    # Write files
    npy_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(npy_path, embeddings)
    np.save(valid_path, valid_mask)

    # Build manifest
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = _build_frame_manifest(
        project_path=project_path,
        filename=filename,
        media_type=media_type,
        model_name=model_name,
        json_path=json_path,
        npy_path=npy_path,
        valid_path=valid_path,
        embeddings=embeddings,
        valid_mask=valid_mask,
        item_count=n_items,
        valid_count=valid_count,
        missing_count=missing_count,
        now=now,
    )
    from data.annotate import atomic_write_text
    atomic_write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")

    return {
        "filename": filename,
        "status": "ok",
        "shape": list(embeddings.shape),
        "valid_count": int(valid_count),
        "missing_count": int(missing_count),
        "model": model_name,
        "npy_path": npy_path,
    }


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------

def commit_frame_artifacts(project_path, filename, media_type, embeddings, valid_mask, *, registry,
                           expected_revision, expected_hashes, approved=False, model_name="precomputed",
                           operation_id=None, interrupt=None):
    """Commit precomputed frame rows and their mask; never run a model or infer paths."""
    import hashlib
    import io
    from data.resource_locations import annotation_commitment, annotation_path, resolve_annotation
    from data.resource_storage import ResourceStore
    from services.resource_publication import publish_annotation_collection
    if operation_id is not None:
        with ResourceStore(project_path, registry, approved=approved).transaction() as database:
            operation = database.execute("SELECT * FROM publication_operations WHERE operation_id=? AND kind='annotation-collection'",
                                         (operation_id,)).fetchone()
        if operation is not None:
            details = json.loads(operation["details"])
            if details["filename"] != filename or details["media_type"] != media_type:
                raise ValueError("Frame retry belongs to a different collection")
            changes = {}
            for suffix, array in ((".frames.npy", embeddings), (".frames.valid.npy", valid_mask)):
                buffer = io.BytesIO()
                np.save(buffer, array, allow_pickle=False)
                changes[suffix] = buffer.getvalue()
            from data.resource_locations import project_file
            changes[".frames.manifest.json"] = project_file(project_path, Path(project_path) / details["stage"] / "new.frames.manifest.json").read_bytes()
            if json.loads(changes[".frames.manifest.json"])["model"]["name"] != model_name:
                raise ValueError("Frame retry model association mismatch")
            return publish_annotation_collection(project_path, registry, filename, media_type, changes,
                                                 expected_revision=expected_revision, expected_hashes=expected_hashes,
                                                 approved=approved, operation_id=operation_id, interrupt=interrupt)
    commitment = annotation_commitment(project_path, filename, media_type, registry)
    if commitment is None:
        raise ValueError("Committed annotation collection required")
    json_path = resolve_annotation(project_path, filename, media_type, registry=registry)
    paths = {suffix: annotation_path(project_path, filename, media_type, suffix, layout=commitment["layout"])
             for suffix in (".frames.npy", ".frames.valid.npy", ".frames.manifest.json")}
    changes = {}
    for suffix, array in ((".frames.npy", embeddings), (".frames.valid.npy", valid_mask)):
        buffer = io.BytesIO()
        np.save(buffer, array, allow_pickle=False)
        changes[suffix] = buffer.getvalue()
    hashes = {str(paths[suffix]): "sha256:" + hashlib.sha256(payload).hexdigest() for suffix, payload in changes.items()}
    manifest = _build_frame_manifest(project_path, filename, media_type, model_name, json_path,
                                     paths[".frames.npy"], paths[".frames.valid.npy"], embeddings, valid_mask,
                                     len(json.loads(json_path.read_text())), int(valid_mask.sum()),
                                     int((~valid_mask).sum()), datetime.now(timezone.utc).isoformat(), prepared_hashes=hashes)
    changes[".frames.manifest.json"] = (json.dumps(manifest, indent=2, allow_nan=False) + "\n").encode()
    return publish_annotation_collection(project_path, registry, filename, media_type, changes,
                                         expected_revision=expected_revision, expected_hashes=expected_hashes,
                                         approved=approved, operation_id=operation_id, interrupt=interrupt)


def _build_frame_manifest(
    project_path: str,
    filename: str,
    media_type: str,
    model_name: str,
    json_path: Path,
    npy_path: Path,
    valid_path: Path,
    embeddings: np.ndarray,
    valid_mask: np.ndarray,
    item_count: int,
    valid_count: int,
    missing_count: int,
    now: str,
    prepared_hashes=None,
) -> dict:
    project = Path(project_path)
    stem = Path(filename).stem
    manifest_path = get_frame_manifest_path(project_path, filename, media_type)

    return {
        "version": MANIFEST_VERSION,
        "updated_at": now,
        "index_type": "frame-embeddings",
        "embedding_source": "best-frame images",
        "embedding_modality": "image",
        "media_type": media_type,
        "filename": stem,
        "json": {
            "filename": json_path.name,
            "path": str(json_path.relative_to(project)),
            "hash": hash_file(json_path),
            "item_count": item_count,
        },
        "frames": {
            "source": "best-frame PNGs",
            "valid_count": valid_count,
            "missing_count": missing_count,
        },
        "npy": {
            "filename": npy_path.name,
            "path": str(npy_path.relative_to(project)),
            "hash": prepared_hashes[str(npy_path)] if prepared_hashes is not None else hash_file(npy_path),
            "shape": list(embeddings.shape),
            "dtype": str(embeddings.dtype),
        },
        "valid": {
            "filename": valid_path.name,
            "path": str(valid_path.relative_to(project)),
            "hash": prepared_hashes[str(valid_path)] if prepared_hashes is not None else hash_file(valid_path),
            "shape": [int(valid_mask.shape[0])],
            "dtype": str(valid_mask.dtype),
        },
        "model": {
            "role": "frame_match",
            "name": model_name,
        },
    }
