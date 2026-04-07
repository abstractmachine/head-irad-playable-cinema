"""Mosaic rendering for persona detection results.

Produces one image per persona inside a subfolder, showing representative
frame appearances for that persona.  Each image is a horizontal strip of
up to ``max_per_persona`` frames.

Uses the project's existing MosaicItem / render_mosaic infrastructure so
the output style is consistent with other mosaic commands.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from .io import read_persona_json
from .models import PersonaDocument


def persona_mosaic(
    project_path: str,
    filename: str,
    media_type: str = "movies",
    max_per_persona: int = 6,
    output_path: Optional[Path] = None,
    layout: str = "landscape",
    open_result: bool = True,
) -> Path:
    """Generate one mosaic image per persona for a film, placed in a subfolder.

    Args:
        project_path:      Project root directory.
        filename:          Video filename (used to locate the JSON and video).
        media_type:        'movies' or 'gameplay'.
        max_per_persona:   Maximum appearance frames to show per persona image.
        output_path:       Override output *folder* path.  Default:
                           output/mosaics/<stem>-personas/
        layout:            'landscape' or 'portrait' (passed to render_mosaic).
        open_result:       Open the output folder in the file manager when done.

    Returns:
        Path to the output folder containing the per-persona images.

    Raises:
        FileNotFoundError: if the persona JSON or the video file is missing.
        ValueError:        if no personas are found in the JSON.
    """
    from services.mosaic import MosaicItem, render_mosaic

    doc: PersonaDocument = read_persona_json(project_path, filename, media_type)

    if not doc.personas:
        raise ValueError(
            f"No personas found in the JSON for '{filename}'.\n"
            "  Run persona detection first: crossing persona detect <title>"
        )

    video_path = Path(project_path) / "media" / "videos" / media_type / filename
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    if output_path is None:
        stem = Path(filename).stem
        output_path = (
            Path(project_path) / "output" / "mosaics" / f"{stem}-personas"
        )

    output_path.mkdir(parents=True, exist_ok=True)

    for persona in doc.personas:
        # Pick best appearances, then re-sort chronologically
        sorted_apps = sorted(persona.appearances, key=lambda a: a.confidence, reverse=True)
        selected = sorted(sorted_apps[:max_per_persona], key=lambda a: a.shot_id)

        items = [
            MosaicItem(
                video_path=video_path,
                frame_index=app.frame_index,
                caption=(
                    f"{persona.persona_id}  ·  shot {app.shot_id}"
                    f"  ·  {app.confidence:.2f}"
                ),
                crop_bbox=app.bbox,
                crop_padding=20,
            )
            for app in selected
        ]

        if not items:
            continue

        out_file = output_path / f"{persona.persona_id}.png"
        render_mosaic(items, out_file, layout=layout, show_captions=True)

    if open_result:
        subprocess.Popen(["xdg-open", str(output_path)])

    return output_path
