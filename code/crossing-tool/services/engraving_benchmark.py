"""Deterministic benchmark-candidate selection for engraving research.

Selects a heterogeneous *candidate pool* from the silhouette catalog so a human
can choose the final benchmark.  It proposes; it never accepts, never generates
an engraving, and never writes to the catalog or the Illustration index.

Selection prefilters ``services.illustration_index``'s SQLite metadata, then
optionally measures cached silhouette PNGs lazily. No video is decoded.

Two classification axes are added on top of the catalog's own taxonomy and are
recorded *beside* it, never instead of it:

``structural_group``
    The reconstruction problem an object poses.  The catalog's ``field`` has
    only five values (objects, wearing, action, animals, setting), which is far
    too coarse, so the grouping is derived from the actual label vocabulary.

``difficulty_stratum``
    How much structure a generator would have to invent, derived from the
    catalog's own ``occlusion`` / ``completeness`` / ``fragment_count`` /
    ``touches_frame`` signals.

``semantic_confidence`` is deliberately *not* used: across the active corpus it
spans only 0.3338-0.3460, so it cannot discriminate anything.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

CLEAN = "clean_control"
MODERATE = "moderate"
DIFFICULT = "difficult"
DIFFICULTY_STRATA = (CLEAN, MODERATE, DIFFICULT)

SMALL = "small"
MEDIUM = "medium"
LARGE = "large"
SCALE_STRATA = (SMALL, MEDIUM, LARGE)

# Boundaries taken from the active corpus's normalized mask-area deciles:
# deciles 1-4 end at 0.00715, deciles 5-8 end at 0.02573.
SCALE_BOUNDARIES = {"small_max": 0.00715, "medium_max": 0.02573}

# Label tokens -> structural group, derived from the most common real labels in
# each catalog field.  Matching is token-based so colour-prefixed variants
# ("brown hat", "black pants") fall into the same group as their base noun.
STRUCTURAL_LEXICON: dict[str, tuple[str, ...]] = {
    "animal": (
        "horse", "cattle", "donkey", "mule", "dog", "chicken", "bison", "cow",
        "sheep", "goat", "pig", "chick", "bull", "bird", "deer", "snake",
        "rabbit", "bear", "calf", "turkey", "cat", "buffalo", "ox", "duck",
        "lamb", "rooster", "wolf", "owl", "elk", "foal", "mustang", "pony",
    ),
    "human_body": ("hand", "hands", "face", "faces", "head", "arm", "leg", "foot", "feet"),
    "garment": (
        "hat", "coat", "shirt", "dress", "pant", "pants", "suit", "glove",
        "gloves", "belt", "tie", "uniform", "vest", "scarf", "suspender",
        "bandana", "boot", "boots", "apron", "poncho", "shawl", "cape", "skirt",
        "jacket", "collar", "cuff", "sombrero",
    ),
    "weapon": (
        "rifle", "gun", "pistol", "revolver", "shotgun", "knife", "holster",
        "sword", "dagger", "carbine", "winchester", "colt", "derringer",
        "cartridge", "bullet", "ammo", "ammunition", "bandolier", "whip",
    ),
    "furniture": (
        "chair", "chairs", "table", "bed", "bench", "desk", "stool", "cabinet",
        "shelf", "dresser", "wardrobe", "couch", "sofa", "counter", "piano",
    ),
    "container": (
        "barrel", "bottle", "bottles", "glass", "pot", "cup", "bowl", "sack",
        "box", "crate", "bag", "jug", "basket", "can", "plate", "bucket",
        "canteen", "flask", "keg", "jar", "trunk", "chest", "tin", "kettle",
    ),
    "vehicle": (
        "wagon", "cart", "carriage", "buggy", "stagecoach", "coach", "wheel",
        "train", "locomotive", "boat", "canoe", "sled", "buckboard",
    ),
    "tack_harness": (
        "saddle", "rope", "reins", "rein", "bridle", "harness", "stirrup",
        "lasso", "lariat", "saddlebag", "halter", "yoke", "spur", "spurs",
    ),
    "signage": ("sign", "poster", "banner", "notice", "placard", "signboard"),
    "architecture": (
        "window", "door", "building", "fence", "wall", "roof", "porch",
        "stairs", "gate", "railing", "post", "beam", "pillar", "chimney",
        "balcony", "archway", "bridge",
    ),
    "vegetation": (
        "tree", "log", "wood", "branch", "bush", "plant", "flower", "cactus",
        "grass", "shrub", "hay", "straw", "leaf", "leaves", "stump",
    ),
    "lighting_instrument": ("lamp", "lantern", "candle", "torch", "chandelier", "candlestick"),
    "paper_media": (
        "book", "books", "paper", "picture", "letter", "map", "newspaper",
        "card", "cards", "photograph", "document", "poster board", "envelope",
    ),
    "textile_furnishing": (
        "curtain", "cloth", "blanket", "rug", "flag", "tablecloth", "sheet",
        "towel", "drape", "banner cloth", "carpet", "pillow", "cushion",
    ),
    "environment": (
        "mountain", "camp", "field", "town", "cave", "rock", "ruin", "ruins",
        "hillside", "hill", "canyon", "river", "riverbank", "forest", "cliff",
        "desert", "corral", "yard", "snow",
    ),
}

STRUCTURAL_GROUPS = tuple(STRUCTURAL_LEXICON)

# Lightweight, overridable selection policy.  Thresholds live here rather than
# in the selection code so a benchmark run records exactly what produced it.
DEFAULT_BENCHMARK_CONFIG: dict = {
    "schema_version": "1",
    "media_type": "movie",
    "id_prefix": "bench-",
    # A pool for human review, not the final benchmark (12-20 cases).
    "target_candidates": 40,
    # Diversity guards: frequent labels and prolific films must not dominate.
    "max_per_label": 2,
    "max_per_film": 2,
    "max_per_cell": 3,
    # Decile 1 of the active corpus starts at 0.003; stay just above the floor
    # so candidates remain visually evaluable.
    "min_mask_area_fraction": 0.004,
    "structural_groups": list(STRUCTURAL_GROUPS),
    "difficulty_strata": list(DIFFICULTY_STRATA),
    # Proposal identity. Proposal 1's manifest and ids are never reused.
    "proposal": 1,
    # Visual viability runs BEFORE difficulty stratification, so a tiny or
    # fragmented object can never be admitted merely to fill a group quota.
    "apply_viability_gate": False,
    # Bound on how many silhouette PNGs may be measured during one proposal;
    # selection measures lazily and stops once quotas are filled.
    "max_measurements": 20000,
    # Curatorial, NOT a measured viability property. In the 40-candidate
    # review these groups were accepted 0/3, 0/3, 0/3, 0/3 and 0/2; pixel
    # metrics did not distinguish them, so the preference is recorded here
    # where it stays visible and adjustable rather than hidden in a threshold.
    "deprioritized_groups": [],
    "reference_only_paths": [],
    # Always included first, ahead of the stratified pool. The Companeros bird
    # is the existing single-frame qwen-local baseline; it is read only.
    "reference_cases": [
        "data/silhouettes/catalog/movie/Companeros (1970) {tmdb-61044}/bird/object_0002.json"
    ],
    "shot_evidence": {
        "roles": ["first", "best", "last", "adaptive"],
        # Sampling counts come from the annotator's own preferences; see
        # services.engraving_benchmark_evidence.
        "use_canonical_sampler": True,
        # This project's annotate prefs request 1 frame/shot, which is too thin
        # to judge temporal evidence. Raising the baseline changes only N; the
        # bucketing, spacing, clamping and dedup stay the canonical sampler's.
        "frames_per_shot": 6,
    },
}


def resolve_benchmark_config(config_path: str | Path | None = None, overrides: dict | None = None) -> dict:
    """Return the merged benchmark configuration."""
    import copy

    config = copy.deepcopy(DEFAULT_BENCHMARK_CONFIG)
    if config_path:
        path = Path(config_path)
        if not path.is_file():
            raise FileNotFoundError(f"Benchmark config not found: {path}")
        config.update(json.loads(path.read_text(encoding="utf-8")))
    if overrides:
        config.update({k: v for k, v in overrides.items() if v is not None})
    return config

# Catalog fields whose labels are verbs or events rather than depictable
# objects.  Only the body-part labels in "action" are usable.
_VERB_FIELDS = {"action"}

_TOKEN_TO_GROUP: dict[str, str] = {}
for _group, _tokens in STRUCTURAL_LEXICON.items():
    for _token in _tokens:
        _TOKEN_TO_GROUP.setdefault(_token, _group)


def classify_structural_group(field: str | None, label: str | None) -> str | None:
    """Return the structural group for a catalog object, or ``None``.

    ``None`` means "not confidently classifiable"; such objects are excluded
    from the candidate pool rather than being forced into a bucket.
    """
    text = str(label or "").strip().lower()
    if not text:
        return None

    tokens = [token for token in text.replace("-", " ").replace("_", " ").split() if token]
    if not tokens:
        return None

    # Prefer the most specific match: the last noun usually carries the type
    # ("brown cowboy hat" -> hat).
    for token in reversed(tokens):
        group = _TOKEN_TO_GROUP.get(token)
        if group:
            if str(field or "") in _VERB_FIELDS and group != "human_body":
                return None
            return group

    if str(field or "") == "animals":
        return "animal"
    return None


def difficulty_stratum(record: dict) -> str:
    """Return how much structure a generator would have to invent."""
    occlusion = str(record.get("occlusion") or "").lower()
    completeness = str(record.get("completeness") or "").lower()
    fragments = record.get("fragment_count")
    fragments = int(fragments) if isinstance(fragments, (int, float)) else 1

    if (
        (occlusion == "heavy" and completeness in {"cropped", "partial"})
        or fragments >= 4
        or bool(record.get("touches_frame"))
    ):
        return DIFFICULT
    if occlusion == "none" and completeness == "full" and fragments == 1:
        return CLEAN
    return MODERATE


def scale_stratum(fraction: float | None) -> str | None:
    """Return the size band for a normalized mask-area fraction."""
    if fraction is None:
        return None
    if fraction < SCALE_BOUNDARIES["small_max"]:
        return SMALL
    if fraction < SCALE_BOUNDARIES["medium_max"]:
        return MEDIUM
    return LARGE


def mask_area_fraction(record: dict) -> float | None:
    """Return mask area as a fraction of the frame, or ``None`` if unknowable.

    Normalized so objects from sources with different resolutions compare.
    """
    area = record.get("mask_area")
    size = record.get("frame_size")
    if not isinstance(area, (int, float)) or not isinstance(size, (list, tuple)) or len(size) < 2:
        return None
    try:
        total = float(size[0]) * float(size[1])
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return float(area) / total


def selection_reasons(candidate: dict) -> list[str]:
    """Return the explicit reasons this object entered the pool."""
    classification = candidate["classification"]
    evidence = candidate["evidence"]
    reasons = [
        f"structural_group = {classification['structural_group']}",
        f"difficulty_stratum = {classification['difficulty_stratum']}",
    ]
    if classification.get("scale_stratum"):
        reasons.append(f"scale_stratum = {classification['scale_stratum']}")
    for key in ("occlusion", "completeness", "fragment_count"):
        if evidence.get(key) is not None:
            reasons.append(f"{key} = {evidence[key]}")
    if evidence.get("mask_area_fraction") is not None:
        reasons.append(f"mask_area_fraction = {evidence['mask_area_fraction']:.5f}")
    if evidence.get("touches_frame"):
        reasons.append("touches_frame = true")
    return reasons


def _sort_key(row: dict) -> tuple:
    """Stable deterministic ordering key, independent of traversal order."""
    return (
        str(row.get("filename_stem") or ""),
        str(row.get("shot_id") or ""),
        int(row.get("frame") or 0),
        str(row.get("label") or ""),
        str(row.get("object_id") or ""),
    )


def _fetch_pool(project_path: str | Path, config: dict) -> tuple[list[dict], dict]:
    """Return every classifiable active silhouette, as plain records."""
    from services.illustration_index import index_path

    media_type = config["media_type"]
    database = index_path(project_path, "silhouettes", media_type)
    if not Path(database).is_file():
        raise FileNotFoundError(
            f"Silhouette index missing: {database}\n"
            f"Run: crossing index illustration --media {media_type}"
        )

    minimum = float(config["min_mask_area_fraction"])
    # Stage 1 is a pure metadata prefilter. When the viability gate is active
    # its metadata-derivable floors are pushed into SQL so the far more
    # expensive image measurement only ever sees plausible survivors.
    extra = ""
    params: list = []
    if config.get("apply_viability_gate"):
        from services.engraving_viability import DEFAULT_GATE

        extra = " AND cast(json_extract(payload,'$.mask_area') as real) >= ?"
        params.append(float(DEFAULT_GATE["min_mask_area_px"]))
        minimum = max(minimum, float(DEFAULT_GATE["min_mask_area_fraction"]))

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        active = connection.execute(
            "SELECT count(*) FROM records WHERE assignment_state = 'active'"
        ).fetchone()[0]
        metadata_survivors = 0
        rows = connection.execute(
            """
            SELECT payload FROM records
            WHERE assignment_state = 'active'
              AND json_extract(payload,'$.frame_size[0]') IS NOT NULL
            """
            + extra,
            params,
        )
        pool: list[dict] = []
        for (payload,) in rows:
            record = json.loads(payload)
            fraction = mask_area_fraction(record)
            if fraction is None or fraction < minimum:
                continue
            if config.get("apply_viability_gate"):
                bbox = record.get("bbox")
                if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                    if min(float(bbox[2]), float(bbox[3])) < DEFAULT_GATE["min_short_dimension_px"]:
                        continue
            metadata_survivors += 1
            group = classify_structural_group(record.get("field"), record.get("label"))
            if group is None:
                continue
            record["_group"] = group
            record["_difficulty"] = difficulty_stratum(record)
            record["_scale"] = scale_stratum(fraction)
            record["_fraction"] = fraction
            pool.append(record)
    finally:
        connection.close()

    pool.sort(key=_sort_key)
    return pool, {
        "active_silhouettes": active,
        "metadata_candidates": metadata_survivors,
        "classifiable_candidates": len(pool),
    }


def evaluate_proposal_policy(record: dict, config: dict) -> dict:
    """Return experimental sampling priority, never intrinsic viability."""
    lower = record.get("_group") in (config.get("deprioritized_groups") or [])
    return {
        "eligible": True,
        "priority": "fallback" if lower else "primary",
        "experimental": bool(config.get("deprioritized_groups")),
        "reasons": ["small-sample group preference; not visual-viability truth"] if lower else [],
    }


def _build_candidate(record: dict, index: int, config: dict) -> dict:
    """Return one machine-readable benchmark candidate record."""
    benchmark_id = f"{config['id_prefix']}{index:03d}"
    candidate = {
        "benchmark_id": benchmark_id,
        "review_state": "proposed",
        "source": {
            "media_type": record.get("media_type"),
            "filename": record.get("filename"),
            "filename_stem": record.get("filename_stem"),
            "media_id": record.get("media_id"),
            "shot_id": record.get("shot_id"),
            "frame": record.get("frame"),
            "field": record.get("field"),
            "label": record.get("label"),
            "object_id": record.get("object_id"),
            "record_path": str(record.get("path") or ""),
            "png": str(record.get("png") or ""),
        },
        "classification": {
            "structural_group": record["_group"],
            "difficulty_stratum": record["_difficulty"],
            "scale_stratum": record["_scale"],
        },
        "evidence": {
            "occlusion": record.get("occlusion"),
            "completeness": record.get("completeness"),
            "isolation": record.get("isolation"),
            "viewpoint": record.get("viewpoint"),
            "fragment_count": record.get("fragment_count"),
            "touches_frame": bool(record.get("touches_frame")),
            "mask_area": record.get("mask_area"),
            "frame_size": record.get("frame_size"),
            "mask_area_fraction": round(record["_fraction"], 6),
            # Recorded but never used for selection: it is near-constant corpus-wide.
            "semantic_confidence": record.get("semantic_confidence"),
            "engraving_score": record.get("engraving_score"),
        },
        "shot_evidence": None,
    }
    candidate["selected_because"] = selection_reasons(candidate)
    if config.get("apply_viability_gate"):
        candidate["proposal_policy"] = evaluate_proposal_policy(record, config)
    if record.get("_metrics"):
        candidate["viability"] = {
            "metrics": record["_metrics"],
            "gate": record.get("_viability"),
            # Reserved: a weak source frame may later be rescued by other
            # frames in its shot. Nothing sets this yet.
            "source_frame_viability": (record.get("_viability") or {}).get("viable"),
            "temporal_evidence_viability": None,
        }
    if record.get("_reference"):
        candidate["is_reference_case"] = True
        candidate["selected_because"].insert(0, "pinned reference case (existing baseline)")
    return candidate


def proposal_two_config(overrides: dict | None = None) -> dict:
    """Return the proposal-2 policy: viability gate first, then diversity."""
    config = resolve_benchmark_config(None, None)
    config.update({
        "proposal": 2,
        "id_prefix": "bench2-",
        "apply_viability_gate": True,
        "target_candidates": 36,
        "deprioritized_groups": [
            "human_body", "tack_harness", "weapon", "container", "environment",
        ],
        # Proposal 1's reference anchor is reported separately rather than
        # pinned, so proposal 2 demonstrates fresh corpus material.
        "reference_cases": [],
    })
    if overrides:
        config.update({k: v for k, v in overrides.items() if v is not None})
    return config


def _load_reference_records(project_path: str | Path, config: dict) -> list[dict]:
    """Return explicitly pinned reference objects, read straight from the catalog.

    These bypass stratification and quotas: they are existing experimental
    baselines, not samples. They are read-only.
    """
    records: list[dict] = []
    for relative in config.get("reference_cases") or []:
        path = Path(relative)
        if not path.is_absolute():
            path = Path(project_path) / path
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        record["path"] = str(path)
        record.setdefault("object_id", path.stem)
        fraction = mask_area_fraction(record)
        record["_group"] = classify_structural_group(record.get("field"), record.get("label")) or "unclassified"
        record["_difficulty"] = difficulty_stratum(record)
        record["_scale"] = scale_stratum(fraction)
        record["_fraction"] = fraction if fraction is not None else 0.0
        record["_reference"] = True
        records.append(record)
    return records


def propose_candidates(project_path: str | Path, config: dict) -> dict:
    """Return a deterministic, stratified candidate pool.

    Fills one (group, difficulty) cell at a time in a fixed order, honouring
    per-label and per-film caps so frequent labels and prolific films cannot
    dominate.  No randomness is involved.
    """
    pool, funnel = _fetch_pool(project_path, config)

    from services.engraving_viability import canonical_record_path

    reference_only = {
        canonical_record_path(project_path, path)
        for path in config.get("reference_only_paths") or []
    }
    before_references = len(pool)
    pool = [
        record for record in pool
        if canonical_record_path(project_path, str(record.get("path") or "")) not in reference_only
    ]
    funnel["reference_only_excluded"] = before_references - len(pool)

    references = _load_reference_records(project_path, config)
    reference_keys = {
        (str(r.get("filename_stem")), str(r.get("label")), str(r.get("object_id")))
        for r in references
    }
    # A reference case must never also be drawn from the stratified pool.
    pool = [
        r for r in pool
        if (str(r.get("filename_stem")), str(r.get("label")), str(r.get("object_id")))
        not in reference_keys
    ]

    by_cell: dict[tuple[str, str], list[dict]] = {}
    for record in pool:
        by_cell.setdefault((record["_group"], record["_difficulty"]), []).append(record)

    per_label = int(config["max_per_label"])
    per_film = int(config["max_per_film"])
    per_cell = int(config["max_per_cell"])
    target = int(config["target_candidates"])

    label_counts: dict[str, int] = {}
    film_counts: dict[str, int] = {}
    chosen: list[dict] = []

    for record in references:
        label_counts[str(record.get("label") or "")] = 1
        film_counts[str(record.get("filename_stem") or "")] = 1

    target = max(0, target - len(references))

    # Viability is a gate applied here, during selection, so a nonviable
    # object can never be admitted to fill an empty structural-group cell.
    # Measurement is lazy and bounded: only candidates actually reached by the
    # round-robin get their PNG read.
    gate_on = bool(config.get("apply_viability_gate"))
    budget = int(config.get("max_measurements", 0)) if gate_on else 0
    stats = {
        "evaluated": 0, "measured": 0, "cache_reused": 0,
        "gate_pass": 0, "gate_fail": 0, "policy_eligible": 0,
        "budget_exhausted": False,
    }

    def _is_viable(record: dict) -> bool:
        nonlocal budget
        if not gate_on:
            return True
        if "_viability" in record:
            return record["_viability"]["viable"]
        from services.engraving_viability import (
            evaluate_gate,
            load_cached_metrics,
            measure_silhouette,
            silhouette_png_for_record,
            store_cached_metrics,
        )

        key = str(record.get("path") or "")
        cached = load_cached_metrics(project_path, [key], config["media_type"]) if key else {}
        metrics = cached.get(key)
        if metrics is None:
            if budget <= 0:
                stats["budget_exhausted"] = True
                return False
            png = silhouette_png_for_record(project_path, {"record_path": key, "png": record.get("png")})
            metrics = measure_silhouette(png) if png else None
            budget -= 1
            stats["measured"] += 1
            if metrics and key:
                store_cached_metrics(project_path, {key: metrics}, config["media_type"])
        else:
            stats["cache_reused"] += 1

        verdict = evaluate_gate(metrics, {"mask_area_fraction": record.get("_fraction")})
        record["_viability"] = verdict
        record["_metrics"] = metrics
        stats["evaluated"] += 1
        if verdict["viable"]:
            stats["gate_pass"] += 1
            stats["policy_eligible"] += int(evaluate_proposal_policy(record, config)["eligible"])
        else:
            stats["gate_fail"] += 1
        return verdict["viable"]

    groups = [g for g in config["structural_groups"] if g in STRUCTURAL_GROUPS]
    # Round-robin across cells so no single group or stratum fills the pool
    # before the others are considered.
    lower = set(config.get("deprioritized_groups") or [])
    for priority_groups in ([group for group in groups if group not in lower],
                            [group for group in groups if group in lower]):
        for round_index in range(per_cell):
            for difficulty in config["difficulty_strata"]:
                for group in priority_groups:
                    if len(chosen) >= target:
                        break
                    bucket = by_cell.get((group, difficulty)) or []
                    for record in bucket:
                        if record.get("_chosen"):
                            continue
                        label = str(record.get("label") or "")
                        film = str(record.get("filename_stem") or "")
                        if label_counts.get(label, 0) >= per_label:
                            continue
                        if film_counts.get(film, 0) >= per_film:
                            continue
                        if not _is_viable(record):
                            continue
                        record["_chosen"] = True
                        label_counts[label] = label_counts.get(label, 0) + 1
                        film_counts[film] = film_counts.get(film, 0) + 1
                        chosen.append(record)
                        break

    chosen.sort(key=lambda r: (r["_group"], r["_difficulty"], _sort_key(r)))
    ordered = references + chosen
    candidates = [_build_candidate(record, i, config) for i, record in enumerate(ordered, start=1)]

    return {
        "schema_version": "1",
        "proposal": int(config.get("proposal", 1)),
        "config": config,
        "pool_size": len(pool),
        "reference_count": len(references),
        "candidate_count": len(candidates),
        "filtering_funnel": {**funnel, "selected": len(candidates)},
        "viability": {
            "applied": gate_on,
            **stats,
        },
        "candidates": candidates,
    }
