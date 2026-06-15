"""Draft shotlist creation for gameplay (and movie) sources.

Implements a conservative candidate-boundary strategy that does NOT require
TransNetV2 / TensorFlow.  It combines three weak signals:

  1. Coarse temporal sampling  – guarantees segments never exceed a max
     duration, giving the editor a reasonable number of rows to work with.
  2. Simple frame-difference motion signal – flags moments of high visual
     change (scene transitions, hard cuts, dramatic camera movement).
  3. Minimum segment length constraint – prevents the draft from being
     flooded with tiny one-frame artefacts.

The result is a rough, *reviewable* shotlist that should be opened in the
Shotlist Visualizer and cleaned up by hand.  It is explicitly not the final
authoritative segmentation.

Public API
----------
create_draft_shotlist(project_path, media_id, media_type, *, force, min_shot_sec, max_shot_sec, motion_threshold) -> Path
"""

from __future__ import annotations

import csv
import subprocess
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Video introspection helpers
# ---------------------------------------------------------------------------

def _probe_video(video_path: str) -> dict[str, Any]:
    """Return basic video metadata via ffprobe.

    Returns dict with keys: duration_sec (float), fps (float), total_frames (int).
    Raises RuntimeError if ffprobe is not available or the file cannot be read.
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(video_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise RuntimeError(
            "ffprobe not found. Install ffmpeg: sudo apt install ffmpeg"
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {video_path}:\n{result.stderr[:400]}"
        )

    data = json.loads(result.stdout)
    duration_sec: float = float(data.get("format", {}).get("duration", 0) or 0)

    fps: float = 25.0
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            r_frame_rate = stream.get("r_frame_rate", "25/1")
            try:
                num, den = r_frame_rate.split("/")
                fps = float(num) / float(den)
            except (ValueError, ZeroDivisionError):
                fps = 25.0
            break

    total_frames = int(round(duration_sec * fps))
    return {"duration_sec": duration_sec, "fps": fps, "total_frames": total_frames}


def _timecode(seconds: float) -> str:
    """Format seconds as HH:MM:SS.mmm."""
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# ---------------------------------------------------------------------------
# Motion-based boundary detection (OpenCV)
# ---------------------------------------------------------------------------

def _detect_motion_boundaries(
    video_path: str,
    fps: float,
    total_frames: int,
    *,
    min_shot_sec: float,
    max_shot_sec: float,
    motion_threshold: float,
    sample_every_n_frames: int,
) -> list[int]:
    """Return a list of boundary frame indices using frame differencing.

    Parameters
    ----------
    video_path:
        Absolute path to the video file.
    fps:
        Video frame rate (used to convert seconds to frames).
    total_frames:
        Approximate total frame count from ffprobe.
    min_shot_sec:
        Hard minimum shot duration; candidate boundaries closer than this
        to the previous boundary are dropped.
    max_shot_sec:
        Maximum shot duration; a forced boundary is inserted every
        ``max_shot_sec`` seconds if no motion boundary was found.
    motion_threshold:
        Mean absolute pixel difference (0–255 scale) above which a frame
        transition is considered a boundary candidate.
    sample_every_n_frames:
        Read only every N-th frame to speed up processing.

    Returns
    -------
    Sorted list of boundary frame indices (start-of-shot positions,
    beginning with 0).
    """
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return []  # Fall back to temporal-only

    min_shot_frames = int(round(min_shot_sec * fps))
    max_shot_frames = int(round(max_shot_sec * fps))

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []

    boundaries: list[int] = [0]  # first shot always starts at frame 0
    prev_gray = None
    frame_idx = 0
    last_boundary_frame = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % sample_every_n_frames == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            # Resize to small thumbnail for speed
            gray_small = cv2.resize(gray, (160, 90))

            if prev_gray is not None:
                diff = np.mean(np.abs(gray_small.astype(np.float32) - prev_gray.astype(np.float32)))

                frames_since_last = frame_idx - last_boundary_frame

                # Force a boundary if max shot duration exceeded
                if frames_since_last >= max_shot_frames:
                    boundaries.append(frame_idx)
                    last_boundary_frame = frame_idx
                elif diff >= motion_threshold and frames_since_last >= min_shot_frames:
                    boundaries.append(frame_idx)
                    last_boundary_frame = frame_idx

            prev_gray = gray_small

        frame_idx += 1

    cap.release()

    # Remove duplicates and sort
    return sorted(set(boundaries))


# ---------------------------------------------------------------------------
# Temporal-only fallback (no OpenCV)
# ---------------------------------------------------------------------------

def _detect_temporal_boundaries(
    total_frames: int,
    fps: float,
    max_shot_sec: float,
) -> list[int]:
    """Return boundary frames at regular intervals (no CV required)."""
    step = int(round(max_shot_sec * fps))
    if step < 1:
        step = 1
    return list(range(0, total_frames, step))


# ---------------------------------------------------------------------------
# Convert boundaries → shot rows
# ---------------------------------------------------------------------------

def _boundaries_to_shots(
    boundaries: list[int],
    total_frames: int,
    fps: float,
    media_id: str,
) -> list[dict[str, Any]]:
    """Convert a sorted list of start-of-shot frame indices into shot rows."""
    from data.media_id import build_shot_id

    shots: list[dict[str, Any]] = []
    n = len(boundaries)
    for i, start_frame in enumerate(boundaries):
        end_frame = (boundaries[i + 1] - 1) if i + 1 < n else (total_frames - 1)
        if end_frame < start_frame:
            end_frame = start_frame

        start_sec = start_frame / fps
        end_sec = end_frame / fps

        shot_id = build_shot_id(media_id, start_frame, end_frame) if media_id else ""
        shots.append({
            "Ignore": "No",
            "Scene": "0",
            "start_time": _timecode(start_sec),
            "end_time": _timecode(end_sec),
            "start_frame": start_frame,
            "end_frame": end_frame,
            "shot_id": shot_id,
            "Shot_Caption": "",
            "Scene_Caption": "",
        })
    return shots


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def create_draft_shotlist(
    project_path: str,
    media_id: str,
    media_type: str = "gameplay",
    *,
    force: bool = False,
    min_shot_sec: float = 3.0,
    max_shot_sec: float = 30.0,
    motion_threshold: float = 8.0,
    verbose: bool = False,
) -> Path:
    """Create a provisional draft shotlist for *media_id* and write it to disk.

    Parameters
    ----------
    project_path:
        Root of the Crossing project.
    media_id:
        Stable ``media_id`` of the source (e.g. ``"game_rdr2_ce5e0bba"``).
    media_type:
        ``"gameplay"`` or ``"movie"``.  Determines metadata lookup and output
        folder.
    force:
        Overwrite an existing shotlist CSV if ``True``; raise
        ``FileExistsError`` otherwise.
    min_shot_sec:
        Minimum shot duration in seconds.  Candidate boundaries closer than
        this to the previous one are discarded.
    max_shot_sec:
        Maximum shot duration in seconds.  A forced boundary is inserted when
        no motion boundary is found within this interval.
    motion_threshold:
        Mean absolute pixel difference threshold (0–255).  Lower values detect
        more changes; higher values detect only strong cuts.
    verbose:
        Print progress to stdout.

    Returns
    -------
    Path to the written CSV file.

    Raises
    ------
    ValueError
        If the media_id is not found in the metadata.
    FileNotFoundError
        If the source video is missing on disk.
    FileExistsError
        If the shotlist CSV already exists and *force* is False.
    RuntimeError
        If ffprobe is unavailable or the video cannot be read.
    """
    from data.metadata import get_metadata
    from data.shotlist import get_shotlist_path

    # ------------------------------------------------------------------
    # 1. Resolve metadata record
    # ------------------------------------------------------------------
    entries = get_metadata(project_path, media_type=media_type)
    record = next((e for e in entries if e.get("media_id") == media_id), None)
    if record is None:
        raise ValueError(
            f"No {media_type} record found with media_id={media_id!r}. "
            "Check `crossing media list --media gameplay`."
        )

    filename = record["filename"]
    video_path = Path(project_path) / "media" / "videos" / media_type / filename
    if not video_path.exists():
        raise FileNotFoundError(
            f"Source video not found: {video_path}\n"
            f"Import it first with `crossing media import --media {media_type}`."
        )

    # ------------------------------------------------------------------
    # 2. Check output path
    # ------------------------------------------------------------------
    shotlist_path = get_shotlist_path(project_path, filename, media_type)
    if shotlist_path.exists() and not force:
        raise FileExistsError(
            f"Draft shotlist already exists: {shotlist_path}\n"
            "Use --force to overwrite."
        )

    # ------------------------------------------------------------------
    # 3. Probe video
    # ------------------------------------------------------------------
    if verbose:
        print(f"  Probing: {filename}")
    info = _probe_video(str(video_path))
    fps = info["fps"]
    total_frames = info["total_frames"]
    duration_sec = info["duration_sec"]

    if verbose:
        print(f"  Duration: {duration_sec:.1f}s  FPS: {fps:.3f}  Frames: {total_frames}")

    # ------------------------------------------------------------------
    # 4. Detect boundaries
    # ------------------------------------------------------------------
    # Sample every ~0.5 s for the motion pass (balances speed vs coverage).
    sample_every = max(1, int(round(fps * 0.5)))

    if verbose:
        print(f"  Sampling every {sample_every} frames for motion detection …")

    try:
        import cv2 as _cv2  # noqa: F401
        boundaries = _detect_motion_boundaries(
            str(video_path),
            fps,
            total_frames,
            min_shot_sec=min_shot_sec,
            max_shot_sec=max_shot_sec,
            motion_threshold=motion_threshold,
            sample_every_n_frames=sample_every,
        )
        strategy = "motion+temporal"
    except ImportError:
        if verbose:
            print("  OpenCV not available — using temporal-only sampling.")
        boundaries = _detect_temporal_boundaries(total_frames, fps, max_shot_sec)
        strategy = "temporal"

    if not boundaries or boundaries[0] != 0:
        boundaries = [0] + boundaries

    if verbose:
        print(f"  Strategy: {strategy}  Candidate boundaries: {len(boundaries)}")

    # ------------------------------------------------------------------
    # 5. Convert to shot rows
    # ------------------------------------------------------------------
    shots = _boundaries_to_shots(boundaries, total_frames, fps, media_id)

    if verbose:
        print(f"  Shots in draft: {len(shots)}")

    # ------------------------------------------------------------------
    # 6. Write CSV
    # ------------------------------------------------------------------
    shotlist_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "Ignore", "Scene", "start_time", "end_time",
        "start_frame", "end_frame", "shot_id",
        "Shot_Caption", "Scene_Caption",
    ]
    with open(shotlist_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(shots)

    return shotlist_path
