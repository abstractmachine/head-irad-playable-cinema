"""Freeze manifest for an E9-A transfer run.

Written immediately before the blind film is generated, so the run can be
pointed back at the exact prompts, source modules and model settings that
produced it. Hashes are of file contents, not timestamps.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from services import palette_e9 as e9

# Everything whose content could change an E9-A proposal.
SOURCE_MODULES = (
    "services/palette_e9.py",
    "services/palette_review.py",
    "data/palette_review.py",
    "scripts/palette_lab/e8_palette_review.py",
    "scripts/palette_lab/e8_articulation.py",
    "scripts/palette_lab/e8_carrier.py",
    "scripts/palette_lab/stages.py",
    "scripts/palette_lab/two_color.py",
    "data/palette.py",
)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git_state(repo: Path) -> dict:
    def run(*args):
        try:
            return subprocess.run(args, cwd=repo, capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except Exception:
            return ""

    commit = run("git", "rev-parse", "HEAD")
    dirty = run("git", "status", "--porcelain")
    return {
        "commit": commit or None,
        "dirty": bool(dirty) if commit else None,
        "dirty_files": dirty.splitlines()[:40] if dirty else [],
        "note": "not a git repository" if not commit else None,
    }


def build(project_path: str, repo_path: str, *, calibration: str | None = None,
          target: dict | None = None) -> dict:
    project, repo = Path(project_path), Path(repo_path)
    prompts = {}
    for role, (system, user) in e9.STAGES.items():
        for kind, relative in (("system", system), ("user", user)):
            path = project / relative
            prompts[f"{role}.{kind}"] = {
                "path": relative, "sha256": _sha256(path),
                "bytes": path.stat().st_size if path.exists() else None,
            }
    sources = {
        relative: {"sha256": _sha256(repo / relative),
                   "bytes": (repo / relative).stat().st_size
                   if (repo / relative).exists() else None}
        for relative in SOURCE_MODULES
    }
    from tool import prefs

    return {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "experiment": e9.E9_EXPERIMENT,
        "version": e9.E9_VERSION,
        "git": _git_state(repo),
        "prompts": prompts,
        "sources": sources,
        "models": {
            "text": prefs.get("model_annotation") or "Qwen3-VL-8B-Instruct",
            "segmentation": prefs.get("model_segmentation") or "sam3",
            "curation_max_new_tokens": e9.CURATION_TOKENS,
            "repair_max_new_tokens": e9.REPAIR_TOKENS,
            "do_sample": False,
        },
        "configuration": {
            "max_repairs": e9.MAX_REPAIRS,
            "choices": {key: strategy for key, (strategy, _) in e9.STRATEGIES.items()},
            "frozen_downstream": [
                "operationalize prompt (2026-09-15 v1)",
                "SAM model and thresholds",
                "union-of-masks rule",
                "mean-of-mask representative colour",
                "control palette (data.palette)",
            ],
        },
        "calibration_artifact": calibration,
        "target": target,
    }


def write(project_path: str, repo_path: str, destination: Path, **kwargs) -> Path:
    manifest = build(project_path, repo_path, **kwargs)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    return destination
