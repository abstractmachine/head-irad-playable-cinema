"""E9-A retrospective calibration against the completed tmdb_95864 review.

    uv run python -m services.palette_e9_calibration --limit 150

Runs E9-A over calibration frames WITHOUT touching the review record. The
frozen measured-review-v1 proposals and every human decision are what this is
evaluated against, so overwriting them would destroy the evidence; nothing here
calls ``create_frame`` or ``save_review``.

No human decision is ever passed into a prompt. The review is read only after
generation, to score it.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from data import palette_review as store
from services import palette_e9 as e9

SPACE = "palette-system-e9"
CALIBRATION = "calibration-tmdb_95864"
SAMPLE_SEED = 20260921


def output_root(project_path: str) -> Path:
    return Path(project_path) / "outputs" / "tests" / SPACE / CALIBRATION


def sample_shots(project_path: str, filename: str, media_type: str,
                 limit: int | None) -> list:
    """A seeded random sample, declared before any result is seen."""
    from services.palette_review import list_frames

    frames = [item for item in list_frames(project_path, filename, media_type)
              if item["available"]]
    if limit is None or limit >= len(frames):
        return frames
    return sorted(random.Random(SAMPLE_SEED).sample(frames, limit),
                  key=lambda item: item["index"])


def run(project_path: str, filename: str, media_type: str = "movie",
        *, limit: int | None = None) -> Path:
    from services.palette_review import _free_models, load_models

    root = output_root(project_path)
    (root / "frames").mkdir(parents=True, exist_ok=True)
    frames = sample_shots(project_path, filename, media_type, limit)

    models = load_models(project_path)
    started = time.time()
    done = failed = 0
    try:
        for position, item in enumerate(frames, start=1):
            target = root / "frames" / f"{item['shot_id']}.json"
            if target.exists():
                done += 1
                continue
            record = {"shot_id": item["shot_id"], "index": item["index"],
                      "image": item["image"]}
            try:
                result = e9.generate_frame_proposals(
                    project_path, filename, media_type, item["shot_id"],
                    models=models)
                record["choices"] = e9._public_choices(result["choices"])
                record["provenance"] = result["provenance"]
                record["status"] = "generated"
                done += 1
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = f"{type(exc).__name__}: {exc}"
                failed += 1
            target.write_text(json.dumps(record, indent=2, ensure_ascii=False,
                                         default=str), encoding="utf-8")
            rate = (time.time() - started) / position
            print(f"  [{position}/{len(frames)}] {item['shot_id']} "
                  f"{record['status']}  {rate:.0f}s/frame "
                  f"eta {rate * (len(frames) - position) / 60:.0f}m", flush=True)
    finally:
        _free_models(models)

    (root / "00-run.json").write_text(json.dumps({
        "experiment": e9.E9_EXPERIMENT,
        "version": e9.E9_VERSION,
        "created": datetime.now(timezone.utc).isoformat(),
        "filename": filename,
        "media_type": media_type,
        "sample": {"seed": SAMPLE_SEED, "limit": limit, "frames": len(frames)},
        "generated": done, "failed": failed,
        "note": ("Evaluation only. The review record was not modified and no "
                 "human decision was supplied to any prompt."),
    }, indent=2), encoding="utf-8")
    return root


def main() -> None:
    parser = argparse.ArgumentParser(description="E9-A retrospective calibration")
    parser.add_argument("--project", default="/home/cowpoke/playable/dead-crossing")
    parser.add_argument("--film", default="10 000 Dollari Per Un Massacro (1967) {tmdb-95864}.mp4")
    parser.add_argument("--limit", type=int, default=150)
    args = parser.parse_args()
    root = run(args.project, args.film, limit=args.limit)
    print("calibration ->", root)


if __name__ == "__main__":
    main()
