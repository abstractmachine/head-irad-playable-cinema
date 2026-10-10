"""Read-only identity proposals; no allocation registry or migration executor."""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from data.media_id import build_shot_id, parse_shot_id


SLUG_VERSION = "unicode-nfc-lower-hyphen-v1"


def label_slug(label: str) -> str:
    normalized = unicodedata.normalize("NFC", label).lower().strip()
    return re.sub(r"[^\w]+", "-", normalized.replace("_", "-"), flags=re.UNICODE).strip("-")


def slug_collisions(labels) -> dict[str, list[str]]:
    groups = {}
    for label in labels:
        groups.setdefault(label_slug(label), set()).add(label)
    return {slug: sorted(values) for slug, values in sorted(groups.items()) if len(values) > 1 or not slug}


def canonical_shot(shot_id: str, media_id: str | None = None) -> str:
    parsed_media, start, end = parse_shot_id(shot_id)
    if end < start or not re.fullmatch(r"[A-Za-z0-9_.-]+", parsed_media):
        raise ValueError("Invalid bounded shot identity or unsafe media ID")
    if media_id and media_id != parsed_media:
        raise ValueError("Metadata media ID disagrees with shot ID")
    canonical = build_shot_id(parsed_media, start, end)
    if canonical != shot_id:
        raise ValueError("Shot ID is parseable but not canonically formatted")
    return canonical


def old_object_ordinal(object_id: str) -> int:
    match = re.fullmatch(r"object_(\d{4,})", object_id)
    if not match or int(match.group(1)) < 1:
        raise ValueError("Missing interpretable old filesystem ordinal")
    return int(match.group(1))


def tentative_instance_id(shot_id: str, object_id: str, media_id: str | None = None) -> str:
    return f"{canonical_shot(shot_id, media_id)}#{old_object_ordinal(object_id):04d}"


def proposed_directory(media_type: str, field: str, label: str, instance_id: str) -> str:
    if media_type not in {"movie", "gameplay"} or not re.fullmatch(r"[a-z][a-z0-9_]*", field):
        raise ValueError("Unknown media type or unsafe field component")
    slug = label_slug(label)
    if not slug or not re.fullmatch(r"[A-Za-z0-9_.@#-]+", instance_id):
        raise ValueError("Empty label slug or unsafe instance token")
    return f"data/silhouettes/{media_type}/{field}/{slug}/{instance_id}"


def normalize_reference(project: str | Path, value: str, relative_to: str | Path | None = None) -> str | None:
    value = value.replace("\\", "/")
    marker = "data/silhouettes/"
    offset = value.find(marker)
    if offset >= 0:
        suffix = value[offset:]
        components = suffix.split("/")
        if ".." in components:
            return None
        return "/".join(component for component in components if component not in {"", "."})
    if relative_to and not Path(value).is_absolute():
        base = Path(relative_to)
        try:
            candidate = Path(os.path.abspath(base / value)).relative_to(Path(project).absolute()).as_posix()
        except ValueError:
            return None
        return candidate if candidate.startswith(marker) else None
    return None


def path_length_summary(paths) -> dict:
    lengths = sorted(len(os.fsencode(path)) for path in paths)
    if not lengths:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    middle = len(lengths) // 2
    median = lengths[middle] if len(lengths) % 2 else (lengths[middle - 1] + lengths[middle]) / 2
    return {"count": len(lengths), "min": lengths[0], "median": median, "p95": lengths[max(0, (95 * len(lengths) + 99) // 100 - 1)], "max": lengths[-1]}


def plan_empty_catalog_parents(catalog_root: str | Path, removed_files=(), removed_trees=()) -> list[str]:
    root = Path(catalog_root).absolute()
    removed = {Path(path).absolute() for path in removed_files}
    trees = {Path(path).absolute() for path in removed_trees}
    if any(path == root or not path.is_relative_to(root) for path in removed | trees):
        raise ValueError("Removal simulation escapes the catalog boundary")
    removable = []
    for directory, subdirs, filenames in os.walk(root, topdown=False, followlinks=False):
        path = Path(directory)
        if path == root or path.is_symlink():
            continue
        if any((path / name) not in removed for name in filenames):
            continue
        if any((path / name) not in trees and (path / name) not in removable for name in subdirs):
            continue
        removable.append(path)
    return [str(path) for path in removable]