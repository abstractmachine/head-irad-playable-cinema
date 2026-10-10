"""Human-review rendering for engraving benchmark candidates.

Two sheets:

- a **candidate overview** showing each proposed object's silhouette crop with
  the metadata a curator needs to accept or reject it;
- a **temporal evidence sheet** per candidate, showing the shot's frames in
  chronological order with their roles, to answer whether another frame in the
  shot reveals structure the source frame does not.

Both reuse ``generators.mosaic``'s grid renderer rather than drawing their own,
and both are strictly read-only with respect to the catalog and the
Illustration index.
"""

from __future__ import annotations

from pathlib import Path

_MISSING_TILE = (320, 240)


def _review_tile(project_path, candidate, caption, width):
    import json
    from PIL import Image, ImageDraw, ImageOps
    from generators._common import load_font_with_fallback
    from generators.mosaic import _TILE_BG, _CAPTION_COLOR

    width = max(400, int(width))
    tile = Image.new("RGB", (width, width + 150), _TILE_BG)
    draw = ImageDraw.Draw(tile)
    font = load_font_with_fallback(18)
    context = None
    record_path = Path(candidate["source"].get("record_path") or "")
    if not record_path.is_absolute():
        record_path = Path(project_path) / record_path
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        raw = record.get("source_frame")
        if raw:
            context = Path(raw)
            if not context.is_absolute():
                context = Path(project_path) / context
            if not context.is_file():
                context = None
    except (OSError, ValueError):
        pass

    object_height = int(width * .7) if context else width
    panels = [(_silhouette_path(project_path, candidate), 0, object_height)]
    if context:
        panels.append((context, object_height, width - object_height))
    for path, top, height in panels:
        try:
            if path is None:
                raise ValueError("missing image")
            with Image.open(path) as opened:
                image = ImageOps.contain(opened.convert("RGBA"), (width - 16, height - 16))
            tile.paste(image, ((width - image.width) // 2, top + (height - image.height) // 2), image)
        except (OSError, TypeError, ValueError):
            draw.text((12, top + 20), "missing image", font=font, fill=_CAPTION_COLOR)

    wrapped = []
    for line in caption.splitlines():
        current = ""
        for word in line.split():
            proposed = f"{current} {word}".strip()
            if draw.textlength(proposed, font=font) > width - 24 and current:
                wrapped.append(current)
                current = word
            else:
                current = proposed
        wrapped.append(current)
    for index, line in enumerate(wrapped[:6]):
        draw.text((12, width + 6 + index * 23), line, font=font, fill=_CAPTION_COLOR)
    return tile


def _placeholder(text: str):
    """Return a grey tile standing in for an image that could not be loaded."""
    from generators.mosaic import make_intertitle_item

    return make_intertitle_item(text, width=_MISSING_TILE[0], height=_MISSING_TILE[1])


def _silhouette_path(project_path: str | Path, candidate: dict) -> Path | None:
    record_path = candidate["source"].get("record_path") or ""
    if not record_path:
        return None
    json_path = Path(record_path)
    if not json_path.is_absolute():
        json_path = Path(project_path) / json_path
    png_name = candidate["source"].get("png") or ""
    if png_name:
        sibling = json_path.parent / png_name
        if sibling.is_file():
            return sibling
    sibling = json_path.with_suffix(".png")
    return sibling if sibling.is_file() else None


def _candidate_caption(candidate: dict) -> str:
    """Return a compact two-line caption.

    The mosaic renderer only shows the first couple of caption lines, so the
    selection-critical facts are packed rather than listed.
    """
    source = candidate["source"]
    classification = candidate["classification"]
    evidence = candidate["evidence"]
    fraction = evidence.get("mask_area_fraction")
    area = f"{fraction:.3f}" if fraction else "?"

    line1 = (
        f"{candidate['benchmark_id']} {classification['structural_group']} "
        f"{classification['difficulty_stratum']}/{classification.get('scale_stratum')}"
    )
    line2 = (
        f"{source.get('label')} | {str(source.get('filename_stem') or '')[:26]} "
        f"f{int(source.get('frame') or 0):06d} "
        f"occ={str(evidence.get('occlusion'))[:4]} "
        f"cmp={str(evidence.get('completeness'))[:4]} "
        f"fr={evidence.get('fragment_count')} a={area}"
    )
    viability = candidate.get("viability") or {}
    metrics = viability.get("metrics") or {}
    gate = viability.get("gate") or {}
    line3 = (
        f"short={metrics.get('short_dimension_px', '?')}px | "
        f"gate={'PASS (all floors)' if gate.get('viable') else '; '.join(gate.get('reasons') or ['not measured'])}"
    )
    return f"{line1}\n{line2}\n{line3}"


def render_candidate_overview(
    project_path: str | Path,
    manifest: dict,
    output_path: str | Path,
    *,
    tile_max_dim: int = 560,
    per_sheet: int = 6,
) -> list[Path]:
    """Render the candidate pool as one or more reviewable contact sheets.

    Paginated and large-tiled on purpose: the first review sheet packed 40
    objects into one grid, which made it impossible to tell *the source object
    is too small* from *the thumbnail is too small*.
    """
    from generators.mosaic import MosaicItem, render_mosaic

    candidates = manifest["candidates"]
    if not candidates:
        raise ValueError("No candidates to render")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    per_sheet = max(1, int(per_sheet))
    pages = [candidates[i:i + per_sheet] for i in range(0, len(candidates), per_sheet)]

    written: list[Path] = []
    for page_number, page in enumerate(pages, start=1):
        items: list[MosaicItem] = []
        for candidate in page:
            caption = _candidate_caption(candidate)
            items.append(MosaicItem(pil_image=_review_tile(project_path, candidate, caption, tile_max_dim)))

        destination = (
            output_path if len(pages) == 1
            else output_path.with_name(f"{output_path.stem}-{page_number:02d}{output_path.suffix}")
        )
        render_mosaic(
            items, destination, layout="portrait",
            tile_max_dim=max(400, tile_max_dim) + 150, show_captions=False, verbose=False,
        )
        written.append(destination)
    return written


def render_review_comparison_sheet(
    project_path: str | Path,
    manifest: dict,
    review: dict,
    metrics: dict,
    output_path: str | Path,
    *,
    tile_max_dim: int = 460,
) -> Path:
    """Render all candidates in manifest order, labelled ACCEPTED / REJECTED.

    Pairs the human verdict with the measured signals so the two can be
    compared directly -- usually more informative than the summary statistics.
    """
    from generators.mosaic import MosaicItem, render_mosaic

    from services.engraving_benchmark_review import ACCEPTED, decision_for

    items: list[MosaicItem] = []
    for candidate in manifest["candidates"]:
        decision = decision_for(review, candidate["benchmark_id"])
        verdict = "ACCEPTED" if decision == ACCEPTED else "REJECTED"
        found = metrics.get(candidate["source"].get("record_path") or "") or {}
        caption = (
            f"{verdict}  {candidate['benchmark_id']}  {candidate['source']['label']}\n"
            f"short={found.get('short_dimension_px', '?')}px "
            f"mask={found.get('mask_area_px', '?')} "
            f"sharp={found.get('laplacian_variance', '?')} "
            f"fill={found.get('mask_fill_fraction', '?')}"
        )
        png = _silhouette_path(project_path, candidate)
        if png is None:
            item = _placeholder(f"{candidate['benchmark_id']}\nmissing")
            item.caption = caption
        else:
            item = MosaicItem(image_path=png, caption=caption)
        items.append(item)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return render_mosaic(
        items, output_path, layout="landscape",
        tile_max_dim=tile_max_dim, show_captions=True, verbose=False,
    )


def render_diagnostic_pages(project_path, manifest, review, metrics, output_path):
    """Render readable human decisions in unchanged Proposal-1 order."""
    from services.engraving_benchmark_review import annotate_candidates
    from services.engraving_viability import evaluate_gate

    annotated = annotate_candidates(manifest, review)
    for candidate in annotated:
        found = metrics.get(candidate["source"]["record_path"]) or {}
        candidate["benchmark_id"] = f"{candidate['human_review'].upper()} {candidate['benchmark_id']}"
        candidate["viability"] = {"metrics": found, "gate": evaluate_gate(found, candidate["evidence"])}
        candidate["source"] = dict(candidate["source"])
        candidate["source"]["label"] += (
            f" | Lap={found.get('laplacian_variance', '?')} contrast={found.get('local_contrast', '?')}"
        )
    return render_candidate_overview(project_path, {"candidates": annotated}, output_path)


def render_metric_distributions(analysis, output_path):
    """Draw per-case distributions, retaining outliers rather than hiding them."""
    import math
    from PIL import Image, ImageDraw
    from generators._common import load_font_with_fallback
    from generators.mosaic import _BG_COLOR, _CAPTION_COLOR, _TILE_BG
    from services.engraving_benchmark_review import ACCEPTED

    names = list(analysis["distributions"])
    canvas = Image.new("RGB", (1100, 60 + len(names) * 112), _BG_COLOR)
    draw = ImageDraw.Draw(canvas)
    font = load_font_with_fallback(17)
    draw.text((24, 15), "Proposal 1: all 40 cases; log1p axes; not a learned perceptual model", font=font, fill=_CAPTION_COLOR)
    for index, name in enumerate(names):
        top = 60 + index * 112
        draw.text((24, top), name, font=font, fill=_CAPTION_COLOR)
        values = [case["metrics"].get(name) for case in analysis["cases"]]
        maximum = max((value for value in values if value is not None), default=1)
        for row, decision in enumerate((ACCEPTED, "rejected_visual_viability")):
            height = top + 37 + row * 28
            draw.text((24, height - 10), "ACCEPTED" if row == 0 else "REJECTED", font=font, fill=_CAPTION_COLOR)
            draw.line((180, height, 1000, height), fill=_TILE_BG, width=2)
            for case in analysis["cases"]:
                value = case["metrics"].get(name)
                if case["decision"] == decision and value is not None:
                    horizontal = 180 + int(820 * math.log1p(value) / max(1e-9, math.log1p(maximum)))
                    draw.ellipse((horizontal - 3, height - 3, horizontal + 3, height + 3), fill=_CAPTION_COLOR)
        draw.text((1005, top + 45), f"{maximum:.3g}", font=font, fill=_CAPTION_COLOR)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path)
    return output_path


def _role_caption(entry: dict, fps: float | None) -> str:
    roles = " · ".join(role.upper() for role in entry["roles"])
    seconds = f"  {entry['frame'] / fps:.2f}s" if fps else ""
    return f"f{entry['frame']:06d}{seconds}\n{roles}"


def render_shot_evidence_sheet(
    project_path: str | Path,
    record: dict,
    evidence: dict,
    output_path: str | Path,
    *,
    tile_max_dim: int = 420,
) -> Path:
    """Render one shot's evidence frames in chronological order.

    Frames are pulled from the source video on demand via the existing
    extractor; the cached best-frame PNG is preferred when it matches.
    """
    from generators.mosaic import MosaicItem, _find_video_path, render_mosaic

    from services.engraving_benchmark_evidence import best_frame_image

    filename = str(record.get("filename") or "")
    video = _find_video_path(str(project_path), Path(filename).stem)
    cached_best = best_frame_image(project_path, record)
    best_frame = evidence.get("best")
    fps = evidence.get("fps")

    items: list[MosaicItem] = []
    for entry in evidence["selected_unique"]:
        caption = _role_caption(entry, fps)
        if cached_best is not None and entry["frame"] == best_frame:
            items.append(MosaicItem(image_path=cached_best, caption=caption))
        elif video is not None:
            items.append(
                MosaicItem(video_path=video, frame_index=entry["frame"], caption=caption)
            )
        else:
            item = _placeholder(f"f{entry['frame']:06d}\nunavailable")
            item.caption = caption
            items.append(item)

    if not items:
        raise ValueError("No evidence frames to render")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return render_mosaic(
        items,
        output_path,
        layout="landscape",
        tile_max_dim=tile_max_dim,
        show_captions=True,
        verbose=False,
    )
