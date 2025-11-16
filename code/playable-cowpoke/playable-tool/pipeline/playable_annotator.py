import os
import json
import time
from datetime import datetime
from typing import List, Dict, Optional
import cv2
from jsonschema import validate, ValidationError
import subprocess  # added
import shutil      # added
import functools

from pipeline.ollama_client import OllamaClient

_schema_cache: Optional[dict] = None

# Hard schema for validation (matches annotation.schema.json structure)
VALIDATION_SCHEMA = {
    "type": "object",
    "required": ["Setting", "Protagonists", "Place", "Actions", "Objects", "Props", "Environment", "Architecture"],
    "properties": {
        "Setting": {"type": "string"},
        "Protagonists": {"type": "array", "items": {"type": "string"}},
        "Place": {"type": "array", "items": {"type": "string"}},
        "Actions": {"type": "array", "items": {"type": "string"}},
        "Objects": {"type": "array", "items": {"type": "string"}},
        "Props": {"type": "array", "items": {"type": "string"}},
        "Environment": {"type": "array", "items": {"type": "string"}},
        "Architecture": {"type": "array", "items": {"type": "string"}}
    },
    "additionalProperties": False
}

def _extract_and_validate_json(text: str, max_tries: int = 1) -> Optional[dict]:
    """
    Extract first {...} from text, validate against schema.
    Returns dict or None.
    """
    if not text:
        return None
    
    # Try raw parse first
    try:
        data = json.loads(text.strip())
        validate(instance=data, schema=VALIDATION_SCHEMA)
        return data
    except Exception:
        pass
    
    # Extract first {...}
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return None
    
    candidate = text[start:end+1]
    try:
        data = json.loads(candidate)
        validate(instance=data, schema=VALIDATION_SCHEMA)
        return data
    except (json.JSONDecodeError, ValidationError) as e:
        print(f"[warn] JSON extraction/validation failed: {e}")
        return None

def _minify_system_text(text: str) -> str:
    """
    Trim comments and blanks to reduce prompt size.
    """
    kept = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#") or set(s) <= {"-", "—"}:
            continue
        kept.append(s)
    return "\n".join(kept)

def load_annotation_schema(project_root: str) -> dict:
    """
    Load and cache annotation.schema.json.
    """
    global _schema_cache
    if _schema_cache is not None:
        return _schema_cache

    candidates = [
        os.path.join(os.path.dirname(__file__), "schema", "annotation.schema.json")
    ]

    for path in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                _schema_cache = json.load(f)
            # Print only the file name
            print(f"Using annotation schema: {os.path.basename(path)}")
            return _schema_cache

    tried = "\n  - ".join(os.path.abspath(p) for p in candidates)
    raise FileNotFoundError(f"Schema file not found. Tried:\n  - {tried}")

def load_system_prompt(project_root: str, image_count: int, film: Dict) -> str:
    """
    Load system prompt from the local repository's system.txt only.
    """
    prompt_path = os.path.join(os.path.dirname(__file__), "system.txt")
    if not os.path.exists(prompt_path):
        return ""
    with open(prompt_path, 'r', encoding='utf-8') as f:
        prompt = f.read()

    prompt = prompt.replace("{title}", film.get('title') or film.get('Title', 'Unknown'))
    prompt = prompt.replace("{year}", str(film.get('year', 'Unknown')))
    prompt = prompt.replace("{director}", film.get('director', 'Unknown'))
    prompt = prompt.replace("{image-count}", str(image_count))
    return prompt

def has_scenes(shotlist: List[Dict]) -> bool:
    for shot in shotlist:
        if (shot.get('Scene') or '').strip():
            return True
    return False

def get_unique_scenes(shotlist: List[Dict]) -> List[str]:
    scenes = set()
    for shot in shotlist:
        s = (shot.get('Scene') or '').strip()
        if s:
            scenes.add(s)
    return sorted(list(scenes), key=lambda x: int(x) if x.isdigit() else 0)

def parse_timecode(tc: str) -> float:
    parts = (tc or "").split(':')
    if len(parts) == 3:
        hh, mm, ss = parts
    elif len(parts) == 2:
        hh, (mm, ss) = 0, parts
    else:
        return 0.0
    try:
        hh = int(hh)
        mm = int(mm)
        ss = float(ss)
        return hh * 3600 + mm * 60 + ss
    except Exception:
        return 0.0

def ffmpeg_extract_frame(video_path: str, timestamp: float, output_path: str, verbose: bool = False) -> bool:
    """
    Fallback via ffmpeg: precise seek and extract 1 frame.
    """
    if not shutil.which("ffmpeg"):
        if verbose:
            print("    [ffmpeg] ffmpeg not found in PATH")
        return False
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{timestamp:.3f}",
        "-i", video_path,
        "-frames:v", "1",
        "-y", output_path
    ]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ok = r.returncode == 0 and os.path.exists(output_path)
    if verbose and not ok:
        err = r.stderr.decode("utf-8", errors="ignore")
        print(f"    [ffmpeg] failed at {timestamp:.3f}s -> {output_path}\n      {err.strip()}")
    return ok

def extract_frame_at_time(video_path: str, timestamp: float, output_path: str, verbose: bool = False) -> bool:
    """
    Try multiple strategies:
    1) Seek by frame index
    2) Seek by milliseconds
    3) Fallback to ffmpeg CLI
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        if verbose:
            print(f"    [ERROR] Cannot open video: {video_path}")
        return False

    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_s = (frame_count / fps) if (fps and fps > 1e-3 and frame_count > 0) else None

    # If timestamp looks out of bounds, report and short-circuit to ffmpeg
    if duration_s is not None and timestamp >= max(0.0, duration_s - 0.010):
        if verbose:
            print(f"    [WARN] Timestamp {timestamp:.3f}s >= video duration {duration_s:.3f}s (fps={fps:.3f}, frames={frame_count})")
        cap.release()
        return ffmpeg_extract_frame(video_path, timestamp, output_path, verbose=verbose)

    ok = False
    # Strategy 1: POS_FRAMES (can fail on some H.264 builds)
    if fps and fps > 1e-3:
        frame_index = int(timestamp * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = cap.read()
        if ok and frame is not None:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            if cv2.imwrite(output_path, frame):
                cap.release()
                return True
            if verbose:
                print(f"    [ERROR] Cannot write frame to {output_path}")

    # Strategy 2: POS_MSEC (time-based seek)
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
    ok, frame = cap.read()
    if ok and frame is not None:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        if cv2.imwrite(output_path, frame):
            cap.release()
            return True
        if verbose:
            print(f"    [ERROR] Cannot write frame to {output_path}")

    cap.release()

    # Strategy 3: ffmpeg fallback
    return ffmpeg_extract_frame(video_path, timestamp, output_path, verbose=verbose)

# Cache video info to avoid reopening per shot
@functools.lru_cache(maxsize=16)
def _get_video_info(video_path: str) -> Dict[str, float]:
    info = {"fps": 0.0, "frames": 0.0, "duration": 0.0}
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return info
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
    cap.release()
    info["fps"] = float(fps)
    info["frames"] = float(frames)
    info["duration"] = float(frames / fps) if fps and frames else 0.0
    return info

def extract_frames_for_shot(
    video_path: str,
    start_tc: str,
    end_tc: str,
    output_dir: str,
    movie_base: str,
    shot_index: int,
    verbose: bool = False
) -> List[str]:
    t0 = parse_timecode(start_tc)
    t1 = parse_timecode(end_tc)
    if t1 <= t0:
        if verbose:
            print(f"    [SKIP] Invalid timecode range: {start_tc} -> {end_tc}")
        return []

    # Clamp to video duration with small margin
    vinf = _get_video_info(video_path)
    duration = vinf.get("duration", 0.0)
    fps = vinf.get("fps", 0.0)
    margin = max(2.0 / fps if fps else 0.050, 0.050)  # ~2 frames or 50ms

    orig_t0, orig_t1 = t0, t1
    if duration > 0:
        # Clamp end to just before EOF
        t1 = min(t1, max(0.0, duration - margin))
        # Ensure there is some window; if not, pull start left
        if t1 <= t0:
            # Pull start to create a small window near EOF
            t0 = max(0.0, t1 - min(1.0, (orig_t1 - orig_t0) or 1.0))
        # Log clamping
        if verbose and (abs(orig_t0 - t0) > 1e-3 or abs(orig_t1 - t1) > 1e-3):
            print(f"    [CLAMP] {orig_t0:.3f}-{orig_t1:.3f}s -> {t0:.3f}-{t1:.3f}s (video {duration:.3f}s)")

    if t1 <= t0:
        if verbose:
            print(f"    [SKIP] Near end-of-file; no safe sampling window after clamping")
        return []

    duration_win = max(0.001, t1 - t0)

    # Dynamic sample count for very short windows
    samples = 4
    if duration_win < 0.20:
        samples = 1
    elif duration_win < 0.50:
        samples = 2

    # Evenly spaced within (t0, t1)
    times = [t0 + duration_win * (i + 1) / (samples + 1) for i in range(samples)]
    out_paths: List[str] = []

    film_dir = os.path.join(output_dir, movie_base)
    os.makedirs(film_dir, exist_ok=True)

    for idx, ts in enumerate(times, start=1):
        out = os.path.join(film_dir, f"shot_{shot_index:04d}_frame_{idx:02d}.jpg")
        ok = extract_frame_at_time(video_path, ts, out, verbose=verbose)
        if ok:
            out_paths.append(out)
        elif verbose:
            print(f"    [WARN] Failed to extract frame {idx} at {ts:.3f}s")
    return out_paths

def annotate_shot(
    shot: Dict,
    index: int,
    video_path: str,
    film: Dict,
    ollama: OllamaClient,
    frames_dir: str,
    project_root: str,
    verbose: bool = False
) -> tuple[str, float]:
    t0 = time.perf_counter()

    # Skip ignored or invalid rows
    if (shot.get('Ignore') or '').strip().lower() == 'yes':
        if verbose:
            print(f"    [SKIP] Shot marked as Ignore=yes")
        return "", 0.0
    
    start_tc = shot.get('Start') or shot.get('TC In') or ''
    end_tc = shot.get('End') or shot.get('TC Out') or ''
    if not start_tc or not end_tc:
        print(f"    [SKIP] Missing timecodes: Start='{start_tc}' End='{end_tc}'")
        return "", 0.0

    # Extract frames
    movie_filename = (film.get('filename') or film.get('Filename') or '')
    movie_base = os.path.splitext(movie_filename)[0]
    t_extract0 = time.perf_counter()
    image_paths = extract_frames_for_shot(video_path, start_tc, end_tc, frames_dir, movie_base, index-1, verbose=verbose)
    extract_s = time.perf_counter() - t_extract0
    
    if not image_paths:
        dur = time.perf_counter() - t0
        # Parse timecodes to show why extraction failed
        t0_parsed = parse_timecode(start_tc)
        t1_parsed = parse_timecode(end_tc)
        if t1_parsed <= t0_parsed:
            print(f"    [SKIP] Invalid timecode range: {start_tc} ({t0_parsed:.2f}s) to {end_tc} ({t1_parsed:.2f}s)")
        else:
            print(f"    [SKIP] Frame extraction failed (duration: {t1_parsed - t0_parsed:.2f}s)")
        if verbose:
            print(f"    details: frames=0 | extract {extract_s:.2f}s | model 0.00s | parse 0.00s")
        return "", dur

    system_text = load_system_prompt(project_root, len(image_paths), film)
    schema = load_annotation_schema(project_root)
    
    # Strict user prompt (reinforces JSON-only output)
    user_prompt = (
        "Output EXACTLY ONE JSON object matching the schema. "
        "Start with '{' and end with '}'. "
        "No explanations, no markdown, no code fences."
    )

    # Retry loop with stricter prompts
    MAX_RETRIES = 2
    data = None
    t_model0 = time.perf_counter()
    
    for attempt in range(MAX_RETRIES):
        try:
            resp = ollama.generate_with_images(
                prompt=user_prompt,
                image_paths=image_paths,
                system=system_text,
                schema=schema,
                stream=False
            )
            model_s = time.perf_counter() - t_model0
            
            if not resp:
                if verbose:
                    print(f"    [attempt {attempt+1}/{MAX_RETRIES}] No response from model")
                continue
            
            # Extract and validate
            t_parse0 = time.perf_counter()
            data = _extract_and_validate_json(resp)
            parse_s = time.perf_counter() - t_parse0
            
            if data:
                break  # Success
            else:
                if verbose:
                    print(f"    [attempt {attempt+1}/{MAX_RETRIES}] Invalid JSON, retrying...")
                # Tighten prompt for next attempt
                user_prompt = "CRITICAL: Return ONLY valid JSON. Begin with '{', end with '}'. No text before or after."
        except Exception as e:
            if verbose:
                print(f"    [attempt {attempt+1}/{MAX_RETRIES}] Error: {e}")
            continue
    
    model_s = time.perf_counter() - t_model0
    
    # Finalize
    caption = ""
    if data:
        caption = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    else:
        print(f"    [ERROR] Failed to get valid JSON after {MAX_RETRIES} attempts")
    
    dur = time.perf_counter() - t0
    if verbose:
        print(f"    details: frames={len(image_paths)} | extract {extract_s:.2f}s | model {model_s:.2f}s | valid={'YES' if data else 'NO'}")
    
    return caption, dur

def annotate_shots(
    shotlist: List[Dict],
    video_path: str,
    film: Dict,
    ollama: OllamaClient,
    frames_dir: str,
    project_root: str,
    limit: Optional[int] = None,
    start_index: int = 1,
    verbose: bool = False,
    save_callback=None  # New: called after each shot to save progress
) -> List[Dict]:
    os.makedirs(frames_dir, exist_ok=True)
    total = len(shotlist)
    plan = (min(limit, max(0, total - (start_index - 1))) if limit is not None else max(0, total - (start_index - 1)))
    planned_end = min(total, max(start_index, 1) + max(plan, 0) - 1)

    print(f"Starting annotation: {plan} planned shots (from {start_index} to {planned_end} of {total})")

    processed = 0
    sum_dur = 0.0
    t_total0 = time.perf_counter()

    for i, shot in enumerate(shotlist, start=1):
        if i < max(1, start_index):
            continue
        if limit is not None and processed >= limit:
            break
        if (shot.get('Ignore') or '').strip().lower() == 'yes':
            shot['Shot_Caption'] = ""
            continue

        print(f"Processing shot {processed+1}/{plan} (index {i} of {total})...")
        caption, dur = annotate_shot(shot, i, video_path, film, ollama, frames_dir, project_root, verbose=verbose)
        shot['Shot_Caption'] = caption

        # Save progress after each shot
        if save_callback:
            save_callback(shotlist)

        processed += 1
        sum_dur += dur
        avg = sum_dur / processed if processed else 0.0
        remaining = max(0, plan - processed)
        eta = remaining * avg
        mm, ss = divmod(int(eta), 60)
        pct = (processed / plan * 100.0) if plan else 100.0

        print(f"  ✓ {dur:.2f}s | progress {processed}/{plan} ({pct:.1f}%) | ETA {mm:02d}:{ss:02d}")

    total_dur = time.perf_counter() - t_total0
    if processed:
        print(f"\n{'='*60}")
        print(f"Total shots: {processed}/{plan}")
        print(f"Total time: {total_dur:.2f}s ({total_dur/60:.2f}m)")
        print(f"Avg/shot: {total_dur/processed:.2f}s")
        print(f"{'='*60}\n")
    return shotlist

def annotate_scene(scene_id: str, shots: List[Dict]) -> str:
    """Minimal scene summarizer placeholder."""
    return f"Scene {scene_id} summary."

def annotate_scenes(shotlist: List[Dict]) -> List[Dict]:
    """Apply a simple per-scene summary across shots."""
    scenes = get_unique_scenes(shotlist)
    for sid in scenes:
        scene_shots = [s for s in shotlist if (s.get('Scene') or '').strip() == sid]
        summary = annotate_scene(sid, scene_shots)
        for s in scene_shots:
            s['Scene_Caption'] = summary
    return shotlist