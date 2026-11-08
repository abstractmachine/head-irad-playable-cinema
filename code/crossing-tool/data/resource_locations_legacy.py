"""Transitional current-production layout only. Remove after migration.

No discovery, allocation, lifecycle repair or filesystem mutation is permitted.
Canonical destinations must never call these legacy constructors.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def annotation_path(project, filename, media_type, suffix):
    from data.metadata import normalize_media_type
    from data.resource_locations import ANNOTATION_SUFFIXES, component
    normalize_media_type(media_type)
    component(filename)
    if suffix not in ANNOTATION_SUFFIXES:
        raise ValueError("Unsupported annotation artifact")
    return Path(project) / "data/annotations/shots" / media_type / (Path(filename).stem + suffix)


def annotation_filenames(project, media_type):
    from data.resource_locations import ANNOTATION_SUFFIXES
    root = Path(project) / "data/annotations/shots" / media_type
    stems = set()
    for path in root.glob("*"):
        if path.is_file():
            suffix = next((value for value in ANNOTATION_SUFFIXES if path.name.endswith(value)), None)
            if suffix:
                stems.add(path.name.removesuffix(suffix))
    return sorted(stems)


def annotation_v0_json_path(project, filename, media_type):
    """Explicit pre-suffix reader compatibility; never select an alternate layout."""
    return annotation_path(project, filename, media_type, ".annotations.json").with_name(Path(filename).stem + ".json")


def annotation_frame_sidecar(project, frames_path, media_type, suffix):
    from data.resource_locations import ANNOTATION_SUFFIXES
    frames_path = Path(frames_path)
    if suffix not in ANNOTATION_SUFFIXES or not frames_path.name.endswith(".frames.npy"):
        raise ValueError("Unsupported legacy frame sidecar")
    stem = frames_path.name.removesuffix(".frames.npy")
    expected = annotation_path(project, stem + ".mp4", media_type, ".frames.npy")
    if expected.resolve() != frames_path.resolve():
        raise ValueError("Frame artifact differs from legacy location contract")
    return annotation_path(project, stem + ".mp4", media_type, suffix)


def engraving_catalog_root(project, media_type):
    return Path(project) / "data/engravings/catalog" / media_type


def engraving_catalog_paths(project, media_type, *, isolated_only=False):
    root = engraving_catalog_root(project, media_type)
    return root.glob("*/*/*/isolated/engraving.json") if isolated_only else sorted(root.rglob("engraving.json"))


def engraving_catalog_identity(metadata_path):
    mode_dir = Path(metadata_path).parent
    return {"mode": mode_dir.name, "object_id": mode_dir.parent.name,
            "label": mode_dir.parent.parent.name, "filename_stem": mode_dir.parent.parent.parent.name}


def engraving_output(project, mode_dir, metadata, *, include_raw=False, sorted_names=False):
    raw = metadata.get("output_png")
    if raw:
        candidate = Path(str(raw))
        candidate = candidate if candidate.is_absolute() else Path(project) / candidate
        if candidate.exists():
            return candidate
    names = mode_dir.glob("*.png")
    names = sorted(names) if sorted_names else names
    named = [path for path in names if path.name != "raw.png"]
    raw_path = mode_dir / "raw.png"
    return named[0] if named else (raw_path if include_raw and raw_path.exists() else None)


def catalog_label(label):
    """Preserve catalog spelling exactly; unlike engraving's synthetic fallback."""
    return re.sub(r"[^a-z0-9_]", "_", label.lower().strip())


def silhouette_catalog_root(project, media_type):
    return Path(project) / "data/silhouettes/catalog" / media_type


def silhouette_catalog_item(project, media_type, filename_stem, label):
    return silhouette_catalog_root(project, media_type) / filename_stem / catalog_label(label)


def silhouette_png(source_json, metadata):
    """Legacy PNG-name/suffix contract, isolated from canonical identity lookup."""
    from data.resource_locations import component
    source_json = Path(source_json)
    candidates = []
    if metadata.get("png"):
        candidates.append(source_json.parent / component(metadata["png"]))
    candidates.append(source_json.with_suffix(".png"))
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise FileNotFoundError("Missing legacy silhouette PNG: " + str(source_json))


def registered_silhouette(project, binding):
    from data.resource_locations import project_file
    path = project_file(project, binding["legacy_json"])
    if not path.relative_to(Path(project).resolve()).as_posix().startswith("data/silhouettes/catalog/"):
        raise ValueError("Legacy binding is not a current catalog resource")
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if (metadata.get("media_id") != binding["media_id"]
            or metadata.get("media_type") != binding["media_type"]):
        raise ValueError("Stale legacy source identity")
    image = project_file(project, silhouette_png(path, metadata))
    if image != project_file(project, binding["legacy_png"]):
        raise ValueError("Stale registered legacy image location")
    return {"instance_id": binding["instance_id"], "metadata": path,
            "image": image, "record": metadata, "layout": "legacy-v1"}


def registered_engraving(project, binding, mode):
    from data.resource_locations import project_file
    source = registered_silhouette(project, binding)
    paths = engraving_paths(project, source["metadata"], source["record"], mode)
    path = project_file(project, paths["metadata"])
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if metadata.get("status") != "generated" or metadata.get("mode") != mode:
        raise ValueError("Legacy engraving is not a generated publication of this mode")
    source_pointer = (metadata.get("source") or {}).get("silhouette_json") or metadata.get("source_json")
    if not source_pointer or project_file(project, source_pointer) != source["metadata"]:
        raise ValueError("Legacy engraving source differs from registered instance")
    image = project_file(project, metadata.get("output_png") or paths["engraving_png"])
    if image.parent != path.parent:
        raise ValueError("Legacy publication image outside its mode directory")
    return {"instance_id": binding["instance_id"], "metadata": path,
            "image": image, "record": metadata, "layout": "legacy-v1"}


def _safe_part(value):
    value = str(value or "").strip()
    value = re.sub(r"[^a-zA-Z0-9._(){} -]+", "_", value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"_+", "_", value)
    return value.strip(" ._") or "unknown"


def _safe_label(label):
    return re.sub(r"[^a-z0-9_]", "_", str(label or "").lower().strip()) or "unknown"


def label_folder_for_source(source_json, meta):
    return Path(source_json).parent.name or _safe_label(meta.get("label", "unknown"))


def engraving_dir_for_source(project_path, source_json, meta, mode="isolated"):
    stem = meta.get("filename_stem") or Path(meta.get("filename", "unknown")).stem
    return (Path(project_path) / "data/engravings/catalog" / _safe_part(meta.get("media_type", "movie"))
            / _safe_part(stem) / label_folder_for_source(source_json, meta) / Path(source_json).stem / mode)


def engraving_output_filename(source_json, meta, mode):
    stem = meta.get("filename_stem") or Path(meta.get("filename", "unknown")).stem
    title = re.sub(r"[{(][^})]*[})]", "", stem).strip()
    title = re.sub(r"[^a-zA-Z0-9]+", "_", title).strip("_").lower() or "unknown"
    frame = f"f{int(meta['frame']):06d}" if meta.get("frame") is not None else "f000000"
    return f"{title}-{frame}-{Path(source_json).stem}-{mode}.png"


def engraving_paths(project_path, source_json, meta, mode="isolated"):
    base = engraving_dir_for_source(project_path, source_json, meta, mode)
    return {"dir": base, "request": base / "request.json", "raw_png": base / "raw.png",
            "engraving_png": base / engraving_output_filename(source_json, meta, mode),
            "metadata": base / "engraving.json"}