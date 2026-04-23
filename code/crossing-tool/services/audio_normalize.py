import json
import math
import re
import subprocess
from pathlib import Path


_LOUDNORM_JSON_RE = re.compile(r"\{\s*\"input_i\".*?\}", re.DOTALL)


def parse_integrated_lufs(ffmpeg_stderr: str) -> float:
    """Extract integrated LUFS from ffmpeg loudnorm stderr output."""
    match = _LOUDNORM_JSON_RE.search(ffmpeg_stderr)
    if not match:
        raise RuntimeError("Could not parse loudnorm output from ffmpeg.")

    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid loudnorm JSON output from ffmpeg.") from exc

    integrated = payload.get("input_i")
    if integrated in (None, "", "-inf", "inf"):
        raise RuntimeError("ffmpeg loudnorm did not report a usable integrated LUFS value.")

    try:
        integrated_lufs = float(integrated)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integrated LUFS value: {integrated!r}") from exc

    if not math.isfinite(integrated_lufs):
        raise RuntimeError(f"Invalid integrated LUFS value: {integrated!r}")

    return integrated_lufs


def compute_audio_gain_db(integrated_lufs: float, target_lufs: float) -> float:
    """Compute gain adjustment in dB to move integrated LUFS to target."""
    return round(float(target_lufs) - float(integrated_lufs), 3)


def measure_audio_gain_db(video_path: Path, target_lufs: float) -> tuple[float, float]:
    """Measure integrated loudness and return (gain_db, integrated_lufs)."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(video_path),
        "-vn",
        "-sn",
        "-dn",
        "-af",
        f"loudnorm=I={target_lufs}:LRA=7:TP=-2:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is not installed or not found in PATH.") from exc

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip().splitlines()[-10:]
        details = "\n".join(stderr_tail)
        raise RuntimeError(f"ffmpeg loudness analysis failed for {video_path.name}:\n{details}")

    integrated_lufs = parse_integrated_lufs(completed.stderr or "")
    gain_db = compute_audio_gain_db(integrated_lufs, target_lufs)
    return gain_db, integrated_lufs
