"""Frame extraction index: materialize best-frame JPGs from shot annotations.

For each annotated shot that has a ``best_frame.frame`` value, extract a single
representative JPEG from the source video and save it to:

    data/frames/<media_type>/<media_id>/<shot_id>.jpg

A manifest is written alongside each media item's frames:

    data/frames/<media_type>/<media_id>/manifest.json

This database is stable and idempotent: existing JPGs are reused unless
``force=True`` is passed.  The output is designed to feed downstream
CLIP-embedding indexing in a later task.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def frames_dir(project_path: str, media_type: str, media_id: str) -> Path:
    """Return the output directory for a given media item's frame JPGs."""
    return Path(project_path) / "data" / "frames" / media_type / media_id


def frame_jpg_path(project_path: str, media_type: str, media_id: str, shot_id: str) -> Path:
    """Return the canonical JPG path for a single shot's best frame."""
    safe = _safe_shot_id(shot_id)
    return frames_dir(project_path, media_type, media_id) / f"{safe}.jpg"


def manifest_path(project_path: str, media_type: str, media_id: str) -> Path:
    """Return the manifest JSON path for a given media item."""
    return frames_dir(project_path, media_type, media_id) / "manifest.json"


# ---------------------------------------------------------------------------
# Filename safety
# ---------------------------------------------------------------------------

# Characters that are safe on all major platforms in addition to alphanumerics.
_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@-_.")


def _safe_shot_id(shot_id: str) -> str:
    """Return a filesystem-safe version of *shot_id*.

    Shot IDs already use ``@``, ``-``, ``_``, and alphanumerics which are safe
    everywhere.  Any other characters (e.g. spaces, ``/``, ``:``) are replaced
    with ``_``.  The transformation is reversible via the manifest.
    """
    return "".join(c if c in _SAFE_CHARS else "_" for c in shot_id)


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------

def _load_manifest(path: Path) -> dict:
    """Load an existing manifest JSON, returning {} if missing or unreadable."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Frame extraction (ffmpeg)
# ---------------------------------------------------------------------------

def _probe_video_fps(video_path: Path) -> float:
    """Return the video frame rate via ffprobe.  Falls back to 24.0."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        raw = result.stdout.strip()
        if "/" in raw:
            num, den = raw.split("/", 1)
            fps = float(num) / float(den)
        else:
            fps = float(raw)
        return fps if fps > 0 else 24.0
    except Exception:
        return 24.0


def _extract_frame_jpg(video_path: Path, frame_number: int, out_path: Path, fps: float = 24.0) -> bool:
    """Extract a single frame by frame number using ffmpeg fast input-seeking.

    Converts *frame_number* to a timestamp using *fps*, then uses ``-ss``
    before ``-i`` for fast seeking.  This is consistent with the rest of the
    project (see ``services/frame_match._extract_frame_at``) and avoids the
    O(N) decode cost of ``select=eq(n\\,N)`` for large frame numbers.

    Returns True on success, False on any error.
    """
    position = frame_number / fps
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-ss", f"{position:.6f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", "2",
            "-y",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, timeout=60, capture_output=True)
        return out_path.exists() and out_path.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Annotation helpers
# ---------------------------------------------------------------------------

def _find_annotation_json_for_media_id(
    project_path: str, media_type: str, media_id: str
) -> Path | None:
    """Find the annotation JSON whose shots have shot_ids prefixed with *media_id*.

    Scans all JSON files in ``data/annotations/shots/<media_type>/`` and returns
    the first whose first entry's shot_id starts with ``<media_id>@``.

    Returns None if no matching file is found.
    """
    ann_dir = Path(project_path) / "data" / "annotations" / "shots" / media_type
    if not ann_dir.exists():
        return None

    prefix = f"{media_id}@"
    for json_file in sorted(ann_dir.glob("*.json")):
        if json_file.name.endswith(".manifest.json"):
            continue
        try:
            raw = json.loads(json_file.read_text(encoding="utf-8"))
            if not raw:
                continue
            first_shot_id = raw[0].get("shot", {}).get("shot_id", "")
            if str(first_shot_id).startswith(prefix):
                return json_file
        except Exception:
            continue
    return None


def _media_id_from_shot_id(shot_id: str) -> str | None:
    """Extract the media_id prefix from a shot_id like ``tmdb_123@f001-f002``."""
    if "@" in shot_id:
        return shot_id.split("@")[0]
    return None


def _filename_from_annotation(entries: list) -> str | None:
    """Read ``movie.filename`` from the first annotation entry."""
    if entries:
        return entries[0].get("movie", {}).get("filename") or None
    return None


# ---------------------------------------------------------------------------
# Core extraction function
# ---------------------------------------------------------------------------

def extract_best_frames_for_media(
    project_path: str,
    media_type: str,
    media_id: str | None = None,
    filename: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    verbose: bool = False,
) -> dict:
    """Extract one representative JPG per annotated shot for a single media item.

    Parameters
    ----------
    project_path:
        Absolute path to the Crossing project root.
    media_type:
        ``"movie"`` or ``"gameplay"``.
    media_id:
        Stable media identifier (e.g. ``game_rdr2_ce5e0bba``, ``tmdb_14168``).
        Used to locate the annotation JSON and to name the output directory.
        Provide either *media_id* or *filename*, not both.
    filename:
        Video filename (e.g. ``film.mp4``).  The annotation JSON is resolved
        from the file's stem.  *media_id* is inferred from the shot IDs.
        Provide either *media_id* or *filename*, not both.
    force:
        Overwrite existing JPGs.
    dry_run:
        Print what would be extracted without writing any files.
    limit:
        Process at most this many shots (useful for testing).
    verbose:
        Print per-shot progress.

    Returns
    -------
    dict with keys:
        ``media_type``, ``media_id``, ``filename``, ``output_dir``,
        ``items_total``, ``items_created``, ``items_reused``,
        ``items_skipped``, ``items_failed``, ``frames``
    """
    from data.metadata import normalize_media_type

    media_type = normalize_media_type(media_type)

    # ------------------------------------------------------------------
    # Resolve annotation JSON path
    # ------------------------------------------------------------------
    ann_path: Path | None = None

    if filename is not None:
        stem = Path(filename).stem
        ann_path = (
            Path(project_path) / "data" / "annotations" / "shots" / media_type / f"{stem}.json"
        )
        if not ann_path.exists() and media_type == "movie":
            # backward-compat legacy folder
            legacy = (
                Path(project_path) / "data" / "annotations" / "shots" / "movies" / f"{stem}.json"
            )
            if legacy.exists():
                ann_path = legacy
        if not ann_path.exists():
            raise FileNotFoundError(
                f"Annotation JSON not found for {filename!r} ({media_type}): {ann_path}"
            )
    elif media_id is not None:
        ann_path = _find_annotation_json_for_media_id(project_path, media_type, media_id)
        if ann_path is None:
            raise FileNotFoundError(
                f"No annotation JSON found for media_id={media_id!r} ({media_type})"
            )
    else:
        raise ValueError("Provide either media_id or filename")

    # ------------------------------------------------------------------
    # Load annotation entries
    # ------------------------------------------------------------------
    try:
        entries = json.loads(ann_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Could not parse annotation JSON {ann_path}: {exc}") from exc

    if not entries:
        raise ValueError(f"Annotation JSON is empty: {ann_path}")

    # Resolve filename from annotation if not provided
    if filename is None:
        filename = _filename_from_annotation(entries)
        if not filename:
            filename = ann_path.stem + ".mp4"

    # Infer media_id from the first shot's shot_id if not provided
    if media_id is None:
        first_shot_id = str(entries[0].get("shot", {}).get("shot_id", ""))
        media_id = _media_id_from_shot_id(first_shot_id) or Path(filename).stem

    # ------------------------------------------------------------------
    # Resolve source video
    # ------------------------------------------------------------------
    video_path = Path(project_path) / "media" / "videos" / media_type / filename
    if not video_path.exists():
        raise FileNotFoundError(
            f"Source video not found: {video_path}"
        )

    # Probe FPS once for this video (used for timestamp-based seeking)
    fps = _probe_video_fps(video_path)

    # ------------------------------------------------------------------
    # Prepare output directories and manifest
    # ------------------------------------------------------------------
    out_dir = frames_dir(project_path, media_type, media_id)
    mf_path = manifest_path(project_path, media_type, media_id)

    now_iso = datetime.now(timezone.utc).isoformat()
    existing_manifest = _load_manifest(mf_path)
    created_at = existing_manifest.get("created_at", now_iso)

    # Build a lookup of already-extracted frames (from the existing manifest)
    existing_frames: dict[str, str] = {}
    for item in existing_manifest.get("frames", []):
        sid = item.get("shot_id", "")
        if sid and item.get("status") in ("created", "reused"):
            existing_frames[sid] = item.get("path", "")

    # ------------------------------------------------------------------
    # Process shots
    # ------------------------------------------------------------------
    frame_records: list[dict] = []
    counts = {"created": 0, "reused": 0, "skipped": 0, "failed": 0}

    # Apply limit
    shot_entries = entries if limit is None else entries[:limit]

    for entry in shot_entries:
        shot_data = entry.get("shot", {})
        shot_id = str(shot_data.get("shot_id", ""))
        if not shot_id:
            continue

        best_frame_meta = shot_data.get("best_frame")
        if not best_frame_meta:
            record = {
                "shot_id": shot_id,
                "frame": None,
                "path": None,
                "status": "skipped",
                "reason": "no best_frame in annotation",
            }
            frame_records.append(record)
            counts["skipped"] += 1
            if verbose:
                print(f"  skip  {shot_id}  (no best_frame)")
            continue

        frame_number = best_frame_meta.get("frame")
        if frame_number is None:
            record = {
                "shot_id": shot_id,
                "frame": None,
                "path": None,
                "status": "skipped",
                "reason": "best_frame has no frame number",
            }
            frame_records.append(record)
            counts["skipped"] += 1
            if verbose:
                print(f"  skip  {shot_id}  (best_frame missing frame number)")
            continue

        jpg_path = frame_jpg_path(project_path, media_type, media_id, shot_id)
        rel_path = str(jpg_path.relative_to(Path(project_path)))

        # Dry-run: report without writing
        if dry_run:
            action = "reuse" if (jpg_path.exists() and not force) else "extract"
            print(f"  {action}  {shot_id}  frame={frame_number}  → {rel_path}")
            record = {
                "shot_id": shot_id,
                "frame": frame_number,
                "path": rel_path,
                "status": "dry_run",
                "reason": None,
            }
            frame_records.append(record)
            continue

        # Idempotency: reuse if the file exists and force is not set
        if jpg_path.exists() and not force:
            counts["reused"] += 1
            record = {
                "shot_id": shot_id,
                "frame": frame_number,
                "path": rel_path,
                "status": "reused",
                "reason": None,
            }
            frame_records.append(record)
            if verbose:
                print(f"  reuse {shot_id}  frame={frame_number}")
            continue

        # Extract
        ok = _extract_frame_jpg(video_path, frame_number, jpg_path, fps)
        if ok:
            counts["created"] += 1
            status = "created"
            reason = None
            if verbose:
                print(f"  ✓     {shot_id}  frame={frame_number}")
        else:
            counts["failed"] += 1
            status = "failed"
            reason = "ffmpeg extraction failed"
            # Remove any partial output
            if jpg_path.exists():
                try:
                    jpg_path.unlink()
                except Exception:
                    pass
            if verbose:
                print(f"  ✗     {shot_id}  frame={frame_number}  (extraction failed)")

        record = {
            "shot_id": shot_id,
            "frame": frame_number,
            "path": rel_path,
            "status": status,
            "reason": reason,
        }
        frame_records.append(record)

    # ------------------------------------------------------------------
    # Write manifest (unless dry-run)
    # ------------------------------------------------------------------
    items_total = len(frame_records)
    if not dry_run:
        manifest_data: dict[str, Any] = {
            "version": 1,
            "media_type": media_type,
            "media_id": media_id,
            "source_video": str(video_path.relative_to(Path(project_path))),
            "source_annotation": str(ann_path.relative_to(Path(project_path))),
            "created_at": created_at,
            "updated_at": now_iso,
            "frame_source": "shot.best_frame.frame",
            "image_format": "jpg",
            "items_total": items_total,
            "items_created": counts["created"],
            "items_reused": counts["reused"],
            "items_skipped": counts["skipped"],
            "items_failed": counts["failed"],
            "force": force,
            "frames": frame_records,
        }
        _write_manifest(mf_path, manifest_data)

    return {
        "media_type": media_type,
        "media_id": media_id,
        "filename": filename,
        "output_dir": str(out_dir.relative_to(Path(project_path))),
        "items_total": items_total,
        "items_created": counts["created"],
        "items_reused": counts["reused"],
        "items_skipped": counts["skipped"],
        "items_failed": counts["failed"],
    }


# ---------------------------------------------------------------------------
# Batch extraction (all annotation files for a media type)
# ---------------------------------------------------------------------------

def extract_best_frames_for_all(
    project_path: str,
    media_type: str,
    force: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    verbose: bool = False,
) -> list[dict]:
    """Run ``extract_best_frames_for_media`` for every annotation JSON found.

    Returns a list of per-item summary dicts (same structure as
    ``extract_best_frames_for_media`` return value, with an added ``error``
    key when processing fails for a file).
    """
    from data.metadata import normalize_media_type

    media_type = normalize_media_type(media_type)

    ann_dir = Path(project_path) / "data" / "annotations" / "shots" / media_type
    if not ann_dir.exists():
        return []

    results: list[dict] = []
    for json_file in sorted(ann_dir.glob("*.json")):
        if json_file.name.endswith(".manifest.json"):
            continue

        # Derive filename from annotation content
        try:
            raw = json.loads(json_file.read_text(encoding="utf-8"))
        except Exception:
            results.append({"error": f"Could not parse {json_file.name}", "media_id": None})
            continue

        if not raw:
            continue

        fn = raw[0].get("movie", {}).get("filename") if raw else None
        if not fn:
            fn = json_file.stem + ".mp4"

        try:
            summary = extract_best_frames_for_media(
                project_path,
                media_type,
                filename=fn,
                force=force,
                dry_run=dry_run,
                limit=limit,
                verbose=verbose,
            )
            results.append(summary)
        except Exception as exc:
            results.append({
                "media_type": media_type,
                "media_id": None,
                "filename": fn,
                "output_dir": None,
                "items_total": 0,
                "items_created": 0,
                "items_reused": 0,
                "items_skipped": 0,
                "items_failed": 0,
                "error": str(exc),
            })

    return results
