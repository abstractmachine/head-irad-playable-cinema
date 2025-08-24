DEBUG = True
IMAGE_COUNT = 3

import sys
import json
from time import perf_counter
from pathlib import Path
# Ollama is for local AI model inferencing
from ollama import chat
# JSON formatting
from pydantic import BaseModel, Field
# include List
from typing import List, Optional, Literal

from metal import _check_metal_gpu

class DialogueLine(BaseModel):
    character: str
    line: str

class Characters(BaseModel):
    primary: List[str] = Field(
        description="The main characters in the scene"
    )
    secondary: Optional[List[str]] = Field(
        description="The background characters in the scene"
    )

class Scene(BaseModel):
    location_type: Literal["EXTERIOR", "INTERIOR"] | str
    time_of_day: str = Field(
        description="The time of day when the scene takes place"
    )
    setting: str = Field(
        description="Minimal description of the setting"
    )
    shot_type: str = Field(
        description="A cinematographic term that describes the type of shot being used"
    )
    characters: Characters
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
    dialogue: Optional[List[DialogueLine]] = Field(
        description="Direct dialogue summary only. If no subtitles are provided, this field is said to be ignored"
    )
    reasoning: List[str] = Field(
        description="A step-by-step explanation of how the answer was determined"
    )

class AnnotationResponse(BaseModel):
    scene: Scene

def main(model: str = "gemma3:4b"):
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
        # gather all .jpg files then pick a random subset of size IMAGE_COUNT
        all_imgs = [p for p in sorted(img_dir.glob("*.jpg")) if p.is_file()]
        import random
        take = min(IMAGE_COUNT, len(all_imgs))
        chosen = random.sample(all_imgs, k=take) if take and take < len(all_imgs) else list(all_imgs)
        if DEBUG:
            print(json.dumps({"debug": "image_selection", "requested": IMAGE_COUNT, "available": len(all_imgs), "chosen": [p.name for p in chosen]}), file=sys.stderr)
        for p in chosen:
            try:
                b = p.read_bytes()  # measure local I/O / upload-prep cost
                imgs.append(str(p))
                image_info.append({"name": p.name, "bytes": len(b)})
            except Exception as e:
                if DEBUG:
                    print(f"[DEBUG] failed reading {p}: {e}", file=sys.stderr)
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
    try:
        resp = chat(model=model, messages=messages, format=AnnotationResponse.model_json_schema())
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

    try:
        query = AnnotationResponse.model_validate_json(content)
        out = query.model_dump()
        out["elapsed_time_seconds"] = elapsed
        out["image_preparation_seconds"] = image_prep_s
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    except Exception:
        pass

    try:
        parsed = json.loads(content)
        print(json.dumps({"result": parsed, "elapsed": elapsed, "image_info": image_info}, indent=2, ensure_ascii=False))
    except Exception:
        print(json.dumps({"error": "invalid response", "raw": content, "duration": elapsed, "image_info": image_info}, ensure_ascii=False))
        sys.exit(1)

if __name__ == "__main__":
    main()