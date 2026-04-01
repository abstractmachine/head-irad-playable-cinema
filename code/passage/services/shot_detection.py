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
    frame_rate = 25  # Default, will be updated from video metadata
    
    # Try to get actual frame rate
    try:
        import av
        with av.open(video_path) as container:
            stream = container.streams.video[0]
            frame_rate = float(stream.average_rate)
    except Exception:
        # Fallback to default if we can't read frame rate
        pass
    
    # Find boundaries
    shots = []
    boundaries = []
    
    for i, pred in enumerate(single_frame_predictions):
        if pred >= threshold:
            boundaries.append((i, pred))
    
    # Convert boundaries to shots
    if not boundaries:
        # No boundaries detected - entire video is one shot
        duration = len(single_frame_predictions) / frame_rate
        shots.append({
            "start": 0.0,
            "end": duration,
            "confidence": 1.0,
            "source": "auto"
        })
    else:
        # Create shots from boundaries
        prev_frame = 0
        for frame_idx, confidence in boundaries:
            if frame_idx > prev_frame:
                start_time = prev_frame / frame_rate
                end_time = frame_idx / frame_rate
                shots.append({
                    "start": start_time,
                    "end": end_time,
                    "confidence": float(confidence),
                    "source": "auto"
                })
            prev_frame = frame_idx
        
        # Add final shot
        final_frame = len(single_frame_predictions)
        if prev_frame < final_frame:
            start_time = prev_frame / frame_rate
            end_time = final_frame / frame_rate
            shots.append({
                "start": start_time,
                "end": end_time,
                "confidence": 1.0,  # No boundary at end
                "source": "auto"
            })
    
    return shots


def write_shotlist_csv(
    project_path: str,
    filename: str,
    shots: list[dict[str, Any]],
    media_type: str = "movies",
    force: bool = False
) -> Path:
    """Write detected shots to CSV file.
    
    Args:
        project_path: Path to project
        filename: Video filename
        shots: List of shot dicts with start, end, confidence, source
        media_type: movies or gameplay
        force: Overwrite existing file if True
    
    Returns:
        Path to created CSV file
    
    Raises:
        FileExistsError: If CSV exists and force=False
    """
    from services.shotlist import get_shotlist_path
    
    csv_path = get_shotlist_path(project_path, filename, media_type)
    
    # Check if file exists
    if csv_path.exists() and not force:
        raise FileExistsError(
            f"Shotlist already exists: {csv_path}\n"
            "Use --force to overwrite."
        )
    
    # Create directory if needed
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Write CSV
    fieldnames = [
        "Ignore",
        "Scene",
        "Start",
        "End",
        "Shot_Caption",
        "Scene_Caption",
        "Shot_Source",
        "Shot_Confidence"
    ]
    
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for shot in shots:
            writer.writerow({
                "Ignore": "No",
                "Scene": "0",
                "Start": format_timecode(shot["start"]),
                "End": format_timecode(shot["end"]),
                "Shot_Caption": "",
                "Scene_Caption": "",
                "Shot_Source": shot["source"],
                "Shot_Confidence": f"{shot['confidence']:.3f}"
            })
    
    return csv_path
