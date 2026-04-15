#!/usr/bin/env python3
import os
import sys
import subprocess
import json

def probe_duration(filepath):
    """Return duration in seconds using ffprobe, or None on failure."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        filepath
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        # ffprobe couldn't read this file
        return None

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    if "format" in info and "duration" in info["format"]:
        try:
            return float(info["format"]["duration"])
        except (TypeError, ValueError):
            return None

    return None


def extract_middle_10s(input_file, output_file):
    """Extract 10 seconds around the midpoint of the video."""
    duration = probe_duration(input_file)
    if duration is None or duration <= 0:
        print(f"   ⚠️  Skipping (no valid duration): {input_file}")
        return

    clip_length = 10.0

    mid = duration / 2.0
    start = max(mid - clip_length / 2.0, 0)

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(start),
        "-i", input_file,
        "-t", str(clip_length),
        "-c", "copy",
        output_file,
    ]

    print("   ffmpeg:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main():
    if len(sys.argv) < 2:
        print("Usage: python extract.py <folder>")
        sys.exit(1)

    folder = sys.argv[1]
    out_dir = os.path.join(folder, "excerpts")
    os.makedirs(out_dir, exist_ok=True)

    # Skip ._ files (macOS resource forks)
    videos = sorted([
        f for f in os.listdir(folder)
        if f.lower().endswith(".mp4") and not f.startswith("._")
    ])

    if not videos:
        print("No .mp4 files found.")
        sys.exit(0)

    print(f"Found {len(videos)} movies\n")

    for i, filename in enumerate(videos, 1):
        input_path = os.path.join(folder, filename)

        output_name = (
            os.path.splitext(filename)[0]
            + "_excerpt.mp4"
        )
        output_path = os.path.join(out_dir, output_name)

        print(f"[{i}/{len(videos)}] Extracting middle 10s from: {filename}")
        try:
            extract_middle_10s(input_path, output_path)
        except subprocess.CalledProcessError as e:
            print(f"   ❌ ffmpeg failed for {filename}: {e}")

    print("\n✅ Done! Excerpts saved to:", out_dir)


if __name__ == "__main__":
    main()