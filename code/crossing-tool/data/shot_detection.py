"""Shot boundary detection using TransNetV2."""

import csv
from pathlib import Path
from typing import Any


def format_timecode(seconds: float) -> str:
    """Convert seconds to HH:MM:SS.mmm format."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def detect_shots_transnet(video_path: str) -> list[dict[str, Any]]:
    """Detect shot boundaries using TransNet V2.
    
    Args:
        video_path: Path to video file
    
    Returns:
        List of shots with start, end, and confidence data
    
    Raises:
        ImportError: If TransNetV2 not installed
        RuntimeError: If detection fails
    """
    try:
        from transnetv2 import TransNetV2
    except ModuleNotFoundError as e:
        if 'tensorflow' in str(e).lower():
            raise ImportError(
                "TransNetV2 requires TensorFlow. Install with:\n"
                "  pip install tensorflow>=2.5 ffmpeg-python\n"
                "Or install all shot detection dependencies:\n"
                "  pip install git+https://github.com/soCzech/TransNetV2.git tensorflow>=2.5 ffmpeg-python"
            )
        raise ImportError(
            "TransNetV2 not installed. Install with:\n"
            "  pip install git+https://github.com/soCzech/TransNetV2.git tensorflow>=2.5 ffmpeg-python"
        )
    
    # Initialize model
    model = TransNetV2()
    
    # Detect shots
    try:
        video_frames, single_frame_predictions, all_frame_predictions = \
            model.predict_video(video_path)
    except Exception as e:
        if 'ffmpeg' in str(e).lower():
            raise RuntimeError(
                "TransNetV2 requires ffmpeg-python to read video files. Install with:\n"
                "  pip install ffmpeg-python"
            )
        raise
    
    # Get shot boundaries with confidence threshold
    threshold = 0.5
    frame_rate = 25.0  # Default fallback
    
    # Try to get actual frame rate from video
    try:
        import av
        with av.open(video_path) as container:
            stream = container.streams.video[0]
            # average_rate is a Fraction - convert to float
            if stream.average_rate is not None:
                frame_rate = float(stream.average_rate)
                print(f"[Shot Detection] Detected frame rate: {frame_rate:.3f} fps")
            else:
                print(f"[Shot Detection] No frame rate in metadata, using default: {frame_rate} fps")
    except ImportError:
        # PyAV not installed, try ffprobe as fallback
        try:
            import subprocess
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0', 
                 '-show_entries', 'stream=r_frame_rate', '-of', 'default=noprint_wrappers=1:nokey=1',
                 video_path],
                capture_output=True, text=True, check=True
            )
            # Parse fraction like "24000/1001"
            fps_str = result.stdout.strip()
            if '/' in fps_str:
                num, den = fps_str.split('/')
                frame_rate = float(num) / float(den)
            else:
                frame_rate = float(fps_str)
            print(f"[Shot Detection] Detected frame rate (ffprobe): {frame_rate:.3f} fps")
        except Exception as e:
            print(f"[Shot Detection] Could not detect frame rate ({e}), using default: {frame_rate} fps")
    except Exception as e:
        print(f"[Shot Detection] Could not read frame rate ({e}), using default: {frame_rate} fps")
    
    # Find boundaries
    shots = []
    boundaries = []
    
    for i, pred in enumerate(single_frame_predictions):
        if pred >= threshold:
            boundaries.append((i, pred))
    
    # Convert boundaries to shots
    # Boundary semantics: frame_idx is the LAST frame of the CURRENT shot
    # (TransNetV2 marks the transition frame as belonging to the ending shot)
    # So the next shot starts at frame_idx + 1
    if not boundaries:
        # No boundaries detected - entire video is one shot
        duration = len(single_frame_predictions) / frame_rate
        total_frames = len(single_frame_predictions)
        shots.append({
            "start": 0.0,
            "end": duration,
            "start_frame": 0,
            "end_frame": total_frames - 1,
            "confidence": 1.0,
            "source": "auto"
        })
    else:
        # Create shots from boundaries
        prev_frame = 0
        for frame_idx, confidence in boundaries:
            if frame_idx >= prev_frame:
                start_time = prev_frame / frame_rate
                # End time is AT the boundary frame (transition frame belongs to current shot)
                end_time = frame_idx / frame_rate
                shots.append({
                    "start": start_time,
                    "end": end_time,
                    "start_frame": prev_frame,
                    "end_frame": frame_idx,
                    "confidence": float(confidence),
                    "source": "auto"
                })
                # Next shot starts AFTER the boundary frame
                prev_frame = frame_idx + 1
        
        # Add final shot (if there are frames after the last boundary)
        final_frame = len(single_frame_predictions)
        if prev_frame < final_frame:
            start_time = prev_frame / frame_rate
            # Last shot goes to the end of the video
            end_time = (final_frame - 1) / frame_rate
            shots.append({
                "start": start_time,
                "end": end_time,
                "start_frame": prev_frame,
                "end_frame": final_frame - 1,
                "confidence": 1.0,  # No boundary at end
                "source": "auto"
            })
    
    # Filter out zero-length shots (can occur when consecutive frames trigger detection)
    original_count = len(shots)
    shots = [shot for shot in shots if shot['start'] < shot['end']]
    filtered_count = original_count - len(shots)
    if filtered_count > 0:
        print(f"[Shot Detection] Filtered out {filtered_count} zero-length shot(s)")
    
    return shots


def write_shotlist_csv(
    project_path: str,
    filename: str,
    shots: list[dict[str, Any]],
    media_type: str = "movie",
    force: bool = False
) -> Path:
    """Write detected shots to CSV file.
    
    Args:
        project_path: Path to project
        filename: Video filename
        shots: List of shot dicts with start, end, confidence, source
        media_type: movie or gameplay
        force: Overwrite existing file if True
    
    Returns:
        Path to created CSV file
    
    Raises:
        FileExistsError: If CSV exists and force=False
    """
    from data.shotlist import get_shotlist_path
    
    csv_path = get_shotlist_path(project_path, filename, media_type)
    
    # Check if file exists
    if csv_path.exists() and not force:
        raise FileExistsError(
            f"Shotlist already exists: {csv_path}\n"
            "Use --force to overwrite."
        )
    
    # Create directory if needed
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    # Look up media_id for stable, canonical shot IDs
    from data.media_id import build_shot_id as _build_shot_id
    from data.metadata import get_metadata as _get_metadata
    meta_entries = _get_metadata(project_path, media_type=media_type)
    meta = next((e for e in meta_entries if e.get("filename") == filename), {})
    media_id = str(meta.get("media_id") or "")

    # Write CSV
    fieldnames = [
        "Ignore",
        "Scene",
        "start_time",
        "end_time",
        "start_frame",
        "end_frame",
        "shot_id",
        "Shot_Caption",
        "Scene_Caption",
    ]
    
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for shot in shots:
            sf = shot["start_frame"]
            ef = shot["end_frame"]
            writer.writerow({
                "Ignore": "No",
                "Scene": "0",
                "start_time": format_timecode(shot["start"]),
                "end_time": format_timecode(shot["end"]),
                "start_frame": sf,
                "end_frame": ef,
                "shot_id": _build_shot_id(media_id, sf, ef) if media_id else "",
                "Shot_Caption": "",
                "Scene_Caption": "",
            })
    
    return csv_path
