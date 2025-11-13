#!/usr/bin/env python3
from __future__ import annotations
from pathlib import Path
from typing import Optional, Tuple, List
import json
import numpy as np
import pandas as pd
import faiss
from sentence_transformers import SentenceTransformer

FIELD_ORDER = ["Protagonists", "Place", "Actions", "Objects"]
MODEL_NAME = "BAAI/bge-small-en-v1.5"

def check_encoded_files(csv_path: Path):
    """
    Return (txt_path, npy_path) if both exist, else (None, None).
    Filenames: <stem>.txt and <stem>.npy
    """
    csv_path = Path(csv_path)
    stem = csv_path.stem
    parent = csv_path.parent
    txt_path = parent / f"{stem}.txt"
    npy_path = parent / f"{stem}.npy"
    if txt_path.exists() and npy_path.exists():
        return txt_path, npy_path
    return None, None

def json_to_text(caption_str: str) -> str:
    """
    Always output four categories in fixed order.
    Empty/missing fields render as 'Category: ' (blank after colon).
    """
    def to_list(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []
    values = {k: [] for k in FIELD_ORDER}
    if isinstance(caption_str, str) and caption_str.strip():
        try:
            obj = json.loads(caption_str)
            for k in FIELD_ORDER:
                if k in obj:
                    values[k] = to_list(obj[k])
        except Exception:
            pass
    parts = []
    for k in FIELD_ORDER:
        content = ", ".join(values[k]) if values[k] else ""
        parts.append(f"{k}: {content}")
    return " | ".join(parts)

def encode_csv_to_npy(csv_path: Path,
                      out_txt: Path,
                      out_npy: Path,
                      text_col: str = "Shot_Caption",
                      model_name: str = MODEL_NAME):
    df = pd.read_csv(csv_path)
    # Keep row alignment: ignored rows become empty lines
    texts: List[str] = []
    for _, row in df.iterrows():
        if str(row.get("Ignore", "")).strip().lower() == "yes":
            texts.append("")
        else:
            texts.append(json_to_text(row.get(text_col, "")))
    # Write .txt (one line per CSV row)
    with open(out_txt, "w", encoding="utf-8") as f:
        for line in texts:
            f.write(line + "\n")
    # Embeddings for all rows (empty strings produce near-zero embeddings)
    model = SentenceTransformer(model_name, device="cpu")
    X = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True).astype("float32")
    np.save(out_npy, X)
    return X

def ensure_embeddings(csv_path: Path, model_name: str = MODEL_NAME):
    """
    Ensure sidecar .txt and .npy exist for the given CSV.
    Returns (txt_path, npy_path).
    """
    csv_path = Path(csv_path)
    txt_path, npy_path = check_encoded_files(csv_path)
    if txt_path and npy_path:
        print(f"✅ Found precomputed files for {csv_path.name}")
        return txt_path, npy_path
    stem = csv_path.stem
    parent = csv_path.parent
    out_txt = parent / f"{stem}.txt"
    out_npy = parent / f"{stem}.npy"
    print(f"⚙️ Encoding {csv_path.name} with {model_name} …")
    encode_csv_to_npy(csv_path, out_txt, out_npy, model_name=model_name)
    print(f"✅ Done: {out_txt.name}, {out_npy.name}")
    return out_txt, out_npy

def _timecode_to_seconds(tc: str) -> Optional[float]:
    if not isinstance(tc, str) or ":" not in tc:
        return None
    try:
        h, m, s = tc.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return None

class FaissMatcher:
    """
    Simple cosine-similarity matcher over movie captions.
    Uses the precomputed movie embeddings .npy for speed.
    """
    def __init__(self, movie_csv: Path, movie_emb_npy: Path, model_name: str = MODEL_NAME):
        self.df = pd.read_csv(movie_csv)  # unfiltered; rows align with .npy
        self.start_seconds = [ _timecode_to_seconds(x) for x in self.df.get("Start", []) ]
        self.X = np.load(movie_emb_npy).astype("float32")
        d = self.X.shape[1]
        self.index = faiss.IndexFlatIP(d)  # cos if inputs are normalized
        self.index.add(self.X)
        self.encoder = SentenceTransformer(model_name, device="cpu")

    def search_best_from_text(self, text_line: str, k: int = 1):
        """
        text_line must be the same formatting as saved in .txt (one line).
        Returns dict {row, start_seconds, sim} or None.
        """
        if not text_line or not text_line.strip():
            return None
        q = self.encoder.encode([text_line], convert_to_numpy=True, normalize_embeddings=True).astype("float32")
        sims, idxs = self.index.search(q, k)
        row = int(idxs[0, 0])
        sim = float(sims[0, 0])
        start_s = self.start_seconds[row] if 0 <= row < len(self.start_seconds) else None
        return {"row": row, "start_seconds": start_s, "sim": sim}

def handle_game_caption_change(caption: str, shot: dict, position: float) -> None:
    # Left as a no-op hook; FAISS jump is triggered from app.py
    return


