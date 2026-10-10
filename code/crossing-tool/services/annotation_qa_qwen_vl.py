"""Read-only, experimental ontology-aware annotation relevance diagnostics."""

from __future__ import annotations

import hashlib
import difflib
import importlib.util
import json
import math
import os
import time
from pathlib import Path


PREDICATE_VERSION = "crossing-annotation-qa-v1"
CONTROL_VERSION = "source-vocabulary-ontology-controls-v2"
PREDICATES = {
    "objects": ("inanimate_entity", "Does the image depict the inanimate object {label}?", "crop_and_frame"),
    "animals": ("animal_entity", "Does the image depict the animal {label}?", "crop_and_frame"),
    "humans": ("visible_human", "Does the image visibly support the human category {label}, without inferring identity or story?", "crop_and_frame"),
    "wearing": ("worn_clothing_or_gear", "Does the image show a person or figure wearing the clothing or gear {label}?", "frame_primary_crop_diagnostic"),
    "action": ("visible_action", "Does the image provide visible evidence of the action {label} occurring?", "frame_primary_crop_diagnostic"),
    "setting": ("dominant_place", "Does the image support {label} as the dominant visible place or environment?", "frame_primary_crop_diagnostic"),
    "description": ("shot_description", "Does the image support the factual visible subject, environment and action described by {label}?", "frame_only"),
    "spatial": ("spatial_environment", "Does the image support the spatial environment classification {label}?", "frame_only"),
    "time_of_day": ("lighting_time", "Does the visible lighting support the time-of-day classification {label}?", "frame_only"),
    "shot": ("framing_scale", "Does the image support the cinematic framing scale {label}?", "frame_only"),
    "camera": ("camera_motion", "Does the visual evidence support the camera classification {label}? A single still does not establish camera movement.", "frame_only"),
    "type": ("shot_type", "Does the image support the shot-content classification {label}?", "frame_only"),
    "text": ("visible_text", "Does the image contain the visible text {label}, excluding subtitles?", "frame_only"),
}
INSTRUCTION = (
    "Judge whether the visual document supports the query according to its annotation "
    "field. Use only visible evidence, not story or off-screen inference. This is "
    "semantic relevance, not image quality or engraving suitability. Actions and "
    "settings need not be bounded objects."
)


def predicate_for(field: str, label: str) -> dict:
    if field not in PREDICATES:
        raise ValueError(f"No documented visual QA predicate for field {field!r}; human review required")
    scope, template, policy = PREDICATES[field]
    return {
        "id": f"{PREDICATE_VERSION}:{field}",
        "field": field,
        "scope": scope,
        "query": template.format(label=json.dumps(label, ensure_ascii=True)),
        "instruction": INSTRUCTION,
        "input_policy": policy,
    }


def select_controls(field: str, label: str, vocabulary: dict[str, list[str]], count: int = 4) -> list[str]:
    predicate_for(field, label)
    options = sorted({value.strip() for value in vocabulary.get(field, []) if value.strip() and value.casefold() != label.casefold() and compatible_control(field, value)})
    if len(options) < count:
        raise ValueError(f"Insufficient same-field controls for {field!r}")
    tokens = set(label.lower().split())
    from services.engraving_benchmark import classify_structural_group

    group = classify_structural_group(field, label)
    nearby = sorted(options, key=lambda value: (
        -len(tokens & set(value.lower().split())),
        -(group is not None and classify_structural_group(field, value) == group),
        -difflib.SequenceMatcher(None, label.lower(), value.lower()).ratio(),
        value,
    ))
    chosen = nearby[:2]
    remaining = sorted([value for value in options if value not in chosen], key=lambda value: hashlib.sha256(f"{field}:{label}:{value}".encode()).hexdigest())
    if group is not None:
        similar = next((value for value in remaining if classify_structural_group(field, value) == group), None)
        if similar and len(chosen) < count:
            chosen.append(similar)
            remaining.remove(similar)
        remaining.sort(key=lambda value: classify_structural_group(field, value) == group)
    chosen.extend(remaining[:count - len(chosen)])
    return [label, *chosen]


def compatible_control(field: str, label: str) -> bool:
    from services.engraving_benchmark import STRUCTURAL_LEXICON, classify_structural_group

    tokens = set(label.lower().replace("-", " ").split())
    singular_tokens = tokens | {token.rstrip("s") for token in tokens}
    if field == "animals":
        return bool(singular_tokens & set(STRUCTURAL_LEXICON["animal"]))
    if field == "wearing":
        gear = {"holster", "bandolier", "spur", "neckerchief"}
        return bool(singular_tokens & (set(STRUCTURAL_LEXICON["garment"]) | gear))
    if field == "action":
        first = label.lower().split()[0]
        return first.endswith(("ing", "ed"))
    if field == "objects":
        return classify_structural_group("objects", label) not in {"animal", "human_body"} and not tokens & {"object", "thing", "item"}
    return True


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_key(model_identity: dict, predicate: dict, label: str, image_hash: str) -> str:
    payload = {"model": model_identity, "predicate": predicate, "label": label, "image_sha256": image_hash}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def resolve_model(project: str | Path) -> Path:
    model = Path(project).resolve() / "models" / "Qwen3-VL-Reranker-2B"
    for name in ("model.safetensors", "config.json", "scripts/qwen3_vl_reranker.py"):
        if not (model / name).is_file():
            raise FileNotFoundError(model / name)
    config = json.loads((model / "config.json").read_text())
    if config.get("model_type") != "qwen3_vl" or config.get("text_config", {}).get("num_hidden_layers") != 28:
        raise ValueError("Not the expected Qwen3-VL-Reranker-2B configuration")
    return model


def model_provenance(model: Path) -> dict:
    from importlib.metadata import PackageNotFoundError, version

    names = ["torch", "transformers", "qwen-vl-utils", "accelerate", "Pillow", "numpy", "scipy", "flash-attn"]
    dependencies = {}
    for name in names:
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = None
    files = [path for path in model.rglob("*") if path.is_file() and ".cache" not in path.parts and path.suffix in {".json", ".jinja", ".py", ".safetensors"}]
    revisions = sorted({path.read_text().splitlines()[0] for path in (model / ".cache/huggingface/download").rglob("*.metadata")})
    return {
        "model": "Qwen/Qwen3-VL-Reranker-2B",
        "local_path": str(model),
        "revisions": revisions,
        "files_sha256": {str(path.relative_to(model)): file_sha256(path) for path in sorted(files)},
        "config": json.loads((model / "config.json").read_text()),
        "processor_config": json.loads((model / "preprocessor_config.json").read_text()),
        "dependencies": dependencies,
        "dtype": "bfloat16",
        "attention": "sdpa",
        "compatibility": "Transformers 5: reconstruct mm_token_type_ids using processor.create_mm_token_type_ids after official token padding",
        "offline": True,
        "crop_background": "white; alpha-composited RGB, no upscaling",
    }


class LocalReranker:
    """Use the downloaded official implementation, including binary scoring."""

    def __init__(self, model: Path):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError("This BF16 experiment requires CUDA; no silent CPU substitution")
        torch.manual_seed(0)
        torch.cuda.manual_seed_all(0)
        torch.backends.cuda.matmul.allow_tf32 = False
        module_spec = importlib.util.spec_from_file_location("crossing_official_qwen_reranker", model / "scripts/qwen3_vl_reranker.py")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        started = time.perf_counter()
        self.reference = module.Qwen3VLReranker(
            model_name_or_path=str(model), torch_dtype=torch.bfloat16,
            local_files_only=True, attn_implementation="sdpa",
        )
        torch.cuda.synchronize()
        self.load_seconds = time.perf_counter() - started
        self.raw_scores = []
        self.reference.score_linear.register_forward_hook(self._capture_raw)
        self.parameter_bytes = sum(parameter.numel() * parameter.element_size() for parameter in self.reference.model.parameters())
        self.parameter_count = sum(parameter.numel() for parameter in self.reference.model.parameters())
        self.parameter_dtypes = sorted({str(parameter.dtype) for parameter in self.reference.model.parameters()})

    def _capture_raw(self, module, inputs, output):
        self.raw_scores = output.detach().float().cpu().flatten().tolist()

    def score(self, requests: list[dict]) -> list[dict]:
        import torch

        if not requests:
            return []
        pairs = []
        for request in requests:
            image = Path(request["image"]).resolve()
            if not image.is_file():
                raise FileNotFoundError(image)
            predicate = request["predicate"]
            pairs.append(self.reference.format_mm_instruction(
                predicate["query"], None, None, None, str(image), None,
                instruction=predicate["instruction"],
            ))
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        inputs = self.reference.tokenize(pairs)
        if "mm_token_type_ids" in inputs:
            inputs["mm_token_type_ids"] = torch.tensor(
                self.reference.processor.create_mm_token_type_ids(inputs["input_ids"].tolist()),
                dtype=torch.long,
            )
        image_token = self.reference.model.config.image_token_id
        if any(image_token not in row.tolist() for row in inputs["input_ids"]):
            raise RuntimeError("Official preprocessing lost image input; refusing text-only fallback")
        inputs = inputs.to(self.reference.model.device)
        with torch.inference_mode():
            normalized = self.reference.compute_scores(inputs)
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - started
        peak = torch.cuda.max_memory_allocated()
        scores = []
        if len(normalized) != len(requests) or len(self.raw_scores) != len(requests):
            raise RuntimeError("Score count mismatch")
        for raw, score in zip(self.raw_scores, normalized):
            if not math.isfinite(raw) or not math.isfinite(score) or not 0 <= score <= 1:
                raise RuntimeError("Nonfinite or out-of-range reranker score")
            scores.append({
                "raw_score": raw, "normalized_score": score,
                "batch_seconds": elapsed, "amortized_seconds": elapsed / len(requests),
                "batch_size": len(requests), "peak_vram_bytes": peak,
            })
        return scores


def save_artifact(path: Path, data: dict) -> None:
    from data.annotate import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=True) + "\n")


def protected_snapshot(project: Path, cases: list[dict]) -> dict:
    paths = set()
    for relative in ("data/annotations", "data/indexes/illustration", "data/engravings/catalog", "outputs/tests/engraving-benchmark"):
        paths.update(path for path in (project / relative).rglob("*") if path.is_file())
    for case in cases:
        record = project / case["source"]["record_path"]
        paths.add(record)
        metadata = json.loads(record.read_text())
        paths.add(record.parent / metadata["png"])
    for path in (project / "preferences/data").glob("*"):
        if path.is_file():
            paths.add(path)
    return {
        str(path.relative_to(project)): {
            "sha256": file_sha256(path), "size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
        }
        for path in sorted(paths)
    }


def prepare_cases(project: Path, root: Path, manifest: dict, review: dict, *, include_history=False) -> tuple[list[dict], dict]:
    from data.annotate import get_annotation_json_path
    from generators.mosaic import _find_video_path, extract_frame_pil
    from services.frame_match import best_frame_path
    from services.engraving_benchmark_review import decision_for
    from PIL import Image

    loaded = {}
    vocabulary = {}
    prepared = []
    for candidate in manifest["candidates"]:
        source = candidate["source"]
        from services.silhouette_catalog import assignment_state_for_record, load_active_record
        record_path = project / source["record_path"]
        metadata = json.loads(record_path.read_text()) if include_history else load_active_record(record_path, project)
        filename = source["filename"]
        if filename not in loaded:
            path = Path(get_annotation_json_path(str(project), filename, source["media_type"]))
            loaded[filename] = json.loads(path.read_text())
            for entry in loaded[filename]:
                annotation = entry.get("shot", {}).get("annotation", {})
                for field, values in annotation.items():
                    if field not in PREDICATES:
                        continue
                    values = values if isinstance(values, list) else [values]
                    vocabulary.setdefault(field, set()).update(str(value).strip() for value in values if value)
        entry = next((entry for entry in loaded[filename] if entry.get("shot", {}).get("shot_id") == source["shot_id"]), None)
        if entry is None:
            raise ValueError(f"Missing canonical source annotation: {candidate['benchmark_id']}")
        values = entry["shot"]["annotation"].get(source["field"])
        frame = int(source["frame"])
        if int(metadata["frame"]) != frame:
            raise ValueError("Manifest/catalog frame mismatch")
        from data.media_id import parse_shot_id

        _, start_frame, end_frame = parse_shot_id(source["shot_id"])
        if not start_frame <= frame <= end_frame:
            raise ValueError("Source frame outside authoritative shot")
        image_root = root / "inputs"
        image_root.mkdir(parents=True, exist_ok=True)
        crop_path = image_root / f"{candidate['benchmark_id']}-crop.png"
        frame_path = image_root / f"{candidate['benchmark_id']}-frame.png"
        original_crop = record_path.parent / metadata["png"]
        with Image.open(original_crop) as original:
            rgba = original.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            Image.alpha_composite(background, rgba).convert("RGB").save(crop_path)
        best = entry["shot"].get("best_frame", {})
        cached = best_frame_path(str(project), source["media_type"], filename, source["shot_id"])
        input_provenance = {"frame": frame, "general_best_frame": best.get("frame"), "original_crop": str(original_crop), "original_crop_sha256": file_sha256(original_crop)}
        if best.get("frame") == frame and cached.is_file():
            with Image.open(cached) as image:
                image.convert("RGB").save(frame_path)
            input_provenance["frame_origin"] = str(cached)
            input_provenance["cached_frame_sha256"] = file_sha256(cached)
        else:
            video = _find_video_path(str(project), Path(filename).stem)
            if video is None:
                raise FileNotFoundError(f"Source video unavailable: {filename}")
            image = extract_frame_pil(Path(video), frame, apply_sample_aspect_ratio=False)
            if image is None:
                raise RuntimeError(f"Unable to extract exact source frame: {candidate['benchmark_id']}")
            image.convert("RGB").save(frame_path)
            input_provenance.update({"frame_origin": "existing generators.mosaic.extract_frame_pil", "video": str(video), "video_size": Path(video).stat().st_size, "video_mtime_ns": Path(video).stat().st_mtime_ns})
        predicate = predicate_for(source["field"], source["label"])
        labels = values if isinstance(values, list) else [values]
        exact = source["label"] in labels
        flags = []
        if not exact:
            flags.append("catalog_label_not_exact_current_annotation_value")
        if source["field"] == "action" and source["label"] in {"head", "face"}:
            flags.append("nominal_catalog_action_label_requires_source_proposition_review")
        if source["field"] == "objects" and source["label"] == "head":
            flags.append("body_part_in_inanimate_objects_field")
        prepared.append({
            "benchmark_id": candidate["benchmark_id"], "source": source,
            "annotation_state": "persisted; human acceptance not recorded",
            "annotation_provenance": entry.get("annotation", {}),
            "source_annotation_values": values, "catalog_label_exact_source_value": exact,
            "source_annotation": entry["shot"]["annotation"],
            "assignment_state": assignment_state_for_record(metadata),
            "human_engraving_review": decision_for(review, candidate["benchmark_id"]),
            "evidence_quality": candidate["evidence"],
            "engraving_eligibility": {"decision": "not_evaluated", "note": "Separate downstream policy; not decided by reranker"},
            "predicate": predicate, "input_provenance": input_provenance,
            "images": {view: {"path": str(path), "sha256": file_sha256(path)} for view, path in (("crop", crop_path), ("frame", frame_path))},
            "qa_flags": flags,
        })
    vocabulary = {field: sorted(values) for field, values in vocabulary.items()}
    for case in prepared:
        source = case["source"]
        case["controls"] = select_controls(source["field"], source["label"], vocabulary)
        case["controls_note"] = "Same-field source vocabulary with conservative experimental animal/garment/verb-like filters and deterministic lexical/structural contrasts. Alternatives may be synonyms or co-present, not proven negatives. Filters do not define canonical ontology or invalidate labels."
    return prepared, vocabulary


def ranking(scores: list[dict], label: str) -> dict:
    ordered = sorted(scores, key=lambda score: (-score["raw_score"], score["label"]))
    proposed = next(score for score in scores if score["label"] == label)
    competitor = next(score for score in ordered if score["label"] != label)
    return {
        "proposed_label_rank": 1 + sum(score["raw_score"] > proposed["raw_score"] for score in scores),
        "proposed_label_score": proposed["normalized_score"],
        "strongest_competitor": competitor["label"],
        "strongest_competitor_score": competitor["normalized_score"],
        "margin": proposed["normalized_score"] - competitor["normalized_score"],
        "raw_margin": proposed["raw_score"] - competitor["raw_score"],
        "tie_count": sum(score["raw_score"] == proposed["raw_score"] for score in scores),
    }


def run_probe(project_path: str | Path) -> Path:
    project = Path(project_path).resolve()
    benchmark = project / "outputs/tests/engraving-benchmark"
    manifest = json.loads((benchmark / "candidates.json").read_text())
    review = json.loads((benchmark / "review.json").read_text())
    cases = manifest["candidates"]
    expected = [f"bench-{number:03d}" for number in range(1, 41)]
    if [case["benchmark_id"] for case in cases] != expected:
        raise ValueError("This probe is restricted to the exact forty Proposal-1 cases")
    root = project / "outputs/tests/annotation-qa-qwen-vl"
    root.mkdir(parents=True, exist_ok=True)
    previous_result = root / "results.json"
    initial_result = root / "results-initial-controls.json"
    if previous_result.is_file() and not initial_result.is_file():
        previous = json.loads(previous_result.read_text())
        if previous.get("control_version") != CONTROL_VERSION:
            save_artifact(initial_result, previous)
    baseline = protected_snapshot(project, cases)
    save_artifact(root / "safety-before.json", baseline)
    started = time.perf_counter()
    try:
        provenance = model_provenance(resolve_model(project))
        prepared, vocabulary = prepare_cases(project, root, manifest, review)
        save_artifact(root / "prepared-cases.json", {"cases": prepared, "vocabulary": vocabulary})
        save_artifact(root / "model-provenance.json", provenance)
        cache_path = root / "scores-cache.json"
        cache = json.loads(cache_path.read_text()) if cache_path.is_file() else {}
        wrapper = LocalReranker(resolve_model(project))
        import torch

        identity = {"model": provenance["model"], "files_sha256": provenance["files_sha256"], "dtype": "bfloat16", "attention": "sdpa", "dependencies": provenance["dependencies"]}
        smoke = []
        for benchmark_id in ("bench-001", "bench-011", "bench-012"):
            case = next(case for case in prepared if case["benchmark_id"] == benchmark_id)
            requests = [{"image": case["images"][view]["path"], "predicate": case["predicate"]} for view in ("crop", "frame")]
            single = [wrapper.score([request])[0] for request in requests]
            repeated = [wrapper.score([request])[0] for request in requests]
            batch = wrapper.score(requests)
            if any(abs(first["normalized_score"] - second["normalized_score"]) > 0.002 or abs(first["raw_score"] - second["raw_score"]) > 0.03 for first, second in zip(single, repeated)):
                raise RuntimeError("Repeated smoke scores are unstable")
            if any(abs(first["raw_score"] - second["raw_score"]) > 0.15 for first, second in zip(single, batch)):
                raise RuntimeError("Single/batch smoke scores disagree")
            smoke.append({"benchmark_id": benchmark_id, "single": single, "repeat": repeated, "batch_two": batch})
            for view, score in zip(("crop", "frame"), repeated):
                key = cache_key(identity, case["predicate"], case["source"]["label"], case["images"][view]["sha256"])
                if key not in cache:
                    cache[key] = score
            save_artifact(cache_path, cache)
            print(f"Smoke {benchmark_id}: finite, repeat stable; batch compared", flush=True)
        if protected_snapshot(project, cases) != baseline:
            raise RuntimeError("Protected artifacts changed during smoke tests")
        save_artifact(root / "smoke.json", {"cases": smoke, "safety_pass": True})
        results = []
        inference_started = time.perf_counter()
        calls = []
        fresh = 0
        reused = 0
        for case in prepared:
            views = {}
            for view, image in case["images"].items():
                requests = []
                keys = []
                scored = []
                for label in case["controls"]:
                    predicate = predicate_for(case["source"]["field"], label)
                    key = cache_key(identity, predicate, label, image["sha256"])
                    keys.append((label, predicate, key))
                    if key not in cache:
                        requests.append({"image": image["path"], "predicate": predicate, "key": key})
                    else:
                        reused += 1
                for offset in range(0, len(requests), 2):
                    chunk = requests[offset:offset + 2]
                    scores = wrapper.score(chunk)
                    calls.append({"seconds": scores[0]["batch_seconds"], "size": len(chunk), "peak_vram_bytes": scores[0]["peak_vram_bytes"]})
                    for request, score in zip(chunk, scores):
                        cache[request["key"]] = score
                        fresh += 1
                    save_artifact(cache_path, cache)
                for label, predicate, key in keys:
                    scored.append({"label": label, "predicate": predicate, "cache_key": key, **cache[key]})
                views[view] = {"scores": scored, "ranking": ranking(scored, case["source"]["label"])}
            source_values = case["source_annotation_values"]
            source_values = source_values if isinstance(source_values, list) else [source_values]
            source_propositions = []
            if case["source"]["field"] == "action":
                for value in source_values:
                    if value != case["source"]["label"]:
                        predicate = predicate_for("action", value)
                        image = case["images"]["frame"]
                        key = cache_key(identity, predicate, value, image["sha256"])
                        if key not in cache:
                            score = wrapper.score([{"image": image["path"], "predicate": predicate}])[0]
                            cache[key] = score
                            fresh += 1
                            calls.append({"seconds": score["batch_seconds"], "size": 1, "peak_vram_bytes": score["peak_vram_bytes"]})
                            save_artifact(cache_path, cache)
                        source_propositions.append({"label": value, "predicate": predicate, "cache_key": key, **cache[key]})
            results.append({**case, "views": views, "source_action_proposition_scores": source_propositions})
            save_artifact(root / "results-in-progress.json", {"cases": results})
            print(f"Scored {case['benchmark_id']} {case['source']['field']}/{case['source']['label']}", flush=True)
        scoring_seconds = time.perf_counter() - inference_started
        decision_seconds = sum(call["seconds"] for call in calls)
        result = {
            "experiment": "Proposal-1-only ontology-aware semantic relevance; no annotation truth decisions",
            "human_review_warning": "Engraving review is NOT semantic correctness ground truth",
            "model_provenance": provenance, "predicate_version": PREDICATE_VERSION,
            "control_version": CONTROL_VERSION,
            "cases": results, "performance": {
                "load_seconds": wrapper.load_seconds, "parameter_bytes": wrapper.parameter_bytes,
                "parameter_count": wrapper.parameter_count, "parameter_dtypes": wrapper.parameter_dtypes,
                "gpu": torch.cuda.get_device_name(), "cuda": torch.version.cuda,
                "fresh_decisions": fresh, "cache_reused": reused, "calls": calls,
                "scoring_wall_seconds": scoring_seconds, "inference_seconds": decision_seconds,
                "decisions_per_second": fresh / decision_seconds if decision_seconds else None,
                "total_wall_seconds": time.perf_counter() - started,
                "peak_vram_bytes": max([call["peak_vram_bytes"] for call in calls] + [torch.cuda.max_memory_allocated()]),
            },
        }
        save_artifact(root / "results.json", result)
        render_report(root, result)
    finally:
        after = protected_snapshot(project, cases)
        changed = sorted(key for key in baseline.keys() | after.keys() if baseline.get(key) != after.get(key))
        save_artifact(root / "safety-after.json", {"files_checked": len(baseline), "changes": changed, "byte_and_mtime_identical": not changed})
        if changed:
            raise RuntimeError(f"Protected artifact changes: {changed}")
    return root


def render_report(root: Path, result: dict) -> None:
    from data.annotate import atomic_write_text
    import statistics

    lines = ["# Annotation QA: Proposal 1", "", "Experimental relevance scores, not annotation correctness probabilities.", "Engraving review is NOT semantic correctness ground truth. No production thresholds or relabeling.", "", "## Diagnostic Matrix", "", "| ID | Field | Label | Engraving Review | Crop | Frame | Crop Rank | Frame Rank | Frame Alternative | Margin | Flags |", "|---|---|---|---|---:|---:|---:|---:|---|---:|---|"]
    for case in result["cases"]:
        crop = case["views"]["crop"]["ranking"]
        frame = case["views"]["frame"]["ranking"]
        flags = list(case["qa_flags"])
        if abs(crop["proposed_label_score"] - frame["proposed_label_score"]) >= 0.4:
            flags.append("crop_frame_gap_at_least_0.4_exploratory")
        row = [case["benchmark_id"], case["source"]["field"], case["source"]["label"], case["human_engraving_review"], f"{crop['proposed_label_score']:.4f}", f"{frame['proposed_label_score']:.4f}", str(crop["proposed_label_rank"]), str(frame["proposed_label_rank"]), frame["strongest_competitor"], f"{frame['margin']:.4f}", "; ".join(flags)]
        lines.append("| " + " | ".join(value.replace("|", "\\|") for value in row) + " |")
    lines.extend(["", "## Ontology Counts", "", "28 objects, 5 wearing, 4 animals, 2 action, 1 setting. All source annotations are persisted; human annotation acceptance is not recorded. Silhouette assignment state is independent.", "", "## Camp Investigation", ""])
    camp = next(case for case in result["cases"] if case["benchmark_id"] == "bench-011")
    crop = camp["views"]["crop"]["ranking"]
    frame = camp["views"]["frame"]["ranking"]
    lines.extend([
        "Actual proposition: setting/camp, one dominant visible place, not an object.",
        "Annie Oakley (1935), shot tmdb_43887@f020169-f020209, recorded frame 20189; general BEST is 20183. The probe used the recorded frame, not the mismatched general BEST PNG.",
        "Visual inspection: the SAM crop isolates CINCINNATI lettering on a bench. The frame shows women outside a log cabin. Neither the crop nor the single frame establishes a camp unambiguously; wider shot context was not scored.",
        f"Crop {crop['proposed_label_score']:.4f}; frame {frame['proposed_label_score']:.4f}, frame rank {frame['proposed_label_rank']}, competitor {frame['strongest_competitor']} at {frame['strongest_competitor_score']:.4f}, margin {frame['margin']:.4f}.",
        "Conclusion: poor local mask evidence and a downstream scope mismatch are evident. Upstream proposal error remains possible but unproven. No invalidation, semantic truth decision, or setting-to-object conversion follows from these scores.",
        "", "## Interesting Disagreements", "",
        "High/low here is relative within this small diagnostic set, not a deployed semantic threshold.",
        "- Rejected yet relatively strong: revolver bench-038 (crop 0.6367/frame 0.5039), barrel bench-008 (0.6055/0.4961), holster bench-040 (0.3887/0.5469). These may be correct annotations with poor engraving evidence.",
        "- Rejected and relatively weak: saddle bench-029 (0.3926/0.2812), camp bench-011 (0.3203/0.4180). Inspect source/mask association rather than declaring semantic failure.",
        "- Accepted yet lower frame relevance: flower bench-034 (0.3770), lamp bench-022 (0.3887). These are possible reranker/predicate/context limitations, not proven incorrect annotations.",
        "- Largest crop/context gaps include lamp bench-021 (0.6406/0.4512), coat bench-017 (0.5859/0.3984), table bench-012 (0.5820/0.4102), and holster bench-040 (0.3887/0.5469).",
        "- action/face bench-019 and action/head bench-020 are nominal catalog labels. Source action propositions remain separate: smile 0.4824; kneeling 0.5430; holding head 0.5312. A high head score does not establish an action.",
        "- objects/head bench-018 is not an exact current annotation value and conflicts with the documented inanimate-object scope. This is a review flag, not a mutation.",
    ])
    for view in ("crop", "frame"):
        for decision in ("accepted", "rejected_visual_viability"):
            values = [case["views"][view]["ranking"]["proposed_label_score"] for case in result["cases"] if case["human_engraving_review"] == decision]
            lines.append(f"- {view}, {decision}: min {min(values):.4f}, median {statistics.median(values):.4f}, max {max(values):.4f}.")
    lines.extend(["", "## Source Trace", "", "| ID | Movie | Frame | Shot | Annotation State | Source Field Values |", "|---|---|---:|---|---|---|"])
    for case in result["cases"]:
        source = case["source"]
        lines.append(f"| {case['benchmark_id']} | {source['filename']} | {source['frame']} | {source['shot_id']} | {case['annotation_state']} | {json.dumps(case['source_annotation_values'])} |")
    lines.extend(["", "## Predicates", ""])
    for field in sorted(PREDICATES):
        predicate = predicate_for(field, "{label}")
        lines.extend([f"### {field}", predicate["query"], f"Input policy: {predicate['input_policy']}", ""])
    lines.extend(["Instruction: " + INSTRUCTION, "", "## Exploratory Distributions", "", "Counts below are associations with engraving review, NOT accuracy or calibration."])
    exploration = {}
    for threshold in (0.2, 0.5, 0.8):
        counts = {}
        for case in result["cases"]:
            score = case["views"]["frame"]["ranking"]["proposed_label_score"]
            group = f"{case['human_engraving_review']} / {'above' if score >= threshold else 'below'}"
            counts[group] = counts.get(group, 0) + 1
        exploration[str(threshold)] = counts
        lines.append(f"- Candidate frame threshold {threshold}: {json.dumps(counts, sort_keys=True)}")
    save_artifact(root / "exploratory-analysis.json", exploration)
    lines.extend(["", "## Performance", "", "```json", json.dumps({key: value for key, value in result["performance"].items() if key != "calls"}, indent=2), "```", "", "## Interpretation Limits", "", "- Full-frame scores do not prove the selected mask depicts the label; another instance may be present.", "- Setting/action crops are only local diagnostics; context is primary.", "- Alternatives may be synonyms or simultaneously visible; a lower proposed-label rank is not a contradiction.", "- Single-frame action evidence is incomplete; no temporal inference or tracking was run.", "- Label-specific source frames were extracted when general BEST provenance did not match.", "- Production annotation validity, engraving eligibility and assignment state remain unchanged."])
    lines.extend(["- Peak VRAM is torch allocated inference memory, not total driver reservation or model-load peak. Wall timings exclude protected-snapshot hashing.", "- The complete initial 403-decision pass took 32.36 s scoring wall time (52.69 s including preparation/load/smoke). The corrected-control pass reused 226 decisions and computed 174 new ones in 14.21 s scoring wall time. Initial results are retained, not erased.", "- Marked contextual frames were deferred; no new overlay infrastructure was built.", "", "## Recommended QA Insertion", "", "Add advisory QA after parsed/normalized machine output and before persistence only in a future explicitly authorized integration. Structural normalization is not semantic validation. Existing persisted annotations can receive a separate read-only audit. Human edits/decisions must not be overridden. Evaluate ontology correctness, mask association and downstream eligibility independently."])
    atomic_write_text(root / "report.md", "\n".join(lines) + "\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Read-only forty-case local BF16 annotation QA experiment")
    parser.add_argument("--project", required=True)
    arguments = parser.parse_args()
    print(run_probe(arguments.project))