import json
import subprocess
from pathlib import Path


def inspect_audio_channel_count(video_path: Path) -> int:
    """Return channel count of the first audio stream using ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=channels",
        "-of",
        "json",
        str(video_path),
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe is not installed or not found in PATH.") from exc

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip().splitlines()[-10:]
        details = "\n".join(stderr_tail)
        raise RuntimeError(f"ffprobe channel inspection failed for {video_path.name}:\n{details}")

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid ffprobe JSON output while reading channels.") from exc

    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError(f"No audio stream found in {video_path.name}.")

    channels = streams[0].get("channels")
    if channels is None:
        raise RuntimeError(f"ffprobe did not report channel count for {video_path.name}.")

    try:
        count = int(channels)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid channel count reported for {video_path.name}: {channels!r}") from exc

    if count <= 0:
        raise RuntimeError(f"Invalid channel count reported for {video_path.name}: {count}")

    return count


def suggest_audio_channels_mapping(channel_count: int) -> dict:
    """Return metadata mapping for playback channel selection.

    - mono streams: {"mono": 0}
    - multi-channel streams: {"left": 0, "right": 1}
    """
    if int(channel_count) <= 1:
        return {"mono": 0}
    return {"left": 0, "right": 1}
