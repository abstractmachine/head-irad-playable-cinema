"""CLIP-based best-frame matching for shot annotations.

For each shot in a film's annotation JSON:
  1. Read the shot's ``description`` field from the existing annotation
  2. Use a CLIP model to find the frame that best matches that description
  3. Save the frame as a PNG under ``media/frames/best/<media_type>/<stem>/<shot_id>.png``
  4. Store ``best_frame`` metadata inside the annotation JSON entry

This is a separate pass from ``annotate shot`` and does not modify any
existing annotation fields other than adding / updating ``best_frame``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from data.index import load_mapping, serialize_annotation_item
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOW_CONFIDENCE_THRESHOLD = 0.18
FALLBACK_SCORE = 0.0


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def compute_description_hash(description: str) -> str:
    """Return a short stable hash of *description* for change detection."""
    normalized = " ".join(description.strip().lower().split())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def best_frame_path(
    project_path: str,
    media_type: str,
    filename: str,
    shot_id: str,
) -> Path:
    """Return the deterministic output path for a best-frame PNG."""
    stem = Path(filename).stem
    return (
        Path(project_path)
        / "media" / "frames" / "best"
        / media_type / stem
        / f"{shot_id}.png"
    )


def load_best_frame_lookup(
    project_path: str,
    filename: str,
    media_type: str = "movies",
) -> dict:
    """Return a {shot_id: best_frame_dict} mapping for *filename*.

    Reads the annotation JSON for the given film and extracts the ``best_frame``
    entry stored at ``shot.best_frame`` for each shot that has one.  Does NOT
    recompute anything — only reads existing data.

    Returns an empty dict when the annotation file does not exist or has no
    best-frame data.
    """
    from data.annotate import get_annotation_json_path

    agg_path = get_annotation_json_path(project_path, filename, media_type)
    if not agg_path.exists():
        return {}

    entries = json.loads(agg_path.read_text(encoding="utf-8"))

    lookup: dict = {}
    for entry in entries:
        shot = entry.get("shot", {})
        bf = shot.get("best_frame")
        if not bf:
            continue
        lookup[shot.get("shot_id")] = bf

    return lookup


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def _load_clip_model(project_path: str, model_name: str):
    """Load CLIPModel + CLIPProcessor from the project's model directory.

    Search order (mirrors the logic in data/annotate.py):
      1. ``<project>/models/<model_name>/``  (preferred — local, no download)
      2. *model_name* as an explicit absolute / expanduser path
      3. HuggingFace repo id — downloads into ``<project>/models/<safe_name>/``

    Returns ``(model, processor, device)`` where *device* is ``"cuda"`` when
    a GPU is available, otherwise ``"cpu"``.
    """
    try:
        import torch
        from transformers import CLIPModel, CLIPProcessor
    except ImportError as exc:
        raise ImportError(
            "transformers and torch are required for frame matching.\n"
            "Install them with:  pip install transformers torch"
        ) from exc

    device = "cuda" if torch.cuda.is_available() else "cpu"

    project_local = Path(project_path) / "models" / model_name
    explicit_path = Path(model_name).expanduser()

    # A CLIP weight file is one of these names
    _WEIGHT_NAMES = {"model.safetensors", "pytorch_model.bin", "pytorch_model.bin.index.json"}

    def _has_weights(directory: Path) -> bool:
        return any((directory / w).exists() for w in _WEIGHT_NAMES)

    if project_local.exists() and _has_weights(project_local):
        source = str(project_local)
        try:
            model = CLIPModel.from_pretrained(source, local_files_only=True)
            processor = CLIPProcessor.from_pretrained(source, local_files_only=True)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load CLIP model from '{project_local}': {exc}"
            ) from exc

    elif explicit_path.exists() and _has_weights(explicit_path):
        source = str(explicit_path)
        try:
            model = CLIPModel.from_pretrained(source, local_files_only=True)
            processor = CLIPProcessor.from_pretrained(source, local_files_only=True)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load CLIP model from '{explicit_path}': {exc}"
            ) from exc

    else:
        # No complete local copy found — download from HuggingFace into the
        # project's model directory (reusing any partially-downloaded config
        # files that may already be present there).
        dest_dir = project_local if project_local.exists() else (
            Path(project_path) / "models" / model_name.replace(":", "_").replace("/", "_")
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        print(f"  Downloading CLIP model '{model_name}' into {dest_dir} …")
        try:
            model = CLIPModel.from_pretrained(model_name, cache_dir=str(dest_dir))
            processor = CLIPProcessor.from_pretrained(model_name, cache_dir=str(dest_dir))
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load CLIP model '{model_name}' from Hugging Face Hub "
                f"(and no complete local model found under '{project_local}').\n"
                f"Error: {exc}"
            ) from exc

    model = model.to(device)
    model.eval()
    return model, processor, device


# ---------------------------------------------------------------------------
# CLIP scoring
# ---------------------------------------------------------------------------

def clip_score_batch(
    images: List,
    text: str,
    model,
    processor,
    device: str,
) -> List[float]:
    """Score a batch of PIL images against a text query using CLIP.

    Returns a list of cosine-similarity scores (one per image), in the same
    order as *images*.  All images are processed in a single forward pass.
    """
    import torch

    inputs = processor(
        text=[text],
        images=images,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=77,
    ).to(device)

    with torch.no_grad():
        outputs = model(**inputs)

    image_embeds = outputs.image_embeds
    text_embeds = outputs.text_embeds

    image_embeds = image_embeds / image_embeds.norm(dim=-1, keepdim=True)
    text_embeds = text_embeds / text_embeds.norm(dim=-1, keepdim=True)

    scores = (image_embeds @ text_embeds.T).squeeze(-1)
    result = scores.cpu().tolist()
    if isinstance(result, float):
        result = [result]
    return result


# ---------------------------------------------------------------------------
# Frame extraction helpers
# ---------------------------------------------------------------------------

def _timecode_to_seconds(tc: str) -> float:
    parts = tc.split(":")
    try:
        seconds = float(parts[-1])
    except Exception:
        seconds = 0.0
    if len(parts) >= 2:
        try:
            seconds += int(parts[-2]) * 60
        except Exception:
            pass
    if len(parts) == 3:
        try:
            seconds += int(parts[-3]) * 3600
        except Exception:
            pass
    return seconds


def _get_video_fps(video_path: str) -> float:
    """Return the video frame rate via ffprobe.  Falls back to 25.0."""
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
        return fps if fps > 0 else 25.0
    except Exception:
        return 25.0


def _extract_frame_at(video_path: str, position: float, out_path: Path) -> bool:
    """Extract a single frame at *position* seconds using ffmpeg.

    Uses fast input-seeking (``-ss`` before ``-i``) so that long seeks do not
    block.  Returns ``True`` on success.
    """
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
        subprocess.run(cmd, check=True, timeout=30)
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception:
        return False


def _extract_frame_png(video_path: str, position: float, out_path: Path) -> bool:
    """Extract a single frame as PNG at *position* seconds."""
    try:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-ss", f"{position:.6f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-y",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, timeout=30)
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception:
        return False


def _extract_frame_pil(video_path: str, position: float, tmp_dir: Path):
    """Extract a frame and return ``(PIL.Image, path)`` or ``(None, None)``."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError(
            "Pillow is required for frame matching.  "
            "Install it with:  pip install Pillow"
        ) from exc

    tmp = tmp_dir / f"frame_{abs(hash(position)) % 10**9}.jpg"
    if _extract_frame_at(video_path, position, tmp):
        try:
            img = Image.open(str(tmp)).convert("RGB")
            img.load()
            return img, tmp
        except Exception:
            return None, None
    return None, None


# ---------------------------------------------------------------------------
# Sampling strategy
# ---------------------------------------------------------------------------

def _coarse_frame_count(duration: float) -> int:
    """Adaptive frame count for the coarse sampling stage."""
    if duration < 3.0:
        return 1
    if duration < 8.0:
        return 3
    if duration < 20.0:
        return 5
    return 6


def _coarse_positions(start: float, end: float) -> List[float]:
    """Return evenly-spaced time positions for coarse sampling.

    The centre of the shot is always included as one of the sample points.
    """
    duration = max(end - start, 0.0)
    n = _coarse_frame_count(duration)
    center = (start + end) / 2.0

    if n == 1:
        return [center]

    raw = [start + (i + 1) / (n + 1) * duration for i in range(n)]
    # Replace the position nearest to center with the exact center
    closest_idx = min(range(len(raw)), key=lambda i: abs(raw[i] - center))
    raw[closest_idx] = center
    return raw


def _refinement_positions(
    best_pos: float,
    start: float,
    end: float,
    n: int = 3,
) -> List[float]:
    """Return positions within ±0.5 s of *best_pos* for the refinement stage."""
    window = 0.5
    lo = max(start, best_pos - window)
    hi = min(end, best_pos + window)
    if hi <= lo or n <= 0:
        return []
    step = (hi - lo) / (n + 1)
    positions = [lo + step * (i + 1) for i in range(n)]
    # Drop positions that are too close to best_pos to add new information
    positions = [p for p in positions if abs(p - best_pos) > 0.05]
    return positions


# ---------------------------------------------------------------------------
# Per-shot best-frame search
# ---------------------------------------------------------------------------

def _find_best_frame_for_shot(
    video_path: str,
    description: str,
    start_time: str,
    end_time: str,
    fps: float,
    model,
    processor,
    device: str,
    tmp_dir: Path,
) -> Tuple[int, float]:
    """Two-stage CLIP frame search for a single shot.

    Returns ``(frame_number, cosine_similarity_score)``.
    Falls back to the centre frame with ``score = 0.0`` when confidence is
    below ``LOW_CONFIDENCE_THRESHOLD`` or no frames could be extracted.
    """
    start = _timecode_to_seconds(start_time)
    end = _timecode_to_seconds(end_time)
    center = (start + end) / 2.0

    # Stage 1 — coarse sampling
    coarse_positions = _coarse_positions(start, end)
    coarse_frames: List = []
    coarse_valid_positions: List[float] = []

    for pos in coarse_positions:
        img, _ = _extract_frame_pil(video_path, pos, tmp_dir)
        if img is not None:
            coarse_frames.append(img)
            coarse_valid_positions.append(pos)

    if not coarse_frames:
        return int(round(center * fps)), FALLBACK_SCORE

    coarse_scores = clip_score_batch(coarse_frames, description, model, processor, device)
    best_coarse_idx = max(range(len(coarse_scores)), key=lambda i: coarse_scores[i])
    best_pos = coarse_valid_positions[best_coarse_idx]
    best_score = float(coarse_scores[best_coarse_idx])

    # Stage 2 — refinement (±0.5 s window around best coarse frame)
    refine_positions = _refinement_positions(best_pos, start, end, n=3)
    refine_frames: List = []
    refine_valid_positions: List[float] = []

    for pos in refine_positions:
        img, _ = _extract_frame_pil(video_path, pos, tmp_dir)
        if img is not None:
            refine_frames.append(img)
            refine_valid_positions.append(pos)

    if refine_frames:
        refine_scores = clip_score_batch(refine_frames, description, model, processor, device)
        best_refine_idx = max(range(len(refine_scores)), key=lambda i: refine_scores[i])
        if float(refine_scores[best_refine_idx]) > best_score:
            best_pos = refine_valid_positions[best_refine_idx]
            best_score = float(refine_scores[best_refine_idx])

    # Fallback: low-confidence → use centre frame, score 0.0
    if best_score < LOW_CONFIDENCE_THRESHOLD:
        best_pos = center
        best_score = FALLBACK_SCORE

    return int(round(best_pos * fps)), best_score


# ---------------------------------------------------------------------------
# Query-based best-frame search (no storage)
# ---------------------------------------------------------------------------

def find_query_best_frame_for_shot(
    project_path: str,
    filename: str,
    shot_id: str,
    query: str,
    media_type: str,
    model,
    processor,
    device,
) -> Tuple[int, float]:
    """Find the best frame matching *query* in the shot identified by *shot_id*.

    Uses the two-stage CLIP search without modifying any stored data.
    Requires a stable shot_id in ``<media_id>@fSTART-fEND`` format.
    Returns ``(frame_number, cosine_similarity_score)``.
    """
    import tempfile
    from data.media_id import parse_shot_id

    video_path = Path(project_path) / "media" / "videos" / media_type / filename
    _, start_frame, end_frame = parse_shot_id(shot_id)
    fps = _get_video_fps(str(video_path))

    start_time = f"{start_frame / fps:.6f}"
    end_time   = f"{end_frame / fps:.6f}"

    with tempfile.TemporaryDirectory() as tmp:
        frame, score = _find_best_frame_for_shot(
            str(video_path),
            query,
            start_time,
            end_time,
            fps,
            model,
            processor,
            device,
            Path(tmp),
        )

    return frame, score


# ---------------------------------------------------------------------------
# Annotation JSON helpers
# ---------------------------------------------------------------------------

def _load_annotation_entries(agg_path: Path) -> List[Dict[str, Any]]:
    if not agg_path.exists():
        return []
    try:
        return json.loads(agg_path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_annotation_entries(agg_path: Path, entries: List[Dict[str, Any]]) -> None:
    agg_path.parent.mkdir(parents=True, exist_ok=True)
    agg_path.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def annotate_best_frames(
    project_path: str,
    filename: str,
    media_type: str = "movies",
    model_name: str = "clip-vit-base-patch32",
    force: bool = False,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Find the best matching frame for every annotated shot in *filename*.

    For each shot whose annotation contains a ``description`` field:
      1. Compute a hash of the description to detect changes
      2. Skip the shot when ``best_frame`` already exists and the hash matches
         (unless *force* is True)
      3. Run the two-stage CLIP search to find the best frame
      4. Extract and save the frame as a PNG
      5. Write ``best_frame`` metadata back into the annotation JSON

    Returns a summary dict with keys ``filename``, ``updated``, ``skipped``,
    and ``low_confidence`` (list of shot_ids with score == 0.0).
    """
    from data.annotate import get_annotation_json_path
    from data.shotlist import read_shotlist
    from data.metadata import get_metadata
    from data.media_id import compute_media_id, build_shot_id

    project = Path(project_path)
    agg_path = get_annotation_json_path(project_path, filename, media_type)

    if not agg_path.exists():
        raise FileNotFoundError(
            f"No annotation JSON found for '{filename}'.\n"
            f"Expected: {agg_path}\n"
            f"Run 'crossing annotate shot' first to generate annotations."
        )

    entries = _load_annotation_entries(agg_path)
    if not entries:
        return {"filename": filename, "updated": 0, "skipped": 0, "low_confidence": []}

    # Build a lookup from shot_id → shotlist row for timing data
    shots = read_shotlist(project_path, filename, media_type)
    meta_entries = get_metadata(project_path, media_type=media_type)
    meta = next((e for e in meta_entries if e.get("filename") == filename), {})
    media_id = compute_media_id(meta, media_type)

    shots_by_id: Dict[str, Dict[str, Any]] = {}
    for shot in shots:
        sf = int(shot.get("start_frame") or 0)
        ef = int(shot.get("end_frame") or 0)
        sid = build_shot_id(media_id, sf, ef)
        shots_by_id[sid] = shot

    # Resolve video file
    video_path = project / "media" / "videos" / media_type / filename
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    fps = _get_video_fps(str(video_path))

    mapping = load_mapping(project_path)

    # Load CLIP model once for the entire file
    model, processor, device = _load_clip_model(project_path, model_name)

    updated = 0
    skipped = 0
    low_confidence: List[str] = []

    with tempfile.TemporaryDirectory(prefix="crossing-bestframe-") as tmp_str:
        tmp_dir = Path(tmp_str)

        for entry in entries:
            shot_data = entry.get("shot")
            if not isinstance(shot_data, dict):
                skipped += 1
                continue

            shot_id = str(shot_data.get("shot_id", ""))
            annotation = shot_data.get("annotation")
            if not isinstance(annotation, dict):
                skipped += 1
                continue

            text = serialize_annotation_item(entry, mapping)
            if not text:
                skipped += 1
                continue

            desc_hash = compute_description_hash(text)

            # Skip when best_frame is already up to date
            existing_bf = shot_data.get("best_frame")
            if (
                not force
                and isinstance(existing_bf, dict)
                and existing_bf.get("source_description_hash") == desc_hash
            ):
                skipped += 1
                continue

            # Look up shot timing in the shotlist
            shot_info = shots_by_id.get(shot_id)
            if shot_info is None:
                if verbose:
                    print(f"  [skip] no shotlist row for shot_id={shot_id!r}")
                skipped += 1
                continue

            start_time = shot_info.get("start_time", "0:00:00.000")
            end_time = shot_info.get("end_time", "0:00:00.000")

            if verbose:
                print(f"  [{shot_id}] → {text[:70]!r}")

            try:
                best_frame_num, best_score = _find_best_frame_for_shot(
                    str(video_path),
                    text,
                    start_time,
                    end_time,
                    fps,
                    model,
                    processor,
                    device,
                    tmp_dir,
                )
            except Exception as exc:
                if verbose:
                    print(f"  [error] {shot_id}: {exc}")
                skipped += 1
                continue

            # Extract and persist the final PNG
            out_path = best_frame_path(project_path, media_type, filename, shot_id)
            out_path.parent.mkdir(parents=True, exist_ok=True)

            best_pos = best_frame_num / fps if fps > 0 else 0.0
            png_ok = _extract_frame_png(str(video_path), best_pos, out_path)
            if not png_ok and verbose:
                print(f"  [warn] could not save PNG for {shot_id!r}")

            # Update the in-memory annotation entry (at the shot level, not inside annotation)
            shot_data["best_frame"] = {
                "frame": best_frame_num,
                "score": round(float(best_score), 6),
                "method": "clip",
                "source_description_hash": desc_hash,
                "computed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }

            if best_score == FALLBACK_SCORE:
                low_confidence.append(shot_id)
                if verbose:
                    print(
                        f"  [low confidence] {shot_id}: score={best_score:.4f}"
                        f" — centre frame used"
                    )

            updated += 1

    # Persist the updated annotation JSON
    if updated > 0:
        _save_annotation_entries(agg_path, entries)

    return {
        "filename": filename,
        "updated": updated,
        "skipped": skipped,
        "low_confidence": low_confidence,
    }
