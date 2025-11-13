from __future__ import annotations
import csv
from pathlib import Path
from typing import List, Dict, Optional

def load_shotlist(csv_path: str):
    # Optional: return sidecar paths if you need them elsewhere
    from search import ensure_embeddings
    return ensure_embeddings(Path(csv_path))

def load_shots(csv_path: Path) -> List[Dict]:
    """
    Load non-ignored shots with timing, and include the original CSV row index
    so we can map to the corresponding line in the generated .txt.
    """
    shots: List[Dict] = []
    csv_path = Path(csv_path)
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader):
            try:
                if (row.get("Ignore", "") or "").strip().lower() == "yes":
                    continue
                start = timecode_to_seconds(row["Start"])
                end = timecode_to_seconds(row["End"])
                caption = row.get("Shot_Caption", "") or ""
                shots.append({
                    "start": start,
                    "end": end,
                    "caption": caption,
                    "row": row_idx,  # original CSV row index (matches .txt line index)
                })
            except (KeyError, ValueError) as e:
                print(f"Warning: Skipping invalid row in {csv_path.name}: {e}")
    print(f"Loaded {len(shots)} shots from {csv_path.name}")
    return shots

def timecode_to_seconds(timecode: str) -> float:
    parts = timecode.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid timecode: {timecode}")
    h = int(parts[0]); m = int(parts[1]); s = float(parts[2])
    return h * 3600 + m * 60 + s

def find_shot_at_time(shots: List[Dict], t: float) -> Optional[Dict]:
    for shot in shots:
        if shot["start"] <= t <= shot["end"]:
            return shot
    return None