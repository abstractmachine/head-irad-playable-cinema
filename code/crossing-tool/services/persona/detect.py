"""Persona detection pipeline for a single movie.

Pipeline overview
-----------------
1. Resolve movie filename and load its shotlist.
2. For each shot, sample ``frames_per_shot`` representative frames.
3. Run YOLO person detection on each frame.
4. Extract OSNet re-ID embeddings for each detected person crop.
5. Deduplicate similar detections within each shot (same person seen in
   multiple sampled frames → keep one representative).
6. Cluster all (shot_id, embedding) pairs across the whole movie using
   agglomerative hierarchical clustering with complete linkage and a
   cosine-distance threshold.
7. Filter out singletons (personas that appear in only one shot).
8. Detect ambiguous cluster pairs (clusters whose inter-centroid distance
   is below the relaxed threshold).
9. Assemble and return a PersonaDocument.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Optional

import numpy as np

from .embeddings import extract_embedding
from .io import get_persona_json_path, now_iso
from .models import (
    DetectorInfo,
    MovieInfo,
    PersonaAppearance,
    PersonaCluster,
    PersonaDocument,
    SourceInfo,
)

_PIPELINE_VERSION = "0.2.0"
_METHOD_LABEL = "YOLO person detect + torchreid OSNet x1.0 + tracklet-aware adaptive clustering"

# ─── defaults ────────────────────────────────────────────────────────────────

_DEFAULT_FRAMES_PER_SHOT = 2
_DEFAULT_MIN_PERSON_CONF = 0.25      # YOLO confidence threshold
_DEFAULT_CLUSTER_THRESHOLD = 0.45   # cosine distance; lower = stricter
_DEFAULT_LINKAGE          = "average"  # average linkage chains appearances across lighting/angle
_DEFAULT_MIN_SHOTS = 3              # minimum shots for a persona to be recorded
_INTRA_SHOT_MERGE_DIST = 0.20       # cosine distance to merge duplicates
_AMBIGUITY_SLACK = 1.5              # inter-cluster dist < threshold * slack → ambiguous
_YOLO_MODEL_NAME = "yolov8n.pt"
_MIN_PERSON_ASPECT = 0.6            # min bbox height/width; filters torso/ground false positives
_MAX_BBOX_AREA_FRAC  = 0.40         # reject if bbox covers >40% of frame area (whole-scene detections)
_MAX_BBOX_WIDTH_FRAC = 0.65         # reject if bbox spans >65% of frame width (crowd/multi-person shots)
_MIN_BBOX_HEIGHT_FRAC = 0.15        # reject crops shorter than 15% of frame height (tiny partials)
_MAX_CANDIDATES_PER_FRAME = 2       # keep only the top-N highest-confidence crops per sampled frame
_MAX_AMBIGUOUS_WITH = 5             # cap ambiguous_with list to avoid JSON explosion


# Tracklet & adaptive matching defaults
_TRACKLET_IOU_THRESHOLD = 0.30     # IoU to link detections into a tracklet
_TRACKLET_EMB_MATCH_THRESHOLD = 0.35  # embedding distance to link into a tracklet
_MIN_TRACKLET_SUPPORT = 1           # min member detections to form a tracklet (1 = allow singletons)
_ADAPTIVE_SCALE = 0.5               # scale for per-track adaptive threshold from spread
_MAX_REJECTED_DEBUG = 20            # how many rejected tracklets to store for debug


# ─── frame extraction ─────────────────────────────────────────────────────────

def _sample_frame_indices(start_f: int, end_f: int, n: int) -> list[int]:
    """Return n interior-weighted frame indices within [start_f, end_f].

    Samples are placed well away from the shot boundaries to avoid blurry
    or transitional frames that are common near cuts.

    n=1 → 50%
    n=2 → 35%, 65%
    n=3 → 25%, 50%, 75%
    n>3 → evenly spaced interior points (avoiding start/end)
    """
    total = max(0, end_f - start_f)
    if total == 0 or n <= 0:
        return [start_f]
    if n == 1:
        return [start_f + total // 2]
    if n == 2:
        return [int(start_f + total * 0.35), int(start_f + total * 0.65)]
    if n == 3:
        return [
            int(start_f + total * 0.25),
            int(start_f + total * 0.50),
            int(start_f + total * 0.75),
        ]
    # General interior case: evenly spaced, excluding endpoints
    step = total / (n + 1)
    return [int(start_f + step * (i + 1)) for i in range(n)]


def _read_frame(cap, frame_index: int) -> Optional[np.ndarray]:
    """Seek to frame_index and return BGR frame, or None on failure."""
    import cv2
    cap.set(cv2.CAP_PROP_POS_FRAMES, float(frame_index))
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    return frame


# ─── face presence filter ─────────────────────────────────────────────────────

_HAAR_FRONTAL: object = None
_HAAR_PROFILE: object = None


def _load_face_cascades():
    """Lazily load OpenCV Haar cascades for frontal and profile faces.

    These XML files ship with every cv2 installation — no extra download.
    """
    global _HAAR_FRONTAL, _HAAR_PROFILE
    if _HAAR_FRONTAL is None:
        import cv2
        base = cv2.data.haarcascades
        _HAAR_FRONTAL = cv2.CascadeClassifier(base + "haarcascade_frontalface_alt2.xml")
        _HAAR_PROFILE = cv2.CascadeClassifier(base + "haarcascade_profileface.xml")
    return _HAAR_FRONTAL, _HAAR_PROFILE


def _has_face(crop_bgr: np.ndarray) -> bool:
    """Return True if at least one face (frontal or profile) is detectable.

    Uses OpenCV Haar cascades with permissive settings (minNeighbors=2) to
    keep false-negative rate low on old film stock and low-light frames.
    Returns False for backs-of-heads, silhouettes, animals, and body-part
    crops — exactly what we want to reject before embedding.
    """
    import cv2
    frontal, profile = _load_face_cascades()
    h, w = crop_bgr.shape[:2]
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    # Minimum face size: 1/5 of the shorter crop dimension, at least 20px
    min_face = max(20, min(h, w) // 5)
    faces = frontal.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=2, minSize=(min_face, min_face)
    )
    if len(faces) > 0:
        return True
    faces = profile.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=2, minSize=(min_face, min_face)
    )
    return len(faces) > 0


# ─── person detection ─────────────────────────────────────────────────────────

_YOLO_INSTANCE = None
_YOLO_MODEL_PATH: Optional[str] = None


def _get_yolo():
    """Lazily load the YOLO model (downloaded on first use, ~6 MB)."""
    global _YOLO_INSTANCE
    if _YOLO_INSTANCE is None:
        from ultralytics import YOLO
        model_arg = _YOLO_MODEL_PATH or _YOLO_MODEL_NAME
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _YOLO_INSTANCE = YOLO(str(model_arg))
    return _YOLO_INSTANCE


def _detect_persons(frame_bgr: np.ndarray, min_conf: float) -> list[dict]:
    """Run YOLO on a frame and return person detections.

    Returns a list of dicts, each with:
        bbox        [x1, y1, x2, y2]  (clipped to frame bounds)
        confidence  float
    """
    model = _get_yolo()
    h, w = frame_bgr.shape[:2]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        results = model(
            frame_bgr,
            classes=[0],          # class 0 = person
            conf=min_conf,
            verbose=False,
            stream=False,
        )

    detections = []
    for r in results:
        if r.boxes is None:
            continue
        for box in r.boxes:
            conf = float(box.conf[0])
            x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
            # Clip to frame bounds
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 > x1 and y2 > y1:
                detections.append({"bbox": [x1, y1, x2, y2], "confidence": conf})

    return detections


# ─── visibility heuristic ─────────────────────────────────────────────────────

def _classify_visibility(bbox: list[int], frame_h: int) -> str:
    """Classify how prominent the person is in the frame."""
    person_h = bbox[3] - bbox[1]
    ratio = person_h / max(frame_h, 1)
    if ratio > 0.45:
        return "foreground"
    if ratio > 0.25:
        return "mid"
    return "background"


# ─── intra-shot deduplication ─────────────────────────────────────────────────

def _cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine distance in [0, 2]; 0 = identical, 2 = opposite."""
    dot = float(np.dot(a, b))
    # embeddings are already L2-normalised so cosine_sim = dot product
    return 1.0 - dot


def _merge_intra_shot_detections(
    candidates: list[dict],
    merge_threshold: float,
) -> list[dict]:
    """Greedily merge detections within a shot that look like the same person.

    Each candidate dict expected to have 'embedding' and 'appearance' keys.
    Returns a deduplicated list (one representative per apparent person).
    """
    if not candidates:
        return []

    merged = [candidates[0]]
    for cand in candidates[1:]:
        emb = cand["embedding"]
        already_covered = False
        for m in merged:
            if _cosine_distance(emb, m["embedding"]) < merge_threshold:
                # Choose the higher-confidence representative
                if cand["appearance"].confidence > m["appearance"].confidence:
                    m["embedding"] = emb
                    m["appearance"] = cand["appearance"]
                already_covered = True
                break
        if not already_covered:
            merged.append(cand)

    return merged


# ─── tracklet building & quality heuristics ─────────────────────────────────


def _iou(a: list[int], b: list[int]) -> float:
    """Intersection-over-union for two bboxes [x1,y1,x2,y2]."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    iw = max(0, inter_x2 - inter_x1)
    ih = max(0, inter_y2 - inter_y1)
    inter = iw * ih
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return float(inter) / union if union > 0 else 0.0


def _summarize_tracklet(members: list[dict]) -> dict:
    """Compute summary stats for a tracklet from its member detections.

    members: list of dicts with keys 'embedding'(np.array), 'appearance'(PersonaAppearance), 'frame_size' (fh,fw)
    Returns a metadata dict with mean embedding (L2), spread, representative appearance, support, etc.
    """
    embs = np.vstack([m["embedding"] for m in members])
    # centroid and normalise
    centroid = embs.mean(axis=0)
    norm = float(np.linalg.norm(centroid))
    if norm > 1e-6:
        centroid = centroid / norm
    # spread: mean cosine distance from centroid
    sims = embs @ centroid
    dists = 1.0 - sims
    spread = float(np.mean(dists)) if dists.size > 0 else 0.0

    # choose representative appearance (highest confidence)
    best = max(members, key=lambda x: x["appearance"].confidence)
    avg_conf = float(np.mean([m["appearance"].confidence for m in members]))
    vis_counts = {}
    for m in members:
        v = m["appearance"].visibility
        vis_counts[v] = vis_counts.get(v, 0) + 1
    visibility = max(vis_counts.items(), key=lambda x: x[1])[0]

    return {
        "mean_embedding": centroid,
        "emb_spread": spread,
        "rep_appearance": best["appearance"],
        "support": len(members),
        "avg_confidence": avg_conf,
        "visibility": visibility,
        "members": members,
    }


def _is_tracklet_high_quality(meta: dict) -> tuple[bool, str]:
    """Heuristic accept/reject for a tracklet metadata dict.

    Returns (accepted, reason).
    """
    support = meta.get("support", 0)
    avg_conf = meta.get("avg_confidence", 0.0)
    spread = meta.get("emb_spread", 1.0)

    # Reject obvious bad spread or low confidence
    if support < 1:
        return False, "no_support"
    # Large spread -> low consistency
    if spread > 0.6:
        return False, "high_spread"
    # Low confidence
    if support == 1 and avg_conf < 0.6:
        return False, "singleton_low_conf"
    if avg_conf < 0.25:
        return False, "low_confidence"

    return True, "ok"


def _build_tracklets(
    shot_candidates: list[dict],
    shot_id: int,
    iou_threshold: float = _TRACKLET_IOU_THRESHOLD,
    emb_threshold: float = _TRACKLET_EMB_MATCH_THRESHOLD,
    min_support: int = _MIN_TRACKLET_SUPPORT,
) -> tuple[list[dict], list[dict]]:
    """Link detections within a shot into lightweight tracklets.

    Returns (accepted_tracklets_meta, rejected_tracklets_meta).
    Each tracklet_meta contains keys from _summarize_tracklet plus 'shot_id'.
    """
    if not shot_candidates:
        return [], []

    # sort by frame index
    shot_candidates = sorted(shot_candidates, key=lambda c: c["appearance"].frame_index)

    tracklets: list[dict] = []

    for cand in shot_candidates:
        emb = cand["embedding"]
        app = cand["appearance"]
        bbox = app.bbox

        # try to match to an existing tracklet (greedy)
        best_idx = None
        best_score = 0.0

        for i, t in enumerate(tracklets):
            last = t["members"][-1]
            last_bbox = last["appearance"].bbox
            last_emb = last["embedding"]

            iou_v = _iou(bbox, last_bbox)
            emb_dist = _cosine_distance(emb, t.get("mean_emb", last_emb))

            # Prefer robust spatial matches, fallback to embedding similarity
            if iou_v >= iou_threshold and iou_v > best_score:
                best_idx = i
                best_score = iou_v
            elif emb_dist <= emb_threshold and best_idx is None:
                # take first embedding match if no spatial winner
                best_idx = i

        if best_idx is None:
            # create new tracklet
            tracklets.append({"members": [cand], "mean_emb": emb})
        else:
            tracklets[best_idx]["members"].append(cand)
            # update mean_emb lazily; recompute when summarising

    accepted = []
    rejected = []

    for t in tracklets:
        meta = _summarize_tracklet(t["members"])
        meta["shot_id"] = shot_id
        accepted_flag, reason = _is_tracklet_high_quality(meta)
        meta["quality_reason"] = reason
        if accepted_flag and meta.get("support", 0) >= min_support:
            accepted.append(meta)
        else:
            rejected.append(meta)

    return accepted, rejected


# ─── main pipeline ─────────────────────────────────────────────────────────────

def detect_personas(
    project_path: str,
    filename: str,
    media_type: str = "movies",
    frames_per_shot: int = _DEFAULT_FRAMES_PER_SHOT,
    min_person_confidence: float = _DEFAULT_MIN_PERSON_CONF,
    cluster_threshold: float = _DEFAULT_CLUSTER_THRESHOLD,
    min_shots_to_be_persona: int = _DEFAULT_MIN_SHOTS,
    linkage: str = _DEFAULT_LINKAGE,
    include_background: bool = False,
    require_face: bool = True,
    verbose: bool = False,
) -> PersonaDocument:
    """Run the full persona detection pipeline for one movie.

    Args:
        project_path:           Project root directory.
        filename:               Video filename (e.g. "Movie (1968) {tmdb-40575}.mp4").
        media_type:             'movies' or 'gameplay'.
        frames_per_shot:        Number of frames to sample per shot (1–3; default 2).
        min_person_confidence:  YOLO minimum detection confidence (default 0.25).
        cluster_threshold:      Maximum cosine distance to merge detections into
                                the same persona (default 0.35, conservative).
        min_shots_to_be_persona: Detections that appear in fewer shots than this
                                 threshold are excluded (default 2).
        linkage:                Scipy linkage method: 'complete' (default, conservative)
                                or 'average' (more permissive).
        include_background:     If False (default), skip background-visibility detections
                                before embedding to reduce noise in clustering.
        require_face:           If True (default), discard crops with no detectable face
                                (frontal or profile).  Eliminates backs-of-heads,
                                silhouettes, and animal false-positives.
        verbose:                Print per-shot progress.

    Returns:
        A PersonaDocument ready to be serialised.

    Raises:
        FileNotFoundError: if the video or shotlist cannot be found.
    """
    import cv2
    from services.shotlist import get_shotlist_path, read_shotlist
    from services.metadata import get_metadata

    # ── 1. Load movie metadata ──────────────────────────────────────────────
    entries = get_metadata(project_path, media_type=media_type)
    meta = next(
        (e for e in entries if e.get("filename") == filename),
        None,
    )
    if meta is None:
        raise FileNotFoundError(
            f"No metadata entry for '{filename}' in {media_type}."
        )

    video_path = Path(project_path) / "media" / "videos" / media_type / filename
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    shotlist_path = get_shotlist_path(project_path, filename, media_type)
    if not shotlist_path.exists():
        raise FileNotFoundError(
            f"Shotlist not found: {shotlist_path}\n"
            f"  Run: crossing shotlist shot detect --tmdb {meta.get('tmdb')}"
        )

    shots = read_shotlist(project_path, filename, media_type)
    if not shots:
        raise ValueError(f"Shotlist is empty: {shotlist_path}")

    tmdb_id = int(meta.get("tmdb", 0))
    title = meta.get("title", Path(filename).stem)
    year_raw = meta.get("year")
    year = int(year_raw) if year_raw else None

    print(f"  {title} ({year or '?'})  —  {len(shots)} shots")

    # ── 1b. Pre-warm embedding model (fail fast before we open the video) ───
    try:
        from .embeddings import warmup as _warmup_embedder
        _warmup_embedder()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load OSNet embedding model: {exc}\n"
            "  Try: pip install torchreid gdown tensorboard"
        ) from exc

    # Prefer a local YOLO model in project_path/models/yolov8n.pt if present
    try:
        global _YOLO_MODEL_PATH
        candidate = Path(project_path) / "models" / _YOLO_MODEL_NAME
        if candidate.exists():
            _YOLO_MODEL_PATH = str(candidate)
    except Exception:
        # non-fatal: fallback to default behavior in _get_yolo
        pass

    # ── 2. Open video ────────────────────────────────────────────────────────
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0

    # ── 3. Process shots ─────────────────────────────────────────────────────
    # all_detections: list of {shot_id, embedding, appearance}
    all_detections: list[dict] = []

    # detailed counters
    n_frames_sampled    = 0
    n_raw_detections    = 0
    n_bg_filtered       = 0
    n_size_filtered     = 0
    n_topk_filtered     = 0
    n_face_filtered     = 0
    n_embed_failures    = 0
    n_kept_detections   = 0
    n_tracklets_built   = 0
    n_tracklets_rejected = 0
    skipped_frames      = 0
    shots_with_persons  = 0
    rejected_tracklet_samples: list[dict] = []

    for shot_idx, shot in enumerate(shots):
        shot_id = shot_idx + 1  # 1-based shot number

        # Skip shots marked Ignore
        if shot.get("Ignore", "").strip().lower() in ("1", "true", "yes"):
            continue

        # Resolve frame range
        try:
            start_f = int(shot.get("start_frame") or 0)
            end_f = int(shot.get("end_frame") or start_f)
        except (ValueError, TypeError):
            # Fall back to timing
            try:
                start_f = _time_to_frame(shot.get("start_time", ""), fps)
                end_f = _time_to_frame(shot.get("end_time", ""), fps)
            except Exception:
                start_f = end_f = 0

        frame_indices = _sample_frame_indices(start_f, end_f, frames_per_shot)

        shot_candidates: list[dict] = []

        for fi in frame_indices:
            frame = _read_frame(cap, fi)
            if frame is None:
                skipped_frames += 1
                continue

            n_frames_sampled += 1
            fh, fw = frame.shape[:2]
            frame_area = fh * fw
            persons = _detect_persons(frame, min_person_confidence)
            n_raw_detections += len(persons)

            # Pre-filter candidates before embedding — only score, visibility,
            # and geometry checked here (cheap, before any model inference).
            frame_candidates: list[dict] = []

            for det in persons:
                x1, y1, x2, y2 = det["bbox"]
                bbox_h = y2 - y1
                bbox_w = x2 - x1
                vis = _classify_visibility(det["bbox"], fh)

                # 1. Background filter (cheap)
                if not include_background and vis == "background":
                    n_bg_filtered += 1
                    continue

                # 2. Geometric sanity filters — reject non-person proposals
                bbox_area = bbox_h * bbox_w
                if (
                    # Too small to be a portrait crop
                    bbox_h < fh * _MIN_BBOX_HEIGHT_FRAC
                    # Implausible aspect ratio (horizontal / torso / ground)
                    or (bbox_w > 0 and bbox_h / bbox_w < _MIN_PERSON_ASPECT)
                    # Whole-scene capture — bbox covers too much of the frame
                    or (frame_area > 0 and bbox_area / frame_area > _MAX_BBOX_AREA_FRAC)
                    # Too wide relative to frame (crowd shot / multi-person)
                    or (fw > 0 and bbox_w / fw > _MAX_BBOX_WIDTH_FRAC)
                ):
                    n_size_filtered += 1
                    continue

                frame_candidates.append({"det": det, "vis": vis})

            # 3. Top-k per frame: keep only the highest-confidence candidates
            frame_candidates.sort(key=lambda c: c["det"]["confidence"], reverse=True)
            if len(frame_candidates) > _MAX_CANDIDATES_PER_FRAME:
                n_topk_filtered += len(frame_candidates) - _MAX_CANDIDATES_PER_FRAME
                frame_candidates = frame_candidates[:_MAX_CANDIDATES_PER_FRAME]

            # 4. Extract embeddings for the survivors
            for cand in frame_candidates:
                det = cand["det"]
                vis = cand["vis"]
                x1, y1, x2, y2 = det["bbox"]
                crop = frame[y1:y2, x1:x2]

                # 5. Face presence filter — reject backs-of-heads, silhouettes,
                #    animals, and body-part crops before running the heavy model.
                if require_face and not _has_face(crop):
                    n_face_filtered += 1
                    continue

                result = extract_embedding(crop)
                if result is None:
                    n_embed_failures += 1
                    continue

                emb, norm = result
                n_kept_detections += 1

                appearance = PersonaAppearance(
                    shot_id=shot_id,
                    confidence=round(det["confidence"], 4),
                    visibility=vis,
                    bbox=det["bbox"],
                    frame_index=fi,
                    embedding_norm=round(float(norm), 4),
                )
                shot_candidates.append({
                    "embedding": emb,
                    "appearance": appearance,
                    "frame_size": (fh, fw),
                })

        # Build local tracklets (link detections across sampled frames)
        accepted_tracklets, rejected_tracklets = _build_tracklets(
            shot_candidates, shot_id,
            iou_threshold=_TRACKLET_IOU_THRESHOLD,
            emb_threshold=_TRACKLET_EMB_MATCH_THRESHOLD,
            min_support=_MIN_TRACKLET_SUPPORT,
        )

        if accepted_tracklets:
            shots_with_persons += 1
            for t in accepted_tracklets:
                all_detections.append({
                    "embedding": t["mean_embedding"],
                    "appearance": t["rep_appearance"],
                    "tracklet_meta": t,
                })
            n_tracklets_built += len(accepted_tracklets)

        if rejected_tracklets:
            n_tracklets_rejected += len(rejected_tracklets)
            # collect small debug samples
            for t in rejected_tracklets:
                if len(rejected_tracklet_samples) >= _MAX_REJECTED_DEBUG:
                    break
                ra = t.get("rep_appearance")
                rejected_tracklet_samples.append({
                    "shot_id": t.get("shot_id"),
                    "support": int(t.get("support", 0)),
                    "avg_confidence": round(float(t.get("avg_confidence", 0.0)), 4),
                    "emb_spread": round(float(t.get("emb_spread", 0.0)), 4),
                    "rep_bbox": ra.bbox if ra is not None else None,
                    "rep_frame": ra.frame_index if ra is not None else None,
                    "reason": t.get("quality_reason"),
                })

        if verbose:
            print(
                f"    shot {shot_id:4d}/{len(shots)}: "
                f"{len(accepted_tracklets)} tracklet(s), {len(rejected_tracklets)} rejected"
            )

    cap.release()

    print(
        f"  frames sampled: {n_frames_sampled}  |  skipped: {skipped_frames}\n"
        f"  raw detections: {n_raw_detections}  |  bg: -{n_bg_filtered}  size: -{n_size_filtered}  top-k: -{n_topk_filtered}  no-face: -{n_face_filtered}\n"
        f"  embed failures: {n_embed_failures}  |  kept: {n_kept_detections}\n"
        f"  tracklets built: {n_tracklets_built}  |  tracklets rejected: {n_tracklets_rejected}\n"
        f"  shots with persons: {shots_with_persons}/{len(shots)}"
    )

    # ── 4. Build persona document ────────────────────────────────────────────
    detector_info = DetectorInfo(
        version=_PIPELINE_VERSION,
        method=_METHOD_LABEL,
        generated_at=now_iso(),
        frames_per_shot=frames_per_shot,
        cluster_threshold=cluster_threshold,
        min_shots_to_be_persona=min_shots_to_be_persona,
    )
    movie_info = MovieInfo(
        tmdb_id=tmdb_id,
        title=title,
        year=year,
        filename=filename,
    )
    source_info = SourceInfo(
        video_path=str(
            Path("media") / "videos" / media_type / filename
        ),
        shotlist_path=str(
            Path("data") / "shotlists" / media_type / shotlist_path.name
        ),
    )

    if not all_detections:
        print("  No person detections found — writing empty personas list.")
        return PersonaDocument(
            movie=movie_info,
            source=source_info,
            detector=detector_info,
            personas=[],
            stats={
                "total_shots": len(shots),
                "frames_sampled": n_frames_sampled,
                "frames_skipped": skipped_frames,
                "raw_detections": n_raw_detections,
                "background_filtered": n_bg_filtered,
                "size_filtered": n_size_filtered,
                "topk_filtered": n_topk_filtered,
                "face_filtered": n_face_filtered,
                "embed_failures": n_embed_failures,
                "kept_detections": n_kept_detections,
                "tracklets_built": n_tracklets_built,
                "tracklets_rejected": n_tracklets_rejected,
                "shots_with_persons": shots_with_persons,
                "personas_found": 0,
                "note": "No person detections were found in the sampled frames.",
            },
        )

    # ── 5. Global clustering ──────────────────────────────────────────────────
    personas, n_clusters_raw = _cluster_detections(
        all_detections,
        cluster_threshold=cluster_threshold,
        min_shots=min_shots_to_be_persona,
        linkage_method=linkage,
    )

    print(
        f"  clusters raw: {n_clusters_raw}  |  after min-shots filter: {len(personas)}"
    )

    stats = {
        "total_shots": len(shots),
        "frames_sampled": n_frames_sampled,
        "frames_skipped": skipped_frames,
        "raw_detections": n_raw_detections,
        "background_filtered": n_bg_filtered,
        "size_filtered": n_size_filtered,
        "topk_filtered": n_topk_filtered,
        "face_filtered": n_face_filtered,
        "embed_failures": n_embed_failures,
        "kept_detections": n_kept_detections,
        "tracklets_built": n_tracklets_built,
        "tracklets_rejected": n_tracklets_rejected,
        "shots_with_persons": shots_with_persons,
        "clusters_raw": n_clusters_raw,
        "personas_found": len(personas),
        "rejected_tracklet_samples": rejected_tracklet_samples,
    }

    return PersonaDocument(
        movie=movie_info,
        source=source_info,
        detector=detector_info,
        personas=personas,
        stats=stats,
    )


# ─── clustering ───────────────────────────────────────────────────────────────

def _cluster_detections(
    detections: list[dict],
    cluster_threshold: float,
    min_shots: int,
    linkage_method: str = _DEFAULT_LINKAGE,
) -> tuple[list[PersonaCluster], int]:
    """Cluster person detections into anonymous persona groups.

    Uses agglomerative hierarchical clustering with the specified linkage
    method and cosine distance.

    'complete' linkage (default) is conservative: two clusters merge only
    when ALL pairwise distances are within the threshold — less over-merging.

    'average' linkage is more permissive: merges when the mean inter-cluster
    distance is within the threshold — better recall, more false merges.

    Args:
        detections:        Each element has 'embedding' (L2-normed) and
                           'appearance' (PersonaAppearance).
        cluster_threshold: Maximum cosine distance to group into one cluster.
        min_shots:         Minimum number of distinct shots for a valid persona.
        linkage_method:    Scipy linkage method ('complete' or 'average').

    Returns:
        (sorted list of PersonaCluster objects, raw cluster count before filter)
    """
    # New pipeline: coarse adaptive matching to form connected components
    # then hierarchical clustering inside each connected component.
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import pdist, squareform
    from collections import defaultdict

    n = len(detections)
    if n == 0:
        return [], 0

    embeddings = np.vstack([d["embedding"] for d in detections])  # (N, D)

    # Per-tracklet spread if available (fallback to 0)
    spreads = np.array([d.get("tracklet_meta", {}).get("emb_spread", 0.0) for d in detections])
    # Adaptive per-tracklet tolerance
    t_i = cluster_threshold + spreads * _ADAPTIVE_SCALE
    t_i = np.clip(t_i, 0.01, 1.0)

    # pairwise distances
    dist_vec = pdist(embeddings, metric="cosine")
    dist_vec = np.clip(dist_vec, 0.0, 2.0)
    dist_mat = squareform(dist_vec)

    # adjacency if distance <= min(t_i, t_j)
    tol_matrix = np.minimum.outer(t_i, t_i)
    adjacency = dist_mat <= tol_matrix

    # connected components on adjacency graph
    visited = set()
    components: list[list[int]] = []
    for i in range(n):
        if i in visited:
            continue
        stack = [i]
        comp = []
        while stack:
            u = stack.pop()
            if u in visited:
                continue
            visited.add(u)
            comp.append(u)
            neigh = list(np.nonzero(adjacency[u])[0])
            for v in neigh:
                if v not in visited:
                    stack.append(v)
        components.append(sorted(comp))

    n_clusters_raw = len(components)

    # Within each connected component, do hierarchical clustering to split finer groups
    candidate_groups: list[list[int]] = []
    for comp in components:
        if len(comp) == 1:
            candidate_groups.append(comp)
            continue
        sub_emb = embeddings[comp]
        if len(sub_emb) == 1:
            candidate_groups.append([comp[0]])
            continue
        sub_dist = pdist(sub_emb, metric="cosine")
        sub_dist = np.clip(sub_dist, 0.0, 2.0)
        Z = linkage(sub_dist, method=linkage_method)
        sub_labels = fcluster(Z, t=cluster_threshold, criterion="distance")
        groups: dict[int, list[int]] = defaultdict(list)
        for idx, lbl in enumerate(sub_labels):
            groups[int(lbl)].append(comp[idx])
        for g in groups.values():
            candidate_groups.append(sorted(g))

    # Filter candidate groups by min_shots
    valid_groups: list[list[int]] = []
    for group in candidate_groups:
        shot_set = {detections[i]["appearance"].shot_id for i in group}
        if len(shot_set) >= min_shots:
            valid_groups.append(group)

    if not valid_groups:
        return [], n_clusters_raw

    # Compute centroids for ambiguity detection and build PersonaClusters
    centroids: list[np.ndarray] = []
    for group in valid_groups:
        embs = np.vstack([detections[i]["embedding"] for i in group])
        centroid = embs.mean(axis=0)
        norm = np.linalg.norm(centroid)
        centroids.append(centroid / norm if norm > 1e-6 else centroid)

    # Sort groups by first_shot for stable numbering
    def _first_shot_idx(group):
        return min(detections[i]["appearance"].shot_id for i in group)

    order = sorted(range(len(valid_groups)), key=lambda i: _first_shot_idx(valid_groups[i]))

    persona_list: list[PersonaCluster] = []
    for rank, gi in enumerate(order):
        group = valid_groups[gi]
        persona_id = f"p_{rank + 1:03d}"
        members = [detections[i] for i in group]
        shot_set = {m["appearance"].shot_id for m in members}
        shots_sorted = sorted(shot_set)

        # intra-cluster confidence: mean pairwise cosine similarity
        embs = np.vstack([m["embedding"] for m in members])
        if len(embs) == 1:
            intra_conf = 1.0
        else:
            pairwise_sim = embs @ embs.T
            n_e = len(embs)
            upper = [pairwise_sim[i, j] for i in range(n_e) for j in range(i + 1, n_e)]
            intra_conf = float(np.mean(upper)) if upper else 1.0

        # representative appearance per shot: highest-confidence detection
        shot_to_appearance: dict[int, PersonaAppearance] = {}
        for m in members:
            a = m["appearance"]
            if a.shot_id not in shot_to_appearance or a.confidence > shot_to_appearance[a.shot_id].confidence:
                shot_to_appearance[a.shot_id] = a

        appearances = [shot_to_appearance[s] for s in shots_sorted]

        persona_list.append(PersonaCluster(
            persona_id=persona_id,
            label=None,
            shots=shots_sorted,
            shot_count=len(shot_set),
            first_shot=shots_sorted[0],
            last_shot=shots_sorted[-1],
            cluster_confidence=round(intra_conf, 4),
            ambiguous_with=[],  # resolved below
            appearances=appearances,
            notes=None,
        ))

    # Ambiguity: compute distances between persona centroids and record top-k ambiguous candidates
    persona_centroids = [centroids[gi] for gi in order]
    ambiguity_threshold = cluster_threshold * _AMBIGUITY_SLACK
    for i, p in enumerate(persona_list):
        # compute distances to other persona centroids
        dists = []
        for j, c in enumerate(persona_centroids):
            if i == j:
                continue
            dist = _cosine_distance(persona_centroids[i], c)
            if cluster_threshold < dist <= ambiguity_threshold:
                dists.append((dist, f"p_{j + 1:03d}"))
        dists.sort()
        p.ambiguous_with = [pid for _, pid in dists[:3]]

    return persona_list, n_clusters_raw


# ─── helpers ──────────────────────────────────────────────────────────────────

def _time_to_frame(time_str: str, fps: float) -> int:
    """Convert HH:MM:SS.mmm to a frame index."""
    if not time_str or not time_str.strip():
        return 0
    parts = time_str.strip().split(":")
    try:
        h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
        seconds = h * 3600 + m * 60 + s
        return int(seconds * fps)
    except Exception:
        return 0


# ─── batch helper ────────────────────────────────────────────────────────────

def detect_personas_for_all(
    project_path: str,
    media_type: str = "movies",
    force: bool = False,
    frames_per_shot: int = _DEFAULT_FRAMES_PER_SHOT,
    min_person_confidence: float = _DEFAULT_MIN_PERSON_CONF,
    cluster_threshold: float = _DEFAULT_CLUSTER_THRESHOLD,
    min_shots_to_be_persona: int = _DEFAULT_MIN_SHOTS,
    linkage: str = _DEFAULT_LINKAGE,
    include_background: bool = False,
    require_face: bool = True,
    verbose: bool = False,
) -> list[tuple[str, Optional[Path], Optional[str]]]:
    """Run persona detection for every film that has a shotlist.

    Returns a list of (filename, output_path_or_None, error_or_None).
    """
    from services.metadata import get_metadata
    from services.shotlist import get_shotlist_path
    from .io import write_persona_json

    entries = get_metadata(project_path, media_type=media_type)
    results: list[tuple[str, Optional[Path], Optional[str]]] = []

    pending = []
    for entry in entries:
        fn = entry.get("filename")
        if not fn:
            continue
        if not get_shotlist_path(project_path, fn, media_type).exists():
            continue
        out = get_persona_json_path(project_path, fn, media_type)
        if out.exists() and not force:
            results.append((fn, None, "skipped (already exists)"))
            continue
        video_p = Path(project_path) / "media" / "videos" / media_type / fn
        if not video_p.exists():
            results.append((fn, None, "video not found"))
            continue
        pending.append(fn)

    for i, fn in enumerate(pending, 1):
        print(f"[{i}/{len(pending)}] {fn}")
        try:
            doc = detect_personas(
                project_path,
                fn,
                media_type=media_type,
                frames_per_shot=frames_per_shot,
                min_person_confidence=min_person_confidence,
                cluster_threshold=cluster_threshold,
                min_shots_to_be_persona=min_shots_to_be_persona,
                linkage=linkage,
                include_background=include_background,
                require_face=require_face,
                verbose=verbose,
            )
            out = write_persona_json(doc, project_path, fn, media_type, force=force)
            results.append((fn, out, None))
        except FileExistsError as exc:
            results.append((fn, None, str(exc)))
        except Exception as exc:
            import traceback
            traceback.print_exc()
            results.append((fn, None, f"error: {exc}"))

    return results
