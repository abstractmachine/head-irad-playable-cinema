"""Adjacency-constrained hierarchical grouping over SEEDS superpixels (E4).

Deliberately distinct from the Ward clustering in the production palette:

* Ward groups *measured colours* into perceptual families regardless of where
  they occur in the frame.
* This groups *neighbouring pieces of the picture* into larger visual masses.
  Only spatially adjacent regions may ever merge.

The merge cost is the Ward increment computed in CIELAB, applied over a region
adjacency graph, so the two branches share a cost function but not a topology.
Nothing here is learned; it is the simplest defensible grouping that can be
inspected at several granularities.
"""

from __future__ import annotations

import heapq
import math

import numpy as np

from data.palette import _rgb_to_lab

DEFAULT_LEVELS = (12, 6, 3)


def _adjacency(labels: np.ndarray) -> dict[int, set[int]]:
    neighbours: dict[int, set[int]] = {}
    for a, b in (
        (labels[:, :-1].ravel(), labels[:, 1:].ravel()),
        (labels[:-1, :].ravel(), labels[1:, :].ravel()),
    ):
        differing = a != b
        for left, right in zip(a[differing], b[differing]):
            neighbours.setdefault(int(left), set()).add(int(right))
            neighbours.setdefault(int(right), set()).add(int(left))
    return neighbours


def build(
    arr_rgb: np.ndarray,
    labels: np.ndarray,
    levels: "tuple[int, ...]" = DEFAULT_LEVELS,
) -> dict:
    """Merge adjacent superpixels until each level is reached.

    Returns ``{"levels": {n: {"assignment": (H,W) int array, "regions": [...]}},
    "superpixel_count": int}``.  Region statistics are always measured from the
    original pixels of the merged region, never from averaged averages.
    """
    ids = [int(v) for v in np.unique(labels)]
    counts: dict[int, float] = {}
    lab_mean: dict[int, np.ndarray] = {}

    flat_labels = labels.ravel()
    flat_rgb = arr_rgb.reshape(-1, 3)
    order = np.argsort(flat_labels, kind="stable")
    sorted_labels = flat_labels[order]
    boundaries = np.searchsorted(sorted_labels, ids, side="left").tolist() + [len(sorted_labels)]

    for position, label in enumerate(ids):
        block = order[boundaries[position]:boundaries[position + 1]]
        pixels = flat_rgb[block]
        counts[label] = float(len(pixels))
        mean_rgb = pixels.astype(np.float64).mean(axis=0)
        lab_mean[label] = _rgb_to_lab(
            np.rint(mean_rgb).astype(np.uint8).reshape(1, 3)
        )[0].astype(np.float64)

    neighbours = _adjacency(labels)
    parent = {label: label for label in ids}
    version = {label: 0 for label in ids}

    def find(label: int) -> int:
        while parent[label] != label:
            parent[label] = parent[parent[label]]
            label = parent[label]
        return label

    def cost(a: int, b: int) -> float:
        delta = lab_mean[a] - lab_mean[b]
        weight = counts[a] * counts[b] / (counts[a] + counts[b])
        return float(weight * float(delta @ delta))

    heap: list = []
    for a, linked in neighbours.items():
        for b in linked:
            if a < b:
                heapq.heappush(heap, (cost(a, b), a, b, version[a], version[b]))

    targets = sorted({int(n) for n in levels}, reverse=True)
    snapshots: dict[int, dict[int, int]] = {}
    active = len(ids)

    while heap and active > min(targets):
        _, a, b, va, vb = heapq.heappop(heap)
        if parent[a] != a or parent[b] != b:
            continue
        if version[a] != va or version[b] != vb:
            continue

        total = counts[a] + counts[b]
        lab_mean[a] = (lab_mean[a] * counts[a] + lab_mean[b] * counts[b]) / total
        counts[a] = total
        parent[b] = a
        version[a] += 1

        merged = (neighbours.pop(b, set()) | neighbours.get(a, set())) - {a, b}
        resolved = set()
        for other in merged:
            root = find(other)
            if root != a:
                resolved.add(root)
                neighbours.setdefault(root, set()).discard(b)
                neighbours.setdefault(root, set()).add(a)
        neighbours[a] = resolved
        active -= 1

        for other in resolved:
            heapq.heappush(heap, (cost(a, other), *sorted((a, other)),
                                  version[min(a, other)], version[max(a, other)]))

        if active in targets:
            snapshots[active] = {label: find(label) for label in ids}

    # Any level not reached exactly (heap exhausted early) falls back to the
    # finest snapshot at or above it.
    for level in targets:
        if level not in snapshots:
            snapshots[level] = {label: find(label) for label in ids}

    result = {"superpixel_count": len(ids), "levels": {}}
    for level in sorted(snapshots, reverse=True):
        result["levels"][level] = _describe(arr_rgb, labels, snapshots[level])
    return result


def _describe(arr_rgb: np.ndarray, labels: np.ndarray, mapping: dict[int, int]) -> dict:
    """Relabel to 0..n-1 and measure each region from its own pixels."""
    roots = sorted({mapping[label] for label in mapping})
    index_of = {root: order for order, root in enumerate(roots)}

    lookup = np.zeros(int(labels.max()) + 1, dtype=np.int32)
    for label, root in mapping.items():
        lookup[label] = index_of[root]
    assignment = lookup[labels]

    height, width = labels.shape
    total = float(height * width)
    regions = []
    for index in range(len(roots)):
        mask = assignment == index
        pixels = arr_rgb[mask]
        if len(pixels) == 0:
            continue
        mean_rgb = np.rint(pixels.astype(np.float64).mean(axis=0)).astype(np.uint8)
        lab = _rgb_to_lab(mean_rgb.reshape(1, 3))[0]
        rows, cols = np.nonzero(mask)
        regions.append({
            "index": index,
            "rgb": mean_rgb.tolist(),
            "hex": "#{:02x}{:02x}{:02x}".format(*mean_rgb.tolist()),
            "lab": [round(float(v), 1) for v in lab],
            "lightness": round(float(lab[0]), 1),
            "chroma": round(float(math.hypot(lab[1], lab[2])), 1),
            "pixel_count": int(mask.sum()),
            "coverage": round(float(mask.sum()) / total, 4),
            "centroid": [round(float(cols.mean()) / width, 4),
                         round(float(rows.mean()) / height, 4)],
            "bbox": [int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())],
            "contiguous_pieces": int(_piece_count(mask)),
        })

    adjacency = _adjacency(assignment)
    for region in regions:
        region["adjacent"] = sorted(int(v) for v in adjacency.get(region["index"], set()))
    return {"assignment": assignment, "regions": regions}


def _piece_count(mask: np.ndarray) -> int:
    """Connected-component count, so a 'region' that is really scattered shows."""
    try:
        import cv2

        count, _ = cv2.connectedComponents(mask.astype(np.uint8), connectivity=8)
        return max(0, count - 1)
    except Exception:
        return -1


def render(arr_rgb: np.ndarray, level: dict, size: "tuple[int, int]"):
    """Paint every region in its own measured mean colour."""
    from PIL import Image

    assignment = level["assignment"]
    canvas = np.zeros((*assignment.shape, 3), dtype=np.uint8)
    for region in level["regions"]:
        canvas[assignment == region["index"]] = region["rgb"]
    return Image.fromarray(canvas, "RGB").resize(size, Image.Resampling.NEAREST)
