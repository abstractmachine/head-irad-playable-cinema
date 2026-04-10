"""Automated shot annotation utilities using a local LLM (via transformers).

This module provides functions to:
- locate and read system prompt files
- sample representative frames from a shot (optional, via ffmpeg)
- load/cache a text-generation pipeline under the project's `models/` folder
- run the model to produce JSON-structured shot annotations
- write canonical JSON outputs and update shotlist CSVs

The implementation intentionally keeps model/system-prompt concerns separate:
the system prompt text is read from files under `prompts/movies/shots/` (or
provided inline) and is not coupled to caching or transformer logic.
"""

from __future__ import annotations

FRAMES_PER_SHOT = 10

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import traceback
from datetime import datetime


def _safe_model_dir(project_path: str, model_name: str) -> Path:
    safe = model_name.replace(":", "_").replace("/", "_")
    return Path(project_path) / "models" / safe


def _append_log(path, text: str) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.utcnow().isoformat()} {text}\n")
    except Exception:
        # Best-effort logging — never raise from the logger
        return


def find_latest_prompt(project_path: str, media_type: str = "movies", prefix: str = "") -> Optional[Path]:
    d = Path(project_path) / "prompts" / media_type / "shots"
    if not d.exists() or not d.is_dir():
        return None
    pattern = f"{prefix}-*.txt" if prefix else "*.txt"
    files = [p for p in d.glob(pattern) if p.is_file()]
    if not files:
        return None
    files.sort(key=lambda p: p.name, reverse=True)
    return files[0]


def load_system_prompt(project_path: str, media_type: str, prompt_file: Optional[str], prompt_text: Optional[str]) -> Tuple[str, Optional[str]]:
    """Return (prompt_text, prompt_file_path_or_None).

    Preference order:
      1. inline `prompt_text` if provided
      2. explicit `prompt_file` path (absolute or relative to project)
      3. latest system-*.txt under prompts/<media_type>/shots/
      4. latest any .txt under prompts/<media_type>/shots/
      5. fallback short prompt
    """
    if prompt_text:
        return prompt_text, None

    if prompt_file:
        p = Path(prompt_file)
        if not p.exists():
            p = Path(project_path) / prompt_file
        if p.exists():
            return p.read_text(encoding="utf-8"), str(p)

    latest = find_latest_prompt(project_path, media_type, prefix="system")
    if not latest:
        latest = find_latest_prompt(project_path, media_type)
    if latest:
        return latest.read_text(encoding="utf-8"), str(latest)

    # Minimal fallback
    return "You are a visual annotation system. Return only valid JSON.", None


def load_user_prompt(project_path: str, media_type: str, prompt_file: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (user_prompt_text, file_path_or_None). Returns (None, None) if not found.

    Preference order:
      1. explicit `prompt_file` path
      2. latest user-*.txt under prompts/<media_type>/shots/
    """
    if prompt_file:
        p = Path(prompt_file)
        if not p.exists():
            p = Path(project_path) / prompt_file
        if p.exists():
            return p.read_text(encoding="utf-8"), str(p)

    latest = find_latest_prompt(project_path, media_type, prefix="user")
    if latest:
        return latest.read_text(encoding="utf-8"), str(latest)

    return None, None


def _substitute_variables(template: str, variables: Dict[str, str]) -> str:
    """Replace $key placeholders with corresponding values. Missing keys → empty string."""
    for key, value in variables.items():
        template = template.replace(f"${key}", value or "")
    return template


_ANNOTATION_SCHEMA: Dict[str, type] = {
    "setting": str,
    "objects": list,
    "humans": list,
    "animals": list,
    "text": list,
}


def _validate_annotation(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize an extracted JSON dict against the annotation schema.

    Coerces types: missing fields get defaults, scalars get wrapped in lists.
    Extra fields from the model are preserved.
    """
    result = dict(data)  # keep any extra fields the model included
    for field, expected_type in _ANNOTATION_SCHEMA.items():
        val = data.get(field)
        if expected_type is str:
            result[field] = str(val) if val is not None else ""
        elif expected_type is list:
            if isinstance(val, list):
                result[field] = [str(v) for v in val]
            elif val is None:
                result[field] = []
            else:
                result[field] = [str(val)]
    return result


def _timecode_to_seconds(tc: str) -> float:
    parts = tc.split(":")
    try:
        seconds = float(parts[-1])
    except Exception:
        seconds = 0.0
    if len(parts) >= 2:
        try:
            seconds += int(parts[-2]) * 60
        except Exception:
            pass
    if len(parts) == 3:
        try:
            seconds += int(parts[-3]) * 3600
        except Exception:
            pass
    return seconds


def _parse_srt(srt_path) -> List[Tuple[float, float, str]]:
    """Parse an SRT file into a list of (start_s, end_s, text) tuples."""
    import re
    entries: List[Tuple[float, float, str]] = []
    try:
        raw = Path(srt_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return entries
    # Split into blocks separated by blank lines
    blocks = re.split(r"\n\s*\n", raw.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        # Find the timecode line (contains -->)
        tc_line = next((l for l in lines if "-->" in l), None)
        if tc_line is None:
            continue
        m = re.match(
            r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})",
            tc_line.strip()
        )
        if not m:
            continue
        start_s = _timecode_to_seconds(m.group(1).replace(",", "."))
        end_s   = _timecode_to_seconds(m.group(2).replace(",", "."))
        # Text lines: everything after the timecode line, skip the sequence number
        text_lines = []
        after_tc = False
        for l in lines:
            if after_tc:
                text_lines.append(l)
            elif "-->" in l:
                after_tc = True
        text = " ".join(text_lines).strip()
        # Strip HTML tags (e.g. <i>, <b>) sometimes present in SRT
        text = re.sub(r"<[^>]+>", "", text).strip()
        if text:
            entries.append((start_s, end_s, text))
    return entries


def _subtitles_for_shot(srt_entries: List[Tuple[float, float, str]], shot_start: str, shot_end: str) -> str:
    """Return formatted subtitle lines overlapping the shot boundary, or 'None'."""
    start_s = _timecode_to_seconds(shot_start)
    end_s   = _timecode_to_seconds(shot_end)
    lines = []
    for (sub_start, sub_end, text) in srt_entries:
        # Include if subtitle overlaps the shot window (even partially)
        if sub_end > start_s and sub_start < end_s:
            # Format times as HH:MM:SS.mmm matching the shot timecode style
            def _fmt(s: float) -> str:
                h = int(s // 3600)
                m = int((s % 3600) // 60)
                sec = s % 60
                return f"{h:02d}:{m:02d}:{sec:06.3f}"
            lines.append(f'{_fmt(sub_start)} → {_fmt(sub_end)}: "{text}"')
    return "\n".join(lines) if lines else "None"


def _sanitize_system_prompt(text: str) -> str:
    """Remove large example JSON blocks from system prompts to avoid model
    echoing the example instead of generating a new JSON object.

    Heuristic: trim any content starting from the 'Return JSON with this schema'
    marker up to the next '---' delimiter (if present). Also remove any large
    example JSON that appears to begin with a movie schema block.
    """
    if not text:
        return text
    marker = "Return JSON with this schema"
    if marker in text:
        start = text.find(marker)
        # find next delimiter (common in our prompts)
        delim = text.find("\n---", start)
        if delim != -1:
            return text[:start] + "\n" + text[delim:]
        # fallback: strip everything after the marker
        return text[:start] + "\n"

    # If a JSON example block appears directly (common pattern), strip it.
    movie_marker = "\n{\n  \"movie\":"
    if movie_marker in text:
        idx = text.find(movie_marker)
        return text[:idx] + "\n"

    # Aggressive cleanup: remove any large JSON-like blocks to avoid template
    # echoing. We look for the first balanced brace block larger than a
    # threshold and strip it out.
    try:
        L = len(text)
        i = text.find("{")
        while i != -1:
            depth = 0
            found = False
            for j in range(i, L):
                ch = text[j]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        # candidate block from i..j
                        if (j - i) > 120:  # heuristic: large block -> likely example
                            text = text[:i] + "\n" + text[j+1:]
                        found = True
                        break
            if not found:
                break
            i = text.find("{", i + 1)
    except Exception:
        pass

    return text


def sample_frames_for_shot(
    video_path: str,
    start_time: str,
    end_time: str,
    frames_per_shot: int = FRAMES_PER_SHOT,
    sample_mode: str = "center",
    out_dir: Optional[Path] = None,
) -> List[str]:
    """Sample a small set of frames from a shot using ffmpeg.

    Returns list of file paths to the extracted images. If ffmpeg is not
    available or extraction fails the returned list may be shorter or empty.
    """
    s = _timecode_to_seconds(start_time)
    e = _timecode_to_seconds(end_time)
    if e <= s:
        positions = [s] * frames_per_shot
    else:
        duration = e - s
        if sample_mode == "center":
            positions = [s + (i + 1) / (frames_per_shot + 1) * duration for i in range(frames_per_shot)]
        elif sample_mode == "start":
            positions = [s + i * (duration / frames_per_shot) for i in range(frames_per_shot)]
        elif sample_mode == "end":
            positions = [e - (i + 1) / (frames_per_shot + 1) * duration for i in range(frames_per_shot)]
        else:
            positions = [s + (i + 1) / (frames_per_shot + 1) * duration for i in range(frames_per_shot)]

    tmp = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="crossing-frames-"))
    tmp.mkdir(parents=True, exist_ok=True)

    frame_paths: List[str] = []
    for i, pos in enumerate(positions):
        out = tmp / f"frame_{i+1}.jpg"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(pos),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(out),
        ]
        try:
            subprocess.run(cmd, check=True)
            frame_paths.append(str(out))
        except Exception:
            # ignore failures — model can still run without images
            continue

    return frame_paths


def _find_prebaked_frames(project_path: str, media_type: str, filename: str, shot_index: int, frames_per_shot: int) -> List[str]:
    """Look for user-provided frame images under media/frames/<media_type>/<stem>.

    Matches files that include 'shot-<shot_index>' (case-insensitive). If
    not enough matching files are found, returns the first `frames_per_shot`
    images in the folder as a fallback.
    """
    stem = Path(filename).stem
    d = Path(project_path) / "media" / "frames" / media_type / stem
    if not d.exists() or not d.is_dir():
        return []
    imgs = [p for p in sorted(d.glob("*.jpg")) if p.is_file()]
    if not imgs:
        return []
    shot_tag = f"shot-{shot_index}"
    matched: List[Path] = [p for p in imgs if shot_tag in p.name.lower() or f"shot_{shot_index}" in p.name.lower()]
    if matched:
        return [str(p) for p in matched[:frames_per_shot]]
    # fallback: return the first N images
    return [str(p) for p in imgs[:frames_per_shot]]


def _load_text_generation_pipeline(project_path: str, model_name: str):
    try:
        from transformers import AutoModelForCausalLM, AutoModelForImageTextToText, AutoProcessor, pipeline
    except Exception as exc:  # pragma: no cover - depends on user env
        raise ImportError("Transformers is required for model-based annotation") from exc

    # Prefer project-local models under <project>/models/<model_name>
    project_local = Path(project_path) / "models" / model_name
    explicit_path = Path(model_name).expanduser()

    tokenizer = None
    model = None

    # The annotate command requires a vision-language model.  Load the model's
    # processor via AutoProcessor so that image_processor is available.  A
    # text-only tokenizer (AutoTokenizer) is deliberately not accepted here.
    def _load_tokenizer(path_or_name, local_files_only=False, cache_dir: Optional[str] = None):
        base_kwargs: Dict[str, Any] = {"local_files_only": local_files_only}
        if cache_dir:
            base_kwargs["cache_dir"] = cache_dir
        return AutoProcessor.from_pretrained(path_or_name, **base_kwargs)

    def _require_vision(proc, source: str) -> None:
        """Raise a clear error when a text-only tokenizer was loaded instead of
        a vision-capable processor.  This usually means the wrong model variant
        was downloaded (e.g. `Qwen3-8B` instead of `Qwen3-VL-8B-Thinking`)."""
        if not hasattr(proc, "image_processor"):
            raise RuntimeError(
                f"The model loaded from '{source}' does not have an image processor "
                f"and cannot be used for visual annotation.\n"
                f"Please use a vision-language model variant "
                f"(e.g. 'Qwen3-VL-8B-Thinking', 'gemma-4' with vision support) "
                f"and make sure its full processor directory is present."
            )

    def _load_model(path_or_name, **kwargs):
        """Load a vision-language model, trying AutoModelForImageTextToText
        first (covers Qwen3-VL, Gemma4, Llama4, etc. in transformers 5.x)
        then falling back to AutoModelForCausalLM for other architectures."""
        try:
            return AutoModelForImageTextToText.from_pretrained(
                path_or_name, trust_remote_code=True, **kwargs
            )
        except Exception:
            return AutoModelForCausalLM.from_pretrained(
                path_or_name, trust_remote_code=True, **kwargs
            )

    # 1) Project-local directory (preferred)
    if project_local.exists():
        try:
            tokenizer = _load_tokenizer(str(project_local), local_files_only=True)
            model = _load_model(str(project_local), local_files_only=True)
        except Exception as exc:
            raise RuntimeError(f"Failed to load local model from '{project_local}': {exc}") from exc
        _require_vision(tokenizer, str(project_local))

    # 2) Explicit path provided by user (absolute or relative)
    elif explicit_path.exists():
        try:
            tokenizer = _load_tokenizer(str(explicit_path), local_files_only=True)
            model = _load_model(str(explicit_path), local_files_only=True)
        except Exception as exc:
            raise RuntimeError(f"Failed to load local model from '{explicit_path}': {exc}") from exc
        _require_vision(tokenizer, str(explicit_path))

    # 3) Treat as Hugging Face repo id (may download into project cache)
    else:
        cache_dir = _safe_model_dir(project_path, model_name)
        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            tokenizer = _load_tokenizer(model_name, local_files_only=False, cache_dir=str(cache_dir))
            model = _load_model(model_name, cache_dir=str(cache_dir))
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load model '{model_name}' from Hugging Face Hub (and no local model found under '{project_local}').\n"
                f"Install or place the model under '{project_local}', or provide an absolute/local path, or a valid HF repo id.\n"
                f"Error: {exc}"
            ) from exc
        _require_vision(tokenizer, model_name)

    # Ensure the model/config does not carry legacy max_length defaults that
    # conflict with `max_new_tokens`. Set cleaned defaults on both config and
    # generation_config where possible before creating the pipeline.
    try:
        if hasattr(model, "config") and model.config is not None:
            try:
                setattr(model.config, "max_length", None)
            except Exception:
                pass
            try:
                setattr(model.config, "max_new_tokens", 512)
            except Exception:
                pass
            try:
                setattr(model.config, "do_sample", False)
            except Exception:
                pass
    except Exception:
        pass

    try:
        if hasattr(model, "generation_config") and model.generation_config is not None:
            try:
                setattr(model.generation_config, "max_length", None)
            except Exception:
                pass
            try:
                setattr(model.generation_config, "max_new_tokens", 512)
            except Exception:
                pass
            try:
                setattr(model.generation_config, "do_sample", False)
            except Exception:
                pass
    except Exception:
        pass

    # Build a fresh GenerationConfig with only explicit defaults. This avoids
    # inheriting legacy `max_length` or other conflicting params from the
    # model/config which can trigger repeated generation warnings.
    try:
        from transformers import GenerationConfig
        gen_cfg = GenerationConfig()
        try:
            setattr(gen_cfg, "max_new_tokens", 512)
        except Exception:
            pass
        try:
            setattr(gen_cfg, "do_sample", False)
        except Exception:
            pass
        try:
            setattr(gen_cfg, "max_length", None)
        except Exception:
            pass

        # Clear sampling-related params to avoid warnings from transformers
        sampling_attrs = ("temperature", "top_p", "top_k", "top_h", "typical_p")
        for attr in sampling_attrs:
            if hasattr(gen_cfg, attr):
                try:
                    setattr(gen_cfg, attr, None)
                except Exception:
                    pass

        # Copy over essential token ids if available to ensure generation runs
        base_cfg = getattr(model, "generation_config", None) or getattr(model, "config", None)
        if base_cfg is not None:
            for attr in ("eos_token_id", "pad_token_id", "bos_token_id", "decoder_start_token_id"):
                val = getattr(base_cfg, attr, None)
                if val is not None:
                    try:
                        setattr(gen_cfg, attr, val)
                    except Exception:
                        pass
    except Exception:
        gen_cfg = None

    # Create the pipeline using the cleaned generation_config when possible.
    # If pipeline creation fails (e.g. because the processor is not compatible
    # with the text-generation pipeline type), fall back to a minimal wrapper
    # so that _call_model can still access .model and .tokenizer directly.
    class _ModelPipeline:
        """Minimal wrapper holding model + processor/tokenizer."""
        def __init__(self, m, t):
            self.model = m
            self.tokenizer = t

        def __call__(self, prompt, **kwargs):
            try:
                inputs = self.tokenizer(prompt, return_tensors="pt")
                outputs = self.model.generate(**inputs, max_new_tokens=512, do_sample=False)
                text = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
                return [{"generated_text": text}]
            except Exception:
                return [{"generated_text": ""}]

    try:
        if gen_cfg is not None:
            gen = pipeline(
                "text-generation",
                model=model,
                tokenizer=tokenizer,
                trust_remote_code=True,
                generation_config=gen_cfg,
            )
        else:
            gen = pipeline("text-generation", model=model, tokenizer=tokenizer, trust_remote_code=True)
    except Exception:
        # Pipeline construction failed (common when tokenizer is an AutoProcessor
        # for a vision-language model).  Use the minimal wrapper instead.
        gen = _ModelPipeline(model, tokenizer)

    # Enforce cleaned generation settings on pipeline/model config objects
    # to minimize the surface where conflicting defaults may be read.
    try:
        if hasattr(gen, "model"):
            m = gen.model
            if hasattr(m, "generation_config") and m.generation_config is not None:
                try:
                    m.generation_config.max_length = None
                except Exception:
                    pass
                try:
                    m.generation_config.max_new_tokens = getattr(gen_cfg, "max_new_tokens", getattr(m.generation_config, "max_new_tokens", None))
                except Exception:
                    pass
                try:
                    m.generation_config.do_sample = getattr(gen_cfg, "do_sample", getattr(m.generation_config, "do_sample", None))
                except Exception:
                    pass
                # Also clear sampling params on the model generation_config to
                # avoid downstream warnings about invalid flags
                for attr in ("temperature", "top_p", "top_k", "top_h", "typical_p"):
                    if hasattr(m.generation_config, attr):
                        try:
                            setattr(m.generation_config, attr, None)
                        except Exception:
                            pass
            if hasattr(m, "config") and m.config is not None:
                try:
                    m.config.max_length = None
                except Exception:
                    pass
    except Exception:
        pass

    return gen


def _reload_pipeline(project_path: str, model_name: str, log_path, verbose: bool, reason: str, shots_since_reload: int):
    """Clear GPU memory and reload the pipeline from scratch.

    The caller MUST set ``pipeline = None`` before calling this function so
    that the old model's reference count drops to zero before gc.collect()
    runs.  Passing the old pipeline in as a parameter and calling ``del``
    inside the function does NOT release it — the caller's reference is still
    live and the object will not be freed until after this function returns.

    Keeps reload logic separate from annotation logic.  Returns the new
    pipeline instance on success, or raises if loading fails.

    Parameters
    ----------
    reason:
        Either ``"interval"`` (periodic N-shot cycle) or
        ``"consecutive_failures"`` (too many shots failed in a row).
    shots_since_reload:
        How many shots were processed since the last reload (or run start).
        Logged for diagnostic purposes.
    """
    import gc

    _append_log(
        log_path,
        f"RELOAD start: reason='{reason}'  shots_since_reload={shots_since_reload}",
    )
    if verbose:
        print(f"  [pipeline reload: {reason} after {shots_since_reload} shots]")

    # The old pipeline must already be None at this point (caller's responsibility).
    # gc.collect() frees any remaining cyclic references, then we clear CUDA cache.
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

    new_pipeline = _load_text_generation_pipeline(project_path, model_name)
    _append_log(
        log_path,
        f"RELOAD complete: reason='{reason}'  shots_since_reload={shots_since_reload}",
    )
    if verbose:
        print(f"  [pipeline reloaded successfully]")
    return new_pipeline


def _call_model(pipeline, messages: List[Dict[str, Any]], overrides: Optional[Dict[str, Any]] = None, images: Optional[List] = None) -> Tuple[str, str]:
    """Call the HF pipeline and return (full_text, generated_only).

    When *images* (a list of PIL.Image objects) is provided **and** the
    pipeline's tokenizer is an AutoProcessor with an image_processor, the
    frames are embedded into the request via the processor's chat template so
    that vision-language models (Gemma 4, Qwen 3, etc.) actually receive
    pixel data alongside the text prompt.
    """
    # Flatten messages to plain text for fallback paths that expect a string.
    def _fallback_text() -> str:
        parts = []
        for msg in messages:
            c = msg.get("content", "")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                for item in c:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text", ""))
        return "\n\n".join(parts)

    res = None
    # Prefer to run generation directly on the model for precise control and
    # to avoid pipeline-level defaults that can introduce conflicting kwargs
    # (which produce repeated warnings). We use the pipeline's tokenizer +
    # model for this.
    model = getattr(pipeline, "model", None) 
    tokenizer = getattr(pipeline, "tokenizer", None) 
    if model is None or tokenizer is None:
        # Fallback to pipeline call if structure unexpected
        try:
            _txt = _fallback_text()
            res = pipeline(_txt, return_full_text=False)
            if isinstance(res, list) and res:
                full = res[0].get("generated_text", "")
            elif isinstance(res, dict):
                full = res.get("generated_text", "")
            else:
                full = ""
            generated = full
            return full, generated
        except Exception:
            raise

    # Build multimodal chat messages. Images are injected into the user turn
    # so that vision-language models receive pixel data alongside the text.
    chat_messages: List[Dict[str, Any]] = []
    for _msg in messages:
        if _msg["role"] == "user":
            _user_text = _msg["content"] if isinstance(_msg["content"], str) else (
                " ".join(p.get("text", "") for p in _msg["content"]
                         if isinstance(p, dict) and p.get("type") == "text")
            )
            if images:
                chat_messages.append({
                    "role": "user",
                    "content": [{"type": "image"} for _ in images] + [{"type": "text", "text": _user_text}],
                })
            else:
                chat_messages.append({"role": "user", "content": [{"type": "text", "text": _user_text}]})
        else:
            chat_messages.append(_msg)
    try:
        formatted = tokenizer.apply_chat_template(
            chat_messages, tokenize=False, add_generation_prompt=True
        )
    except Exception:
        formatted = _fallback_text()
    try:
        if images:
            inputs = tokenizer(
                images=images,
                text=formatted,
                return_tensors="pt",
                padding=True,
            )
        else:
            inputs = tokenizer(text=formatted, return_tensors="pt")
    except Exception as exc:
        raise RuntimeError(
            f"Processor failed to encode inputs: {exc}. "
            f"Ensure the model is a vision-language variant (e.g. Qwen3-VL-8B-Thinking)."
        ) from exc

    # Build a cleaned generation_config
    try:
        from copy import deepcopy
        base_cfg = getattr(model, "generation_config", None) or getattr(model, "config", None)
        gen_cfg = deepcopy(base_cfg) if base_cfg is not None else None
    except Exception:
        gen_cfg = None

    if gen_cfg is None:
        try:
            from transformers import GenerationConfig

            gen_cfg = GenerationConfig()
        except Exception:
            gen_cfg = None

    if gen_cfg is not None:
        try:
            setattr(gen_cfg, "max_new_tokens", 512)
        except Exception:
            pass
        try:
            setattr(gen_cfg, "do_sample", False)
        except Exception:
            pass
        try:
            setattr(gen_cfg, "max_length", None)
        except Exception:
            pass
        # Clear sampling-related params to avoid warnings
        for attr in ("temperature", "top_p", "top_k", "top_h", "typical_p"):
            if hasattr(gen_cfg, attr):
                try:
                    setattr(gen_cfg, attr, None)
                except Exception:
                    pass

    # Apply per-call overrides (used for retries with different generation settings)
    if gen_cfg is not None and overrides:
        try:
            for k, v in overrides.items():
                try:
                    setattr(gen_cfg, k, v)
                except Exception:
                    # best-effort: ignore unknown attrs
                    pass
        except Exception:
            pass

    # If sampling is disabled after applying overrides, ensure sampling params are cleared
    try:
        if gen_cfg is not None and getattr(gen_cfg, "do_sample", False) is False:
            for attr in ("temperature", "top_p", "top_k", "top_h", "typical_p"):
                if hasattr(gen_cfg, attr):
                    try:
                        setattr(gen_cfg, attr, None)
                    except Exception:
                        pass
    except Exception:
        pass

    # Call model.generate with explicit generation_config when possible
    _device_log = ""  # populated below; always defined so return is safe
    try:
        import torch
        # Resolve the model's current device immediately before generation.
        # Doing this here (not at tokenization time) means we pick up any device
        # change that happened between tokenization and now, and silently-swallowed
        # failures earlier in this function can't leave tensors on the wrong device.
        try:
            _gen_device = next(model.parameters()).device
        except StopIteration:
            _gen_device = torch.device("cpu")
        # Move every tensor in inputs to the model device.  Do not swallow this:
        # if the move fails (OOM, wrong device type) we want to raise into the
        # retry loop rather than silently passing CPU tensors to a CUDA model.
        try:
            inputs = inputs.to(_gen_device)
        except Exception as _to_exc:
            # Last-resort: manual per-key move so partial moves are better than none
            for _k, _v in list(inputs.items()):
                if hasattr(_v, "to"):
                    inputs[_k] = _v.to(_gen_device)
    except ImportError:
        _gen_device = None  # no torch — leave inputs as-is and let generate raise natively

    # Log device placement immediately before generation so failures are visible
    # in the log file even when the run is non-verbose.
    try:
        _input_ids_device = str(inputs["input_ids"].device)
        _model_device = str(next(model.parameters()).device)
        _device_log = f"pre-generate: input_ids.device={_input_ids_device}  model.device={_model_device}"
        if _input_ids_device != _model_device:
            _device_log += "  *** DEVICE MISMATCH ***"
    except Exception as _dev_log_exc:
        _device_log = f"pre-generate: could not read devices ({_dev_log_exc})"

    # model.generate should return full sequences including the prompt
    if gen_cfg is not None:
        outputs = model.generate(**inputs, generation_config=gen_cfg)
    else:
        outputs = model.generate(**inputs, max_new_tokens=512, do_sample=False)

    # Decode outputs: outputs is tensor (batch, seq_len)
    try:
        seq = outputs[0].cpu().tolist()
    except Exception:
        try:
            seq = outputs[0].tolist()
        except Exception:
            seq = []

    # input length to slice generated-only tokens
    input_len = 0
    try:
        input_len = inputs["input_ids"].shape[1]
    except Exception:
        input_len = 0

    try:
        full = tokenizer.decode(seq, skip_special_tokens=True)
    except Exception:
        full = ""

    try:
        gen_ids = seq[input_len:]
        generated = tokenizer.decode(gen_ids, skip_special_tokens=True) if gen_ids else ""
    except Exception:
        generated = full

    # Release the output tensor promptly — over hundreds of shots this prevents
    # GPU memory accumulation that can later cause device-placement failures.
    try:
        del outputs
    except Exception:
        pass

    return full, generated, _device_log


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    s = text.find("{")
    e = text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None
    candidate = text[s : e + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Best-effort fallback: try replace single quotes
        try:
            return json.loads(candidate.replace("'", '"'))
        except Exception:
            return None
def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    # Scan for balanced JSON braces and try to parse the first valid object
    L = len(text)
    for start in range(L):
        if text[start] != "{":
            continue
        depth = 0
        for i in range(start, L):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        # try quick heuristics: replace single quotes
                        try:
                            return json.loads(candidate.replace("'", '"'))
                        except Exception:
                            # not valid JSON, continue searching
                            break
                    except Exception:
                        break

    # Fallback: try the original simple approach
    s = text.find("{")
    e = text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None
    candidate = text[s : e + 1]
    try:
        return json.loads(candidate)
    except Exception:
        try:
            return json.loads(candidate.replace("'", '"'))
        except Exception:
            return None


def _build_prompt(
    system_prompt: str,
    movie: Dict[str, Any],
    shot: Dict[str, Any],
    frames: List[str],
    frames_per_shot: int,
    vision_input: bool = False,
) -> str:
    parts: List[str] = []
    # Add a short guard to prevent the model from echoing the prompt/template.
    guard = (
        "IMPORTANT: Do not repeat the prompt or any example content. "
    )
    parts.append((guard + "\n\n" + system_prompt.strip()).strip())
    parts.append("\n\n---\nContext:\n")
    parts.append(f"Movie: {movie.get('title', '')} (tmdb: {movie.get('tmdb', '')})\nFilename: {movie.get('filename', '')}\n")
    parts.append(f"Shot index: {shot.get('index')}  Scene: {shot.get('Scene', '')}\n")
    parts.append(f"Start: {shot.get('start_time', '')}  End: {shot.get('end_time', '')}\n")
    if frames:
        parts.append(f"Sampled {len(frames)} frames: {', '.join(Path(f).name for f in frames)}\n")
        if vision_input:
            parts.append("(Frames are embedded as images in this request — analyse them directly.)\n")
        else:
            parts.append("(Frames are provided as reference filenames.)\n")
    parts.append("\n\n")
    return "\n".join(parts)


def annotate_file_shots(
    project_path: str,
    filename: str,
    media_type: str = "movies",
    model_name: str = "gemma-4-E4B",
    prompt_file: Optional[str] = None,
    prompt_text: Optional[str] = None,
    user_prompt_file: Optional[str] = None,
    frames_per_shot: int = FRAMES_PER_SHOT,
    sample_mode: str = "center",
    force: bool = False,
    skip_existing: bool = True,
    export_csv: Optional[str] = None,
    export_md: Optional[str] = None,
    scene_number: Optional[int] = None,
        shot_index: Optional[int] = None,
        limit: Optional[int] = None,
        verbose: bool = False,
    on_shot_done=None,
    stop_event=None,
    write_log: bool = False,
    reload_every_n_shots: int = 25,
) -> Dict[str, Any]:
    """Annotate all shots in a single file and write canonical outputs.

    Returns a summary dict with counts and paths.
    """
    from services.shotlist import read_shotlist, write_shotlist
    from services.metadata import get_metadata

    project = Path(project_path)
    shots = read_shotlist(project_path, filename, media_type)

    # Movie metadata lookup (best-effort)
    entries = get_metadata(project_path, media_type=media_type)
    meta = next((e for e in entries if e.get('filename') == filename), {})
    movie_info = {
        "tmdb": meta.get("tmdb") if meta else None,
        "title": meta.get("title") if meta else Path(filename).stem,
        "filename": filename,
    }

    system_prompt, prompt_path = load_system_prompt(project_path, media_type, prompt_file, prompt_text)
    # Sanitize system prompt to remove any large example JSON blocks
    sanitized_prompt = _sanitize_system_prompt(system_prompt)
    prompt_was_sanitized = sanitized_prompt != system_prompt
    system_prompt = sanitized_prompt

    # Load the user prompt template (versioned, with $variable placeholders)
    user_prompt_template, user_prompt_path = load_user_prompt(project_path, media_type, user_prompt_file)
    if user_prompt_template is None:
        user_prompt_template = (
            'Analyze the provided frames for shot $shot_index of "$title" ($year).\n'
            'Start: $timecode_start  End: $timecode_stop  Frames: $framecount'
        )

    # Build output dirs: annotations are stored under data/annotations/shots/<media_type>/<stem>.json
    stem = Path(filename).stem
    annotations_dir = project / "data" / "annotations" / "shots" / media_type
    annotations_dir.mkdir(parents=True, exist_ok=True)
    # Track whether we modified the shotlist CSV entries (we no longer write full
    # annotation JSON into the shotlist; this avoids duplicating data)
    shotlist_modified = False

    # Resolve video path early so we can fail fast before loading the model
    video_path = project / "media" / "videos" / media_type / filename
    if frames_per_shot and frames_per_shot > 0 and not video_path.exists():
        # Only abort if there are also no pre-baked frames for this file
        if not _find_prebaked_frames(str(project_path), media_type, filename, 0, 1):
            raise RuntimeError(
                f"Video not found and no pre-baked frames available: {video_path.name}\n"
                f"Run shot detection first or ensure the video file is accessible."
            )

    # Initialize model pipeline (local-first). If this fails we abort early
    try:
        pipeline = _load_text_generation_pipeline(project_path, model_name)
    except Exception as exc:
        # Fail loudly — do not continue annotation loop without a working pipeline
        raise RuntimeError(f"Failed to initialize model pipeline for '{model_name}': {exc}") from exc

    # Create a per-run log file for debugging generation behavior and outputs
    log_path = annotations_dir / f"{stem}.log" if write_log else None
    if write_log:
        try:
            resolved_name = None
            if hasattr(pipeline, "model") and hasattr(pipeline.model, "config"):
                resolved_name = getattr(pipeline.model.config, "_name_or_path", None)
            _append_log(log_path, f"Loaded pipeline for model_arg='{model_name}' resolved='{resolved_name or model_name}'")
            if prompt_was_sanitized:
                _append_log(log_path, "System prompt was sanitized (example JSON removed) before sending to model")
            try:
                cfg = getattr(pipeline.model, "generation_config", None) or getattr(pipeline.model, "config", None)
                if cfg is not None:
                    try:
                        cfg_dict = cfg.to_dict() if hasattr(cfg, "to_dict") else dict(getattr(cfg, "__dict__", {}))
                        _append_log(log_path, "model.generation_config: " + json.dumps(cfg_dict))
                    except Exception:
                        _append_log(log_path, "model.generation_config: " + str(cfg))
            except Exception as e:
                _append_log(log_path, f"Failed to dump model generation_config: {e}")
            try:
                tok = getattr(pipeline, "tokenizer", None)
                if tok is not None:
                    _append_log(log_path, f"tokenizer: {type(tok).__name__} vocab_size={getattr(tok, 'vocab_size', None)}")
            except Exception:
                pass
        except Exception:
            pass

    results: List[Dict[str, Any]] = []
    updated = 0
    skipped = 0
    failed: List[Tuple[int, str]] = []
    _consecutive_failures = 0  # triggers pipeline reload when too many shots fail in a row
    _shots_since_reload = 0    # counts shots processed since last reload (or run start)

    # video_path already defined above (before model load for early-exit check)

    scene_str = str(scene_number) if scene_number is not None else None

    # Validate shot_index if provided
    if shot_index is not None:
        if shot_index < 0 or shot_index >= len(shots):
            raise IndexError(f"Shot index {shot_index} out of range (0-{len(shots)-1})")

    # Load subtitle file for this film (best-effort; silently absent if not found)
    _srt_entries: List[Tuple[float, float, str]] = []
    _srt_dir = project / "media" / "subtitles" / media_type
    _srt_path = _srt_dir / f"{stem}.srt"
    if not _srt_path.exists():
        # fall back to dash-separated legacy filename
        _srt_path_dash = _srt_dir / f"{stem.replace(' ', '-')}.srt"
        if _srt_path_dash.exists():
            _srt_path = _srt_path_dash
        else:
            _srt_path = None
    if _srt_path is not None:
        _srt_entries = _parse_srt(_srt_path)
        if verbose:
            print(f"  Loaded {len(_srt_entries)} subtitle entries from {_srt_path.name}")

    # Aggregated output path — defined before the loop for incremental writes
    aggregated = annotations_dir / f"{stem}.json"

    # Load any pre-existing aggregated JSON so we can preserve good annotations
    # across runs.  The shotlist CSV never has Shot_Caption populated (annotations
    # are stored here, not there), so this is the only reliable skip-source.
    _existing_agg: Dict[int, Any] = {}
    if aggregated.exists():
        try:
            _raw = json.loads(aggregated.read_text(encoding="utf-8"))
            for _entry in _raw:
                _s = _entry.get("shot") if isinstance(_entry, dict) else None
                if isinstance(_s, dict) and _s.get("shot_id") is not None:
                    _existing_agg[int(_s["shot_id"])] = _entry
        except Exception:
            pass

    for i, shot in enumerate(shots):
        # Allow an external stop signal to abort iteration gracefully
        if stop_event is not None and stop_event.is_set():
            break
        # If a global limit is set, only consider shots with index < limit
        if limit is not None and i >= limit:
            break

        # shot_id is 1-based (shot_index i → shot_id i+1)
        _shot_id = i + 1
        _prior = _existing_agg.get(_shot_id)

        # If a specific scene was requested, skip shots outside that scene
        if scene_str is not None and str(shot.get('Scene', '')) != scene_str:
            if _prior is not None:
                results.append(_prior)
            else:
                results.append({"shot_index": i, "note": "skipped (different scene)"})
            continue

        # If a specific shot index was requested, skip other shots
        if shot_index is not None and i != shot_index:
            if _prior is not None:
                results.append(_prior)
            else:
                results.append({"shot_index": i, "note": "skipped (different shot)"})
            continue

        # Preserve existing valid annotation from the aggregated JSON unless force
        if not force and skip_existing and _prior is not None:
            _prior_ann = _prior.get("shot", {}).get("annotation", {})
            if isinstance(_prior_ann, dict) and _prior_ann.get("setting"):
                skipped += 1
                results.append(_prior)
                continue

        # Sample frames (best-effort). Prefer pre-baked frames under
        # media/frames/<media_type>/<stem>/ if present (useful for debugging).
        frames: List[str] = []
        if frames_per_shot and frames_per_shot > 0:
            try:
                prebaked = _find_prebaked_frames(str(project_path), media_type, filename, i, frames_per_shot)
                if prebaked:
                    frames = prebaked
                    try:
                        _append_log(log_path, f"Using pre-baked frames for shot {i}: {', '.join(Path(p).name for p in frames)}")
                    except Exception:
                        pass
                elif video_path.exists():
                    frames = sample_frames_for_shot(
                        str(video_path),
                        shot.get("start_time", "0:00:00"),
                        shot.get("end_time", shot.get("start_time", "0:00:00")),
                        frames_per_shot,
                        sample_mode,
                    )
            except Exception as exc:
                _append_log(log_path, f"SHOT {i} - frame sampling failed: {exc}")
                failed.append((i, f"frame sampling failed: {exc}"))
                continue

        if not frames:
            _append_log(log_path, f"SHOT {i} - no frames available (video missing or shot has zero duration)")
            failed.append((i, "no frames available"))
            continue

        # Load frames as PIL images — required for vision input.
        # Any failure here is a hard error for the shot: we will not call the
        # model without images since that is the entire point of the tool.
        pil_frames: List = []
        try:
            from PIL import Image as _PILImage
            for fp in frames:
                try:
                    pil_frames.append(_PILImage.open(fp).convert("RGB"))
                except Exception as exc:
                    _append_log(log_path, f"SHOT {i} - failed to open frame {fp}: {exc}")
                    raise RuntimeError(f"Failed to open frame {fp}: {exc}") from exc
        except RuntimeError:
            raise
        except Exception as exc:
            _append_log(log_path, f"SHOT {i} - PIL unavailable or frame load failed: {exc}")
            failed.append((i, f"frame load failed: {exc}"))
            continue

        if not pil_frames:
            _append_log(log_path, f"SHOT {i} - no PIL frames loaded")
            failed.append((i, "no PIL frames loaded"))
            continue

        if verbose:
            frame_names = ", ".join(Path(f).name for f in frames)
            print(f"  Shot {i} [{shot.get('start_time', '?')} → {shot.get('end_time', '?')}] — {len(pil_frames)} frame(s): {frame_names}")

        # Build structured messages with runtime variable substitution
        variables = {
            "title":          movie_info.get("title", ""),
            "year":           str(meta.get("year", "") or ""),
            "director":       str(meta.get("director", "") or ""),
            "filename":       filename,
            "timecode_start": shot.get("start_time", ""),
            "timecode_stop":  shot.get("end_time", ""),
            "framecount":     str(len(pil_frames)),
            "shot_index":     str(i),
            "overview":       str(meta.get("overview", "") or ""),
            "tagline":        str(meta.get("tagline", "") or ""),
            "subtitles":      _subtitles_for_shot(_srt_entries, shot.get("start_time", ""), shot.get("end_time", "")),
        }
        filled_user = _substitute_variables(user_prompt_template, variables)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": filled_user},
        ]

        ann: Optional[Dict[str, Any]] = None
        raw_output = None
        success = False
        try:
            # Retry strategy: deterministic, longer, then sampled
            attempt_configs = [
                {"max_new_tokens": 512, "do_sample": False},
                {"max_new_tokens": 1536, "do_sample": False},
                {"max_new_tokens": 2048, "do_sample": True, "temperature": 0.6, "top_p": 0.95},
            ]

            parsed = None
            last_full = None
            last_generated = None
            for attempt_no, cfg in enumerate(attempt_configs, start=1):
                try:
                    full_raw, raw, _dev_log = _call_model(pipeline, messages, overrides=cfg, images=pil_frames if pil_frames else None)
                    last_full = full_raw
                    last_generated = raw
                    _append_log(log_path, f"SHOT {i} ATTEMPT {attempt_no} cfg={cfg}  {_dev_log}")
                    if verbose:
                        print(f"    Attempt {attempt_no}/{len(attempt_configs)} (max_new_tokens={cfg.get('max_new_tokens')})...")
                        if _dev_log and "MISMATCH" in _dev_log:
                            print(f"    [WARNING] {_dev_log}", file=sys.stderr)
                    _append_log(log_path, "MODEL_FULL_OUTPUT (truncated):\n" + (full_raw[:8000] + "..." if full_raw and len(full_raw) > 8000 else (full_raw or "<empty>")))
                    _append_log(log_path, "MODEL_GENERATED_ONLY (truncated):\n" + (raw[:4000] + "..." if raw and len(raw) > 4000 else (raw or "<empty>")))
                    parsed = _extract_json(raw) or _extract_json(full_raw)
                    if parsed:
                        ann = _validate_annotation(parsed)
                        success = True
                        break
                except Exception as e:
                    _append_log(log_path, f"SHOT {i} ATTEMPT {attempt_no} exception: {e}")
                    continue

            if not parsed:
                # Record last outputs for debugging
                ann = {"model_output": (last_generated or "").strip(), "model_output_full": (last_full or "").strip()}
                failed.append((i, "no JSON found in model output after retries"))
                try:
                    _append_log(log_path, f"SHOT {i} - no JSON found after {len(attempt_configs)} attempts")
                    _append_log(log_path, "PROMPT (user):\n" + (filled_user[:2000] + "..." if len(filled_user) > 2000 else filled_user))
                    _append_log(log_path, "MODEL_FULL_OUTPUT:\n" + (last_full[:8000] + "..." if last_full and len(last_full) > 8000 else (last_full or "<empty>")))
                    _append_log(log_path, "MODEL_GENERATED_ONLY:\n" + (last_generated[:4000] + "..." if last_generated and len(last_generated) > 4000 else (last_generated or "<empty>")))
                except Exception:
                    pass
                success = False
        except Exception as exc:
            failed.append((i, str(exc)))
            ann = {"error": str(exc), "raw_output": raw_output}
            try:
                _append_log(log_path, f"SHOT {i} - exception during model call: {exc}")
                _append_log(log_path, "TRACEBACK:\n" + traceback.format_exc())
                _append_log(log_path, "MODEL_FULL_OUTPUT:\n" + (raw_output[:8000] + "..." if raw_output and len(raw_output) > 8000 else (raw_output or "<empty>")))
                _append_log(log_path, "PROMPT (user):\n" + (filled_user[:2000] + "..." if len(filled_user) > 2000 else filled_user))
            except Exception:
                pass

        # Compose canonical structure
        shot_annotation = {
            "movie": movie_info,
            "annotation": {
                "type": "shot",
                "model": model_name,
                "frames_per_shot": frames_per_shot,
                "prompt_file": prompt_path,
            },
            "shot": {
                "shot_id": i + 1,
                "annotation": ann,
            },
        }

        # (No per-shot JSON files) — we'll write a single aggregated JSON file below

        # Do NOT write the full annotation into the shotlist CSV `Shot_Caption`.
        # Annotation results are kept in the aggregated JSON under
        # data/annotations/shots/<media_type>/<stem>.json to keep shotlists clean.
        # If you want a short marker stored in the CSV, set `shotlist_modified=True`
        # and update the relevant field here. Currently we avoid modifying the CSV.

        if success:
            results.append(shot_annotation)
            updated += 1
            _consecutive_failures = 0
            if verbose:
                print(f"    ✓ Shot {i}")
        else:
            # Do NOT append failed shots to results: they are omitted from the JSON
            # so the validator shows them as unannotated (? not ✗), and they are
            # retried automatically on the next run without --force.
            _consecutive_failures += 1
            if _consecutive_failures >= 5:
                # Consecutive failures indicate device-drift or memory corruption
                # (classic symptom: input_ids on cpu while model is on cuda).
                # Set pipeline=None HERE so gc.collect() inside _reload_pipeline
                # can actually free the GPU memory before loading the new model.
                try:
                    pipeline = None
                    pipeline = _reload_pipeline(
                        project_path, model_name, log_path, verbose,
                        "consecutive_failures", _shots_since_reload,
                    )
                    _shots_since_reload = 0
                    _consecutive_failures = 0
                except Exception as _reload_exc:
                    _append_log(log_path, f"RELOAD failed (consecutive_failures): {_reload_exc}")
                    if verbose:
                        print(f"  [pipeline reload failed: {_reload_exc}]", file=sys.stderr)
            if verbose:
                reason = failed[-1][1] if failed and failed[-1][0] == i else "failed"
                print(f"    ✗ Shot {i}: {reason}")

        # Count this shot as processed (attempted annotation, not a skip).
        # Trigger a periodic pipeline reload after every reload_every_n_shots attempts
        # to prevent the model drifting into generic/repetitive outputs.
        _shots_since_reload += 1
        if reload_every_n_shots > 0 and _shots_since_reload >= reload_every_n_shots:
            try:
                # Set pipeline=None HERE so gc.collect() inside _reload_pipeline
                # can actually free the GPU memory before loading the new model.
                pipeline = None
                pipeline = _reload_pipeline(
                    project_path, model_name, log_path, verbose,
                    "interval", _shots_since_reload,
                )
                _shots_since_reload = 0
                _consecutive_failures = 0
            except Exception as _reload_exc:
                _append_log(log_path, f"RELOAD failed (interval): {_reload_exc}")
                if verbose:
                    print(f"  [periodic pipeline reload failed: {_reload_exc}]", file=sys.stderr)

        # small pause to be polite to model infra
        time.sleep(0.1)

        # Incremental write — keeps the JSON current so GUI watchers see each result
        try:
            aggregated.write_text(json.dumps(results, indent=2), encoding="utf-8")
        except Exception:
            pass
        if on_shot_done is not None and success:
            on_shot_done(i)
        if stop_event is not None and stop_event.is_set():
            break

    # Write back shotlist CSV only if we actually modified it
    if shotlist_modified:
        try:
            write_shotlist(project_path, filename, media_type, shots)
        except Exception:
            pass

    # If the run produced no successful annotations but had failures,
    # treat this as a hard failure: do not write any aggregated output and
    # abort with a clear error so callers do not pick up useless error rows.
    _stopped_early = stop_event is not None and stop_event.is_set()
    if not _stopped_early and updated == 0 and len(failed) > 0:
        raise RuntimeError(f"Annotation failed: no successful annotations produced; {len(failed)} shots failed. Aborting without writing output.")

    # Final canonical write (incremental writes inside the loop keep this current;
    # this also covers the all-skipped case where no incremental writes ran)
    try:
        aggregated.write_text(json.dumps(results, indent=2), encoding="utf-8")
    except Exception:
        pass

    # Optional exports
    if export_csv:
        try:
            _export_annotations_csv(results, Path(export_csv))
        except Exception:
            pass
    if export_md:
        try:
            _export_annotations_markdown(results, Path(export_md))
        except Exception:
            pass

    summary = {
        "filename": filename,
        "movie": movie_info,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "annotations_path": str(aggregated),
    }
    return summary


def _export_annotations_csv(results: List[Dict[str, Any]], dest: Path) -> None:
    import csv

    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["shot_id", "start_time", "end_time", "scene", "main_focus", "overall_description"])
        for r in results:
            shot = r.get("shot", {})
            ann = shot.get("annotation") if isinstance(shot, dict) else None
            if isinstance(ann, dict):
                main = ann.get("main_focus") if isinstance(ann.get("main_focus"), (list, str)) else ""
                if isinstance(main, list):
                    main = ", ".join(main)
                writer.writerow([
                    shot.get("shot_id"),
                    ann.get("start_time") if ann else "",
                    ann.get("end_time") if ann else "",
                    ann.get("Scene") if ann else "",
                    main,
                    ann.get("overall_description") if ann else "",
                ])


def _export_annotations_markdown(results: List[Dict[str, Any]], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as f:
        for r in results:
            shot = r.get("shot", {})
            ann = shot.get("annotation") if isinstance(shot, dict) else None
            f.write(f"## Shot {shot.get('shot_id')}\n\n")
            if isinstance(ann, dict):
                f.write(f"- **Overall**: {ann.get('overall_description', '')}\n")
                f.write(f"- **Framing**: {ann.get('framing', '')}\n")
                f.write(f"- **Main focus**: {', '.join(ann.get('main_focus', [])) if isinstance(ann.get('main_focus'), list) else ann.get('main_focus', '')}\n")
                f.write("\n")


def annotate_all_files(
    project_path: str,
    media_type: str = "movies",
    model_name: str = "gemma-4-26B-A4B",
    prompt_file: Optional[str] = None,
    prompt_text: Optional[str] = None,
    user_prompt_file: Optional[str] = None,
    frames_per_shot: int = FRAMES_PER_SHOT,
    sample_mode: str = "center",
    force: bool = False,
    skip_existing: bool = True,
    limit: Optional[int] = None,
    verbose: bool = False,
    write_log: bool = False,
    reload_every_n_shots: int = 25,
) -> List[Dict[str, Any]]:
    from services.metadata import get_metadata

    entries = get_metadata(project_path, media_type=media_type)
    results = []
    for e in entries:
        fn = e.get("filename")
        if not fn:
            continue
        if verbose:
            print(f"\n--- {fn} ---")
        summary = annotate_file_shots(
            project_path,
            fn,
            media_type=media_type,
            model_name=model_name,
            prompt_file=prompt_file,
            prompt_text=prompt_text,
            user_prompt_file=user_prompt_file,
            frames_per_shot=frames_per_shot,
            sample_mode=sample_mode,
            force=force,
            skip_existing=skip_existing,
            limit=limit,
            verbose=verbose,
            write_log=write_log,
            reload_every_n_shots=reload_every_n_shots,
        )
        results.append(summary)
    return results


def get_annotation_json_path(project_path: str, filename: str, media_type: str) -> Path:
    """Return the Path to the annotation JSON for *filename*."""
    stem = Path(filename).stem
    return Path(project_path) / "data" / "annotations" / "shots" / media_type / f"{stem}.json"


def remove_file_annotations(
    project_path: str,
    filename: str,
    media_type: str = "movies",
) -> bool:
    """Delete the shot-annotation JSON for *filename*.

    Returns True if the file existed and was removed, False if it was absent.
    """
    path = get_annotation_json_path(project_path, filename, media_type)
    if path.exists():
        path.unlink()
        return True
    return False
