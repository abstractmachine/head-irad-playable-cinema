"""Frame-embedding index audit service.

Verifies that a frame-embedding index is complete, aligned, and internally
consistent.  All checks are read-only — no files are modified.

Public interface
----------------
audit_frame_embeddings(project_path, filename, media_type, *, verbose=False)
    Audit a single media item and return a result dict.

Checks performed
----------------
1.  frames.npy exists
2.  frames.valid.npy exists
3.  frames.manifest.json exists
4.  annotation item count == frame-embedding row count
5.  annotation item count == valid-mask row count
6.  frames.npy dtype is float32
7.  frames.valid.npy dtype is bool
8.  manifest["index_type"] == "frame-embeddings"
9.  manifest["embedding_modality"] == "image"
10. manifest npy shape matches actual frames.npy shape
11. manifest valid shape matches actual frames.valid.npy shape
12. manifest valid_count + missing_count == annotation item count
13. manifest valid_count == number of True values in frames.valid.npy
14. manifest missing_count == number of False values in frames.valid.npy
15. every row where valid=False has an all-zero embedding
16. every row where valid=True has a vector norm approximately 1.0
17. every row where valid=True is not an all-zero vector

The result dict always contains:

    status       "ok" | "missing" | "invalid"
    issues       list[str]      (empty when status == "ok")
    filename     str
    media_type   str
    item_count   int | None     (None when annotation JSON missing)
    valid_count  int | None
    missing_count int | None
    npy_shape    tuple | None

On "missing", the annotation JSON exists but one or more index files do not.
On "invalid", files exist but a consistency check has failed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

NORM_TOLERANCE: float = 0.05  # |norm - 1.0| must be < this for a valid row


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def audit_frame_embeddings(
    project_path: str,
    filename: str,
    media_type: str,
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    """Audit frame-embedding index for one media item.

    Returns a result dict — see module docstring for the full schema.
    Never modifies any file.
    """
    from data.annotate import get_annotation_json_path
    from data.index import (
        get_frame_embeddings_path,
        get_frame_valid_path,
        get_frame_manifest_path,
        load_annotation_items,
    )

    stem = Path(filename).stem
    json_path = get_annotation_json_path(project_path, filename, media_type)
    npy_path = get_frame_embeddings_path(project_path, filename, media_type)
    valid_path = get_frame_valid_path(project_path, filename, media_type)
    manifest_path = get_frame_manifest_path(project_path, filename, media_type)

    def _result(status: str, issues: list[str], item_count, valid_count, missing_count, npy_shape):
        return {
            "status": status,
            "issues": issues,
            "filename": filename,
            "stem": stem,
            "media_type": media_type,
            "item_count": item_count,
            "valid_count": valid_count,
            "missing_count": missing_count,
            "npy_shape": npy_shape,
        }

    # ------------------------------------------------------------------
    # Load annotation JSON (must exist)
    # ------------------------------------------------------------------
    if not json_path.exists():
        return _result("missing", ["annotation JSON missing"], None, None, None, None)

    try:
        items = load_annotation_items(project_path, filename, media_type)
    except Exception as exc:
        return _result("invalid", [f"failed to load annotation JSON: {exc}"], None, None, None, None)

    item_count = len(items)

    # ------------------------------------------------------------------
    # Check existence of all three index files
    # ------------------------------------------------------------------
    missing_files: list[str] = []
    if not npy_path.exists():
        missing_files.append("frames.npy missing")
    if not valid_path.exists():
        missing_files.append("frames.valid.npy missing")
    if not manifest_path.exists():
        missing_files.append("frames.manifest.json missing")

    if missing_files:
        return _result("missing", missing_files, item_count, None, None, None)

    # ------------------------------------------------------------------
    # Load arrays
    # ------------------------------------------------------------------
    issues: list[str] = []

    try:
        embeddings: np.ndarray = np.load(str(npy_path))
    except Exception as exc:
        return _result("invalid", [f"cannot load frames.npy: {exc}"], item_count, None, None, None)

    try:
        valid_mask: np.ndarray = np.load(str(valid_path))
    except Exception as exc:
        return _result("invalid", [f"cannot load frames.valid.npy: {exc}"], item_count, None, None, None)

    try:
        with manifest_path.open("r", encoding="utf-8") as fh:
            manifest: dict = json.load(fh)
    except Exception as exc:
        return _result("invalid", [f"cannot load frames.manifest.json: {exc}"], item_count, None, None, None)

    # ------------------------------------------------------------------
    # Check 4: item count == embedding row count
    # ------------------------------------------------------------------
    if embeddings.ndim != 2:
        issues.append(f"frames.npy must be 2-D, got ndim={embeddings.ndim}")
    elif embeddings.shape[0] != item_count:
        issues.append(
            f"row count mismatch: annotations has {item_count} items, "
            f"frames.npy has {embeddings.shape[0]} rows"
        )

    # ------------------------------------------------------------------
    # Check 5: item count == valid-mask row count
    # ------------------------------------------------------------------
    if valid_mask.ndim != 1:
        issues.append(f"frames.valid.npy must be 1-D, got ndim={valid_mask.ndim}")
    elif valid_mask.shape[0] != item_count:
        issues.append(
            f"valid-mask length mismatch: annotations has {item_count} items, "
            f"frames.valid.npy has {valid_mask.shape[0]} rows"
        )

    # ------------------------------------------------------------------
    # Check 6: dtype float32
    # ------------------------------------------------------------------
    if embeddings.dtype != np.float32:
        issues.append(f"frames.npy dtype must be float32, got {embeddings.dtype}")

    # ------------------------------------------------------------------
    # Check 7: dtype bool
    # ------------------------------------------------------------------
    if valid_mask.dtype != bool:
        issues.append(f"frames.valid.npy dtype must be bool, got {valid_mask.dtype}")

    # ------------------------------------------------------------------
    # Check 8 & 9: manifest index_type / embedding_modality
    # ------------------------------------------------------------------
    if manifest.get("index_type") != "frame-embeddings":
        issues.append(
            f"manifest index_type expected 'frame-embeddings', "
            f"got {manifest.get('index_type')!r}"
        )
    if manifest.get("embedding_modality") != "image":
        issues.append(
            f"manifest embedding_modality expected 'image', "
            f"got {manifest.get('embedding_modality')!r}"
        )

    # ------------------------------------------------------------------
    # Check 10: manifest npy shape matches actual
    # ------------------------------------------------------------------
    m_npy_shape = (manifest.get("npy") or {}).get("shape")
    actual_npy_shape = list(embeddings.shape)
    if m_npy_shape is not None and list(m_npy_shape) != actual_npy_shape:
        issues.append(
            f"manifest npy shape {m_npy_shape} does not match actual {actual_npy_shape}"
        )

    # ------------------------------------------------------------------
    # Check 11: manifest valid shape matches actual
    # ------------------------------------------------------------------
    m_valid_shape = (manifest.get("valid") or {}).get("shape")
    actual_valid_shape = list(valid_mask.shape)
    if m_valid_shape is not None and list(m_valid_shape) != actual_valid_shape:
        issues.append(
            f"manifest valid shape {m_valid_shape} does not match actual {actual_valid_shape}"
        )

    # ------------------------------------------------------------------
    # Checks 12–14: valid/missing counts
    # ------------------------------------------------------------------
    m_frames = manifest.get("frames") or {}
    m_valid_count = m_frames.get("valid_count")
    m_missing_count = m_frames.get("missing_count")

    actual_valid_count = int(np.sum(valid_mask)) if valid_mask.dtype == bool else None
    actual_missing_count = int(np.sum(~valid_mask)) if valid_mask.dtype == bool else None

    if actual_valid_count is not None and actual_missing_count is not None:
        # Check 12: valid_count + missing_count == item_count
        if m_valid_count is not None and m_missing_count is not None:
            if m_valid_count + m_missing_count != item_count:
                issues.append(
                    f"manifest valid_count ({m_valid_count}) + missing_count ({m_missing_count}) "
                    f"= {m_valid_count + m_missing_count}, expected {item_count}"
                )
            # Check 13: manifest valid_count matches actual true count
            if m_valid_count != actual_valid_count:
                issues.append(
                    f"manifest valid_count ({m_valid_count}) != "
                    f"True count in valid mask ({actual_valid_count})"
                )
            # Check 14: manifest missing_count matches actual false count
            if m_missing_count != actual_missing_count:
                issues.append(
                    f"manifest missing_count ({m_missing_count}) != "
                    f"False count in valid mask ({actual_missing_count})"
                )

    # ------------------------------------------------------------------
    # Checks 15–17: per-row vector validity
    # Only run when shape/dtype are correct so we avoid index errors.
    # ------------------------------------------------------------------
    shape_ok = (
        embeddings.ndim == 2
        and valid_mask.ndim == 1
        and embeddings.shape[0] == item_count
        and valid_mask.shape[0] == item_count
        and embeddings.dtype == np.float32
        and valid_mask.dtype == bool
    )

    if shape_ok and item_count > 0:
        invalid_indices = np.where(~valid_mask)[0]
        valid_indices = np.where(valid_mask)[0]

        # Check 15: invalid rows should be all zeros
        if len(invalid_indices) > 0:
            invalid_rows = embeddings[invalid_indices]
            non_zero_mask = ~np.all(invalid_rows == 0, axis=1)
            bad = int(np.sum(non_zero_mask))
            if bad:
                issues.append(
                    f"{bad} invalid row(s) (valid=False) have non-zero embeddings"
                )

        # Checks 16 & 17: valid rows should have norm ≈ 1.0 and not be zero
        if len(valid_indices) > 0:
            valid_rows = embeddings[valid_indices]
            norms = np.linalg.norm(valid_rows, axis=1)

            zero_rows = int(np.sum(norms == 0))
            if zero_rows:
                issues.append(
                    f"{zero_rows} valid row(s) (valid=True) are all-zero vectors"
                )

            bad_norm = int(np.sum(np.abs(norms - 1.0) >= NORM_TOLERANCE))
            if bad_norm:
                issues.append(
                    f"{bad_norm} valid row(s) have L2 norm far from 1.0 "
                    f"(tolerance ±{NORM_TOLERANCE})"
                )

    # ------------------------------------------------------------------
    # Compose result
    # ------------------------------------------------------------------
    npy_shape = tuple(embeddings.shape) if embeddings.ndim == 2 else None
    v_count = actual_valid_count if actual_valid_count is not None else m_valid_count
    m_count = actual_missing_count if actual_missing_count is not None else m_missing_count

    status = "ok" if not issues else "invalid"
    return _result(status, issues, item_count, v_count, m_count, npy_shape)
