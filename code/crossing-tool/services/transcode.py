import subprocess
from pathlib import Path

PROFILES = {
    "universal": {"crf": "23", "keyframe": "30", "audio_bitrate": "128k"},
    "pi5":       {"crf": "26", "keyframe": "15", "audio_bitrate": "96k"},
}


def transcode_file(src: Path, project_path: str, media_type: str = "movie", platform: str = "universal") -> Path | None:
    from services.normalize import normalize_filename
    dest_dir = Path(project_path) / "media" / "videos" / media_type
    dest_dir.mkdir(parents=True, exist_ok=True)
    normalized_name = normalize_filename(src.stem + ".mp4")
    dest = dest_dir / normalized_name

    if dest.exists():
        print(f"  skip  {dest.name}  (already exists)")
        return dest

    p = PROFILES[platform]
    duration = _get_duration(src)

    cmd = [
        "ffmpeg", "-i", str(src),
        "-map", "0:v:0",  # Map only first video stream
        "-map", "0:a:0",  # Map only first audio stream
        "-c:v", "libx264", "-preset", "fast",
        "-crf", p["crf"],
        "-g", p["keyframe"],
        "-pix_fmt", "yuv420p",
        "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
        "-c:a", "aac", "-ac", "2", "-b:a", p["audio_bitrate"],  # Force stereo output
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        "-y", str(dest),
    ]

    print(f"  transcoding  {src.name} →  {dest_dir}")

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        print("  ERROR: ffmpeg not found")
        return None

    stderr_output = []
    for line in process.stdout:
        key, _, value = line.strip().partition("=")
        if key == "out_time_ms" and duration and value.lstrip("-").isdigit():
            elapsed = int(value) / 1_000_000
            pct = min(100, int(elapsed / duration * 100))
            print(f"\r  {pct:3d}%", end="", flush=True)
        elif key == "progress" and value == "end":
            print(f"\r  100%  done")

    # Capture any stderr output for error reporting
    stderr_text = process.stderr.read()
    process.wait()

    if process.returncode != 0:
        print(f"\n  ERROR: ffmpeg exited with code {process.returncode}")
        # Show last few lines of stderr if available
        if stderr_text:
            stderr_lines = stderr_text.strip().split('\n')
            relevant_errors = [line for line in stderr_lines[-5:] if line.strip()]
            if relevant_errors:
                print(f"  Details: {relevant_errors[-1]}")
        if dest.exists():
            dest.unlink()
        return None

    return dest


def _get_duration(src: Path) -> float | None:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(src),
            ],
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())
    except (ValueError, FileNotFoundError):
        return None


def get_video_thumbnail_timestamp(video_path: "str | Path") -> float:
    """Return the canonical timestamp used for an extracted video thumbnail."""
    duration = _get_duration(Path(video_path))
    return round(duration * 0.05, 3) if duration else 3.0


def extract_video_thumbnail(video_path: "str | Path", thumb_path: "str | Path") -> "Path | None":
    """Extract a single frame from ~5% into *video_path* and save as JPEG at *thumb_path*.

    Returns *thumb_path* on success, ``None`` on failure.
    Skips silently if the thumbnail already exists.
    """
    video_path = Path(video_path)
    thumb_path = Path(thumb_path)

    if thumb_path.exists():
        return thumb_path

    thumb_path.parent.mkdir(parents=True, exist_ok=True)

    offset = get_video_thumbnail_timestamp(video_path)

    cmd = [
        "ffmpeg",
        "-ss", str(offset),
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", "2",
        "-y", str(thumb_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0 and thumb_path.exists():
            return thumb_path
        return None
    except FileNotFoundError:
        return None
