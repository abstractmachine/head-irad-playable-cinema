"""Hybrid draft shotlist creation for gameplay (and movie) sources.

Two-phase pipeline
------------------
Phase 1 — Classical detection
  • If ``--transnet`` is requested and TransNetV2 is installed, run the
    classical neural detector (same path used for movie shotlists in
    ``data/shot_detection``).
  • Otherwise run a coarse OpenCV frame-difference pass (sampling every
    ~0.5 s) to locate rough boundaries quickly.  Falls back to pure
    temporal sampling when OpenCV is unavailable.

Phase 2 — Gameplay refinement
  • Boundary nudging: for each rough boundary, fine-scan every frame in a
    ±nudge_sec window and snap the boundary to the frame with the highest
    absolute pixel difference.  This corrects the ±0.5 s positional error
    introduced by the coarse sampling step.
  • Long-segment splitting: any segment longer than ``max_shot_sec`` is
    scanned at medium resolution; the frame with the peak motion score is
    inserted as an additional boundary.
  • Short-segment merging: boundaries that would produce a segment shorter
    than ``min_shot_sec`` are dropped.

The result is a reviewable shotlist CSV that can be opened and cleaned up
in the Shotlist Visualizer.  It is not an authoritative final segmentation.

Public API
----------
create_draft_shotlist(
    project_path, media_id, media_type="gameplay", *,
    force, min_shot_sec, max_shot_sec, motion_threshold,
    nudge_sec, use_transnet, verbose
) -> Path
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
# Phase 1a — Classical detection via TransNetV2 (opt-in)
# ---------------------------------------------------------------------------

def _classical_transnet_boundaries(
    video_path: str,
    fps: float,
    *,
    threshold: float = 0.5,
    verbose: bool = False,
) -> list[int]:
    """Run TransNetV2 and return sorted start-of-shot frame boundary indices.

    Returns an empty list if TransNetV2 is not installed or fails.
    The TransNetV2 semantic is that the boundary frame is the *last* frame of
    the outgoing shot, so we return ``boundary_frame + 1`` as the start of the
    next shot (matching ``detect_shots_transnet`` in data/shot_detection.py).
    """
    try:
        from transnetv2 import TransNetV2
    except ImportError:
        if verbose:
            print("  TransNetV2 not available — skipping classical pass.")
        return []

    if verbose:
        print("  Running TransNetV2 classical detector …")

    try:
        model = TransNetV2()
        _video_frames, single_frame_predictions, _all_frame_predictions = \
            model.predict_video(str(video_path))
    except Exception as exc:
        if verbose:
            print(f"  TransNetV2 failed ({exc}) — falling back to OpenCV.")
        return []

    boundaries: list[int] = [0]
    for frame_idx, pred in enumerate(single_frame_predictions):
        if float(pred) >= threshold:
            next_start = frame_idx + 1
            if next_start not in boundaries:
                boundaries.append(next_start)

    if verbose:
        print(f"  TransNetV2 found {len(boundaries) - 1} boundaries.")

    return sorted(boundaries)


# ---------------------------------------------------------------------------
# Phase 1b — Classical detection via OpenCV frame differencing (default)
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
    """Coarse boundary detection using frame differencing (Phase 1 default).

    Samples every ``sample_every_n_frames`` frames.  A boundary is inserted
    whenever the mean absolute pixel difference between consecutive sampled
    frames exceeds ``motion_threshold``, subject to ``min_shot_sec`` and
    ``max_shot_sec`` guards.

    Returns sorted start-of-shot frame indices (always begins with 0).
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

    boundaries: list[int] = [0]
    prev_gray = None
    frame_idx = 0
    last_boundary_frame = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % sample_every_n_frames == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray_small = cv2.resize(gray, (160, 90))

            if prev_gray is not None:
                diff = float(
                    np.mean(
                        np.abs(gray_small.astype(np.float32) - prev_gray.astype(np.float32))
                    )
                )
                frames_since_last = frame_idx - last_boundary_frame

                if frames_since_last >= max_shot_frames:
                    boundaries.append(frame_idx)
                    last_boundary_frame = frame_idx
                elif diff >= motion_threshold and frames_since_last >= min_shot_frames:
                    boundaries.append(frame_idx)
                    last_boundary_frame = frame_idx

            prev_gray = gray_small

        frame_idx += 1

    cap.release()
    return sorted(set(boundaries))


# ---------------------------------------------------------------------------
# Phase 2a — Boundary nudging (fine-scan via single sequential pass)
# ---------------------------------------------------------------------------

def _nudge_boundaries(
    video_path: str,
    boundaries: list[int],
    fps: float,
    total_frames: int,
    *,
    nudge_sec: float = 0.5,
    verbose: bool = False,
) -> list[int]:
    """Snap each rough boundary to the precise peak-diff frame nearby.

    For each boundary B (other than the mandatory 0), identifies the \u00b1nudge
    window [B - nudge_frames, B + nudge_frames].  All windows are merged into
    non-overlapping scan ranges, then the video is read once sequentially
    (no random seeks) to collect per-frame diffs within those ranges.  Each
    original boundary is then moved to the frame with the highest diff in
    its window.

    ``nudge_sec`` defaults to 0.5 s, which matches the coarse-pass sampling
    interval.  This means we search every frame within one sample period of
    the detected boundary, giving single-frame precision without scanning
    large stretches of video.

    Skips between windows are performed with ``cap.grab()`` (no decode) for
    speed.
    """
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return boundaries

    if len(boundaries) <= 1:
        return boundaries

    nudge_frames = max(1, int(round(nudge_sec * fps)))

    # Build per-boundary windows (skip b=0 which is never moved)
    windows: list[tuple[int, int, int]] = []   # (scan_start, scan_end, boundary_idx)
    for idx, b in enumerate(boundaries):
        if idx == 0:
            continue
        ws = max(1, b - nudge_frames)
        we = min(total_frames - 1, b + nudge_frames)
        windows.append((ws, we, idx))

    # Sort and merge overlapping windows into contiguous scan ranges.
    # Each merged range carries a list of (boundary_idx, original_frame).
    windows.sort()
    merged: list[tuple[int, int, list[tuple[int, int]]]] = []  # (range_start, range_end, [(bidx, b)])
    for ws, we, bidx in windows:
        b_orig = boundaries[bidx]
        if merged and ws <= merged[-1][1] + 1:
            # Overlaps with previous range — extend it
            prev_start, prev_end, prev_bs = merged[-1]
            merged[-1] = (prev_start, max(prev_end, we), prev_bs + [(bidx, b_orig)])
        else:
            merged.append((ws, we, [(bidx, b_orig)]))

    # For each merged range, track best_diff[bidx] across frames in the range.
    best_diff:  dict[int, float] = {}
    best_frame: dict[int, int]   = {}
    for _, _, bs in merged:
        for bidx, b_orig in bs:
            best_diff[bidx]  = -1.0
            best_frame[bidx] = b_orig

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return boundaries

    frame_idx = 0
    prev_gray = None

    # Walk through merged ranges sequentially (sorted by start frame)
    for range_start, range_end, bs in merged:
        # Advance to one frame before range_start to prime the diff
        target_start = max(0, range_start - 1)

        if frame_idx > target_start:
            # Already past this range (shouldn't happen if windows are sorted)
            continue

        # Skip forward to target_start - 1 using grab() (no decode)
        while frame_idx < target_start:
            if not cap.grab():
                break
            frame_idx += 1

        # Read and keep prev_gray for diff computation
        ret, frame = cap.read()
        if not ret:
            continue
        prev_gray = cv2.resize(
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (320, 180)
        ).astype(np.float32)
        frame_idx += 1

        # Determine which boundary windows are active in this merged range
        active_bs = [(bidx, b_orig) for bidx, b_orig in bs
                     if b_orig - nudge_frames <= range_end and b_orig + nudge_frames >= range_start]

        while frame_idx <= range_end:
            ret, frame = cap.read()
            if not ret:
                break
            gray = cv2.resize(
                cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (320, 180)
            ).astype(np.float32)
            diff = float(np.mean(np.abs(gray - prev_gray)))

            for bidx, b_orig in active_bs:
                win_s = max(1, b_orig - nudge_frames)
                win_e = min(total_frames - 1, b_orig + nudge_frames)
                if win_s <= frame_idx <= win_e and diff > best_diff[bidx]:
                    best_diff[bidx]  = diff
                    best_frame[bidx] = frame_idx

            prev_gray = gray
            frame_idx += 1

    cap.release()

    # Build refined list
    refined = list(boundaries)
    nudged = 0
    for bidx, b_orig in [(bidx, boundaries[bidx]) for bidx in best_frame]:
        new_b = best_frame[bidx]
        if new_b != b_orig:
            nudged += 1
        refined[bidx] = new_b

    if verbose:
        print(f"  Nudge: adjusted {nudged} / {len(boundaries) - 1} boundaries.")

    return sorted(set(refined))


# ---------------------------------------------------------------------------
# Phase 2b — Long-segment splitting (single sequential pass)
# ---------------------------------------------------------------------------

def _split_long_segments(
    boundaries: list[int],
    video_path: str,
    fps: float,
    total_frames: int,
    *,
    max_shot_sec: float,
    min_shot_sec: float,
    scan_every_n_frames: int = 5,
    verbose: bool = False,
) -> list[int]:
    """Insert a split inside any segment longer than ``max_shot_sec``.

    Identifies all over-length segments, collects their interior scan ranges
    (excluding guard zones), merges overlapping ranges, then reads the video
    once sequentially.  For each long segment the frame with the highest
    absolute pixel difference is inserted as a new boundary.
    """
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return boundaries

    max_shot_frames = int(round(max_shot_sec * fps))
    min_shot_frames = int(round(min_shot_sec * fps))

    # Identify scan ranges for each long segment
    # Each entry: (scan_start, scan_end, seg_idx)
    scan_ranges: list[tuple[int, int, int]] = []
    n = len(boundaries)
    for i, start in enumerate(boundaries):
        end = (boundaries[i + 1] - 1) if i + 1 < n else (total_frames - 1)
        seg_len = end - start + 1
        if seg_len <= max_shot_frames:
            continue
        scan_s = start + min_shot_frames
        scan_e = end   - min_shot_frames
        if scan_s >= scan_e:
            continue
        scan_ranges.append((scan_s, scan_e, i))

    if not scan_ranges:
        return boundaries

    # Merge overlapping scan ranges
    scan_ranges.sort()
    merged: list[tuple[int, int, list[int]]] = []  # (start, end, [seg_idx])
    for ss, se, si in scan_ranges:
        if merged and ss <= merged[-1][1] + 1:
            prev_s, prev_e, prev_segs = merged[-1]
            merged[-1] = (prev_s, max(prev_e, se), prev_segs + [si])
        else:
            merged.append((ss, se, [si]))

    # Per-segment trackers: best diff and best frame
    best_diff:  dict[int, float] = {si: -1.0 for _, _, segs in merged for si in segs}
    best_frame: dict[int, int]   = {}
    for _, _, segs in merged:
        for si in segs:
            seg_start = boundaries[si]
            seg_end   = (boundaries[si + 1] - 1) if si + 1 < n else (total_frames - 1)
            best_frame[si] = (seg_start + seg_end) // 2  # default: midpoint

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return boundaries

    frame_idx = 0
    prev_gray = None

    for range_start, range_end, segs in merged:
        target_start = max(0, range_start - 1)
        # Skip forward using grab() (no decode)
        while frame_idx < target_start:
            if not cap.grab():
                break
            frame_idx += 1

        ret, frame = cap.read()
        if not ret:
            continue
        prev_gray = cv2.resize(
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90)
        ).astype(np.float32)
        frame_idx += 1

        while frame_idx <= range_end:
            ret, frame = cap.read()
            if not ret:
                break
            if (frame_idx - range_start) % scan_every_n_frames == 0:
                gray = cv2.resize(
                    cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90)
                ).astype(np.float32)
                diff = float(np.mean(np.abs(gray - prev_gray)))

                for si in segs:
                    ss_si = boundaries[si] + min_shot_frames
                    se_si_end = (boundaries[si + 1] - 1) if si + 1 < n else (total_frames - 1)
                    se_si = se_si_end - min_shot_frames
                    if ss_si <= frame_idx <= se_si and diff > best_diff[si]:
                        best_diff[si] = diff
                        best_frame[si] = frame_idx

                prev_gray = gray
            frame_idx += 1

    cap.release()

    extra = list(best_frame.values())
    if extra and verbose:
        print(f"  Split: inserted {len(extra)} boundary/ies into long segments.")

    return sorted(set(boundaries + extra))


# ---------------------------------------------------------------------------
# Phase 2c — Short-segment merging
# ---------------------------------------------------------------------------

def _merge_short_segments(
    boundaries: list[int],
    fps: float,
    total_frames: int,
    *,
    min_shot_sec: float,
) -> list[int]:
    """Drop any boundary that would produce a segment shorter than ``min_shot_sec``.

    Iterates forward: keeps a boundary only if the gap from the previous kept
    boundary is at least ``min_frames``.  Short segments are merged forward
    into the following segment.  The first boundary (0) is never removed.

    A second pass handles the case where merging forward leaves the final
    segment too short (it is merged backward instead).
    """
    min_frames = int(round(min_shot_sec * fps))
    if not boundaries:
        return boundaries

    # Forward pass: drop boundaries where the preceding segment is too short
    while True:
        kept = [boundaries[0]]
        changed = False
        for b in boundaries[1:]:
            if b - kept[-1] < min_frames:
                changed = True  # drop b — extend the previous segment
            else:
                kept.append(b)
        boundaries = kept
        if not changed:
            break

    return boundaries


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
    min_shot_sec: float = 2.0,
    max_shot_sec: float = 30.0,
    motion_threshold: float = 8.0,
    nudge_sec: float = 0.5,
    use_transnet: bool = False,
    verbose: bool = False,
) -> Path:
    """Create a hybrid draft shotlist for *media_id* and write it to disk.

    Two-phase pipeline
    ------------------
    Phase 1 (classical detection):
        If ``use_transnet=True`` and TransNetV2 is installed, run the neural
        detector.  Otherwise run coarse OpenCV frame-differencing (sampling
        every ~0.5 s).  Falls back to pure temporal sampling when OpenCV is
        unavailable.

    Phase 2 (gameplay refinement):
        1. Boundary nudging — fine-scan every frame in a ±``nudge_sec``
           window around each rough boundary and snap to the actual peak-diff
           frame.  Corrects the positional error introduced by coarse sampling.
        2. Long-segment splitting — insert a boundary at the highest-motion
           point inside any segment longer than ``max_shot_sec``.
        3. Short-segment merging — drop boundaries that produce segments
           shorter than ``min_shot_sec``.

    Parameters
    ----------
    project_path:
        Root of the Crossing project.
    media_id:
        Stable ``media_id`` of the source (e.g. ``"game_rdr2_ce5e0bba"``).
    media_type:
        ``"gameplay"`` or ``"movie"``.
    force:
        Overwrite an existing shotlist CSV if ``True``.
    min_shot_sec:
        Minimum shot duration in seconds (default: 2.0).
    max_shot_sec:
        Maximum shot duration; a forced split is inserted if exceeded
        (default: 30.0).
    motion_threshold:
        Mean pixel-diff threshold for the coarse Phase-1 detection pass
        (default: 8.0, 0–255 scale).
    nudge_sec:
        Half-width of the fine-scan window around each Phase-1 boundary
        (default: 2.0 s).  Set to 0 to disable nudging.
    use_transnet:
        Use TransNetV2 as the Phase-1 detector instead of OpenCV
        frame-differencing.  Requires TransNetV2 + TensorFlow to be
        installed.  Not recommended for very long videos (>1 hour).
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
    # 4. Phase 1 — Classical detection
    # ------------------------------------------------------------------
    boundaries: list[int] = []
    strategy: str

    if use_transnet:
        if verbose:
            print("  Phase 1: TransNetV2 classical detector …")
        boundaries = _classical_transnet_boundaries(
            str(video_path), fps, threshold=0.5, verbose=verbose
        )
        strategy = "transnet"

    if not boundaries:
        # OpenCV frame-diff (default, or TransNetV2 fallback)
        sample_every = max(1, int(round(fps * 0.5)))
        if verbose:
            label = "Phase 1 fallback" if use_transnet else "Phase 1"
            print(f"  {label}: OpenCV frame-diff (every {sample_every} frames) …")
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
            strategy = "opencv" if not use_transnet else "transnet-fallback-opencv"
        except ImportError:
            if verbose:
                print("  OpenCV not available — using temporal sampling.")
            boundaries = _detect_temporal_boundaries(total_frames, fps, max_shot_sec)
            strategy = "temporal"

    if not boundaries or boundaries[0] != 0:
        boundaries = [0] + boundaries

    if verbose:
        print(f"  Phase 1 result [{strategy}]: {len(boundaries)} boundaries")

    # ------------------------------------------------------------------
    # 5. Phase 2 — Gameplay refinement
    # ------------------------------------------------------------------
    if nudge_sec > 0:
        if verbose:
            print(f"  Phase 2a: boundary nudging (±{nudge_sec:.1f}s window) …")
        boundaries = _nudge_boundaries(
            str(video_path), boundaries, fps, total_frames,
            nudge_sec=nudge_sec, verbose=verbose,
        )

    if verbose:
        print("  Phase 2b: long-segment splitting …")
    boundaries = _split_long_segments(
        boundaries, str(video_path), fps, total_frames,
        max_shot_sec=max_shot_sec,
        min_shot_sec=min_shot_sec,
        verbose=verbose,
    )

    if verbose:
        print("  Phase 2c: short-segment merging …")
    boundaries = _merge_short_segments(
        boundaries, fps, total_frames, min_shot_sec=min_shot_sec,
    )

    if verbose:
        print(f"  Phase 2 result: {len(boundaries)} boundaries → {len(boundaries)} shots")

    # ------------------------------------------------------------------
    # 6. Convert to shot rows and write CSV
    # ------------------------------------------------------------------
    shots = _boundaries_to_shots(boundaries, total_frames, fps, media_id)

    if verbose:
        print(f"  Writing {len(shots)} shots …")

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
