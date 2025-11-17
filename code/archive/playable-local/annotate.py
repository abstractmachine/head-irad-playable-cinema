DEBUG = True
IMAGE_COUNT = 3

MODEL_NAME = "gemma3:27b"
#MODEL_NAME = "gemma3:12b"
# MODEL_NAME = "gemma3:4b"
# MODEL_NAME = "llava"
# MODEL_NAME = "llama3.2-vision"
# MODEL_NAME = "mistral-small3.2"
MAX_DIM = 896

import sys
import json
from time import perf_counter
from pathlib import Path
from PIL import Image
# Ollama is for local AI model inferencing
from ollama import chat
# JSON formatting
from pydantic import BaseModel, Field
# include List
from typing import List, Optional, Literal

from metal import _check_metal_gpu

class Scene(BaseModel):
    location_type: Literal["EXTERIOR", "INTERIOR"] | str
    time_of_day: str = Field(
        description="The time of day when the scene takes place"
    )
    setting: str = Field(
        description="Minimal description of the setting"
    )
    shot_type: List[str] = Field(
        description="A cinematographic term that describes the type of shot(s) being used"
    )
    characters: List[str] = Field(
        description="The important characters in the scene"
    )
    animals: Optional[List[str]] = Field(
        description="A list of animals present in the scene"
    )
    props: Optional[List[str]] = Field(
        description="A list of props present in the scene"
    )
    mood: Optional[str] = Field(
        description="The overall mood or tone of the scene"
    )
    action: Optional[List[str]] = Field(
        description="A list of actions taking place in the scene"
    )
    dialogue: Optional[List[str]] = Field(
        description="Direct dialogue summary only. No quotes. If no subtitles are provided, say 'no dialogue'."
    )
    reasoning: List[str] = Field(
        description="A step-by-step explanation of how the answer was determined"
    )

class AnnotationResponse(BaseModel):
    scene: Scene

def scene_to_text(scene: dict) -> str:
    """Convert scene dictionary to key: value text output."""
    lines = []
    for key, value in scene.items():
        if isinstance(value, list):
            lines.append(f"{key}: {', '.join(value)}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)

def main(model: str = MODEL_NAME):
    # quick debug: report Metal/MPS status
    if DEBUG:
      ok, reason = _check_metal_gpu()
      print(json.dumps({"debug": "metal_gpu", "enabled": ok, "reason": reason}))

    base = Path(__file__).parent

    def read_or_default(fname: str, default: str) -> str:
        p = base / fname
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception as e:
            if DEBUG:
                print(f"[DEBUG] could not read {p}: {e}", file=sys.stderr)
            return default

    system_text = read_or_default("system.txt", "")
    user_text = read_or_default("user.txt", "")

    # include the images if present and measure image-prep time
    img_dir = base / "images" / "scene"
    imgs = []
    image_info = []
    image_prep_s = 0.0
    if img_dir.exists():
        t_img0 = perf_counter()
        # Gather all .jpg files and sort numerically by filename
        def numeric_key(p):
            # Extract leading number from filename, fallback to 0
            import re
            m = re.match(r"(\d+)", p.stem)
            return int(m.group(1)) if m else 0
        all_imgs = sorted([p for p in img_dir.glob("*.jpg") if p.is_file()], key=numeric_key)
        import random
        # Shuffle the sorted list for random distribution
        random.shuffle(all_imgs)
        take = min(IMAGE_COUNT, len(all_imgs))
        chosen = all_imgs[:take]
        if DEBUG:
            print(json.dumps({"debug": "image_selection", "requested": IMAGE_COUNT, "available": len(all_imgs), "chosen": [p.name for p in chosen]}), file=sys.stderr)
        # --- Resize images to MAX_DIM before inferencing ---
        for p in chosen:
            try:
                img = Image.open(p)
                w, h = img.size
                scale = min(MAX_DIM / w, MAX_DIM / h, 1.0)
                if scale < 1.0:
                    new_size = (int(w * scale), int(h * scale))
                    img = img.resize(new_size, Image.LANCZOS)
                    from io import BytesIO
                    buf = BytesIO()
                    img.save(buf, format="JPEG")
                    buf.seek(0)
                    tmp_path = p.parent / f"resized_{p.name}"
                    with open(tmp_path, "wb") as f:
                        f.write(buf.read())
                    imgs.append(str(tmp_path))
                    image_info.append({"name": tmp_path.name, "bytes": tmp_path.stat().st_size, "resized": True})
                else:
                    b = p.read_bytes()
                    imgs.append(str(p))
                    image_info.append({"name": p.name, "bytes": len(b), "resized": False})
            except Exception as e:
                if DEBUG:
                    print(f"[DEBUG] failed reading/resizing {p}: {e}", file=sys.stderr)
        image_prep_s = perf_counter() - t_img0

    user_msg = {"role": "user", "content": user_text}
    if imgs:
        user_msg["images"] = imgs

    messages = [
        {"role": "system", "content": system_text},
        user_msg,
    ]

    # measure inference separately
    start = perf_counter()
    print(json.dumps({"debug": "ollama_model_requested", "model": model}), file=sys.stderr)
    try:
        resp = chat(model=model, messages=messages, format=AnnotationResponse.model_json_schema())
        # Print actual model used if available in response
        actual_model = getattr(resp, "model", None)
        if actual_model:
            print(json.dumps({"debug": "ollama_model_used", "model": actual_model}), file=sys.stderr)
    except Exception as e:
        print(json.dumps({"error": str(e), "image_prep_s": image_prep_s}), ensure_ascii=False)
        sys.exit(1)
    elapsed = perf_counter() - start

    msg = getattr(resp, "message", None)
    content = getattr(msg, "content", None) if msg else None
    if content is None and isinstance(resp, dict):
        content = (resp.get("message") or {}).get("content")

    if not content:
        print(json.dumps({"error": "no content", "duration_s": elapsed}, ensure_ascii=False))
        sys.exit(1)

    txt_output = ""
    try:
        query = AnnotationResponse.model_validate_json(content)
        out = query.model_dump()
        out["elapsed_time_seconds"] = elapsed
        out["image_preparation_seconds"] = image_prep_s
        # Convert scene to text output
        txt_output = scene_to_text(out["scene"])
        print(txt_output)
        return
    except Exception:
        pass

    try:
        parsed = json.loads(content)
        # If scene key exists, convert to text
        if "scene" in parsed:
            txt_output = scene_to_text(parsed["scene"])
            print(txt_output)
        else:
            print(json.dumps({"result": parsed, "elapsed": elapsed, "image_info": image_info}, indent=2, ensure_ascii=False))
    except Exception:
        print(json.dumps({"error": "invalid response", "raw": content, "duration": elapsed, "image_info": image_info}, ensure_ascii=False))
        sys.exit(1)

    # txt_output is available as a variable here if needed elsewhere

if __name__ == "__main__":
    main()