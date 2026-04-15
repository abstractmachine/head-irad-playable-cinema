#!/usr/bin/env python3
import os
import sys
import json
import subprocess
from pathlib import Path

CANVAS_W = 1920
CANVAS_H = 1080


def ffprobe_size(path: str):
    """Return (width, height) for the first video stream."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-select_streams", "v:0",
        "-show_streams",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}")

    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    if not streams:
        raise RuntimeError(f"No video stream found in {path}")

    s0 = streams[0]
    return int(s0["width"]), int(s0["height"])


def compute_layout(files, tile_h):
    """
    Compute (x, y, file_index) for each clip.
    All tiles have height = tile_h; width scales with original ratio.
    """
    layout = []
    x = 0
    y = 0

    sizes = []
    for path in files:
        w, h = ffprobe_size(path)
        scaled_w = int(round(w * (tile_h / h)))
        sizes.append((w, h, scaled_w))

    for idx, (path, (_, _, scaled_w)) in enumerate(zip(files, sizes)):
        if y + tile_h > CANVAS_H:
            break

        layout.append((idx, x, y))

        x += scaled_w
        if x > CANVAS_W:
            x = 0
            y += tile_h

    return layout, sizes


def build_filter_complex(layout, tile_h):
    """
    Build an ffmpeg filter_complex string for only the clips actually used
    in the layout.
    """
    filter_lines = []

    # Base canvas
    filter_lines.append(
        f"nullsrc=size={CANVAS_W}x{CANVAS_H},format=yuv420p[base0]"
    )

    # Which input indices are actually used?
    used_indices = sorted({idx for (idx, _, _) in layout})
    max_idx = max(used_indices)

    # Scale only used inputs (0 .. max_idx, which are contiguous in our layout)
    for idx in range(max_idx + 1):
        filter_lines.append(
            f"[{idx}:v]setpts=PTS-STARTPTS,scale=-1:{tile_h}[v{idx}]"
        )

    # Overlay chain
    last_label = "[base0]"
    for (idx, x, y) in layout:
        out_label = f"[base{idx+1}]"
        filter_lines.append(
            f"{last_label}[v{idx}]overlay={x}:{y}{out_label}"
        )
        last_label = out_label

    filter_lines.append(f"{last_label}null[outv]")

    return ";".join(filter_lines)


def main():
    if len(sys.argv) < 3:
        print("Usage: python grid.py <excerpts_folder> <output.mp4> [tile_height] [max_clips]")
        sys.exit(1)

    folder = Path(sys.argv[1])
    output = Path(sys.argv[2])

    tile_h = int(sys.argv[3]) if len(sys.argv) >= 4 else 60
    max_clips = int(sys.argv[4]) if len(sys.argv) >= 5 else 200

    if not folder.is_dir():
        print(f"❌ Not a directory: {folder}")
        sys.exit(1)

    clips = sorted(
        p for p in folder.iterdir()
        if p.suffix.lower() == ".mp4" and not p.name.startswith("._")
    )

    if not clips:
        print("No .mp4 clips found in", folder)
        sys.exit(1)

    clips = clips[:max_clips]

    print(f"Using {len(clips)} clips, tile height = {tile_h}px")

    # Compute layout
    layout, sizes = compute_layout([str(p) for p in clips], tile_h)
    if not layout:
        print("❌ Could not place any clips (tile_height too big?)")
        sys.exit(1)

    # Only keep clips actually needed (0..max_idx)
    max_idx = max(idx for (idx, _, _) in layout)
    clips = clips[: max_idx + 1]

    print(f"Placing {len(layout)} clips on 1920x1080.")
    if max_idx + 1 < len(sizes):
        print(f"⚠️ Some clips did not fit vertically and are ignored.")

    filter_complex = build_filter_complex(layout, tile_h)

    cmd = ["ffmpeg", "-y"]
    for p in clips:
        cmd += ["-i", str(p)]

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-t", "10",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        str(output),
    ]

    print("\nRunning ffmpeg:")
    print(" ".join(cmd), "\n")

    subprocess.run(cmd, check=True)
    print(f"✅ Mosaic written to: {output}")


if __name__ == "__main__":
    main()