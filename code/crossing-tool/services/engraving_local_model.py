"""Local Qwen-Image-Edit model loading for the engraving converter.

Owns exactly one pipeline per run.  Both the repair and engrave stages share
the same underlying model, so instantiating it per object would dominate
runtime; :class:`LocalEngravingModel` is created once by the pipeline
orchestrator and passed down to every stage call.

Nothing here reaches the network: the pipeline is loaded from
``<project>/models/<dir_name>`` with ``local_files_only=True``, and the
transformer is loaded from a local GGUF file when a quantised backend is
selected.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from services.engraving_local_config import gguf_filename, model_dir

# Fragmentation guard for the large activation tensors the 20B transformer
# allocates; must be set before the CUDA allocator is first used.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")


class ModelNotAvailableError(RuntimeError):
    """Raised when the local model directory is missing or incomplete."""


def _required_subdirs() -> tuple[str, ...]:
    return ("text_encoder", "tokenizer", "vae", "scheduler", "transformer")


def validate_model(project_path: str | Path, config: dict) -> Path:
    """Return the local model directory, raising if it is not usable offline."""
    directory = model_dir(project_path, config)
    if not directory.is_dir():
        raise ModelNotAvailableError(
            f"Local engraving model not found:\n  {directory}\n\n"
            f"Run:  crossing engraving local-download"
        )

    missing = [name for name in _required_subdirs() if not (directory / name).is_dir()]
    if missing:
        raise ModelNotAvailableError(
            f"Local engraving model is incomplete:\n  {directory}\n"
            f"Missing: {', '.join(missing)}\n\n"
            f"Run:  crossing engraving local-download --force"
        )

    backend = config["model"]["transformer_backend"]
    if backend != "bf16":
        name = gguf_filename(backend)
        if not name:
            raise ModelNotAvailableError(f"No GGUF filename registered for backend {backend!r}")
        if not (directory / name).is_file():
            raise ModelNotAvailableError(
                f"Quantised transformer weights not found:\n  {directory / name}\n\n"
                f"Run:  crossing engraving local-download --backend {backend}"
            )
    return directory


def describe_device() -> dict:
    """Return a record of the compute device, for run provenance."""
    import torch

    record = {
        "torch_version": torch.__version__,
        "cuda_version": torch.version.cuda,
        "cuda_available": bool(torch.cuda.is_available()),
        "device_name": None,
        "compute_capability": None,
        "total_vram_bytes": None,
    }
    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        record["device_name"] = props.name
        record["compute_capability"] = f"{props.major}.{props.minor}"
        record["total_vram_bytes"] = int(props.total_memory)
    return record


def download_model(
    project_path: str | Path,
    config: dict,
    *,
    force: bool = False,
    progress=None,
) -> dict:
    """Fetch the weights needed for offline inference into ``<project>/models``.

    Downloads the pipeline components from the Apache-2.0 upstream repo and,
    for a quantised backend, the single GGUF transformer file.  The bf16
    transformer shards are skipped unless the ``bf16`` backend is selected,
    which keeps the footprint at roughly 36 GB instead of 58 GB.
    """
    from huggingface_hub import hf_hub_download, snapshot_download

    from services.engraving_local_config import MODEL_SOURCES

    directory = model_dir(project_path, config)
    directory.mkdir(parents=True, exist_ok=True)
    backend = config["model"]["transformer_backend"]

    def _say(message: str) -> None:
        if progress:
            progress(message)

    ignore = ["*.png", "*.jpg", "*.jpeg"]
    if backend != "bf16":
        ignore.append("transformer/*.safetensors")

    _say(f"Downloading pipeline components from {MODEL_SOURCES['pipeline_repo']} …")
    snapshot_download(
        repo_id=MODEL_SOURCES["pipeline_repo"],
        local_dir=str(directory),
        ignore_patterns=ignore,
        force_download=force,
        max_workers=8,
    )

    weights_path = None
    if backend != "bf16":
        name = gguf_filename(backend)
        if not name:
            raise ModelNotAvailableError(f"No GGUF filename registered for backend {backend!r}")
        _say(f"Downloading quantised transformer {name} …")
        weights_path = hf_hub_download(
            repo_id=MODEL_SOURCES["transformer_gguf_repo"],
            filename=name,
            local_dir=str(directory),
            force_download=force,
        )

    total = sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
    return {
        "model_dir": str(directory),
        "transformer_backend": backend,
        "transformer_weights": weights_path,
        "total_bytes": total,
        "pipeline_repo": MODEL_SOURCES["pipeline_repo"],
        "pipeline_license": MODEL_SOURCES["pipeline_license"],
        "transformer_gguf_repo": (
            MODEL_SOURCES["transformer_gguf_repo"] if backend != "bf16" else None
        ),
        "transformer_gguf_license": (
            MODEL_SOURCES["transformer_gguf_license"] if backend != "bf16" else None
        ),
    }


class LocalEngravingModel:
    """Run-scoped wrapper around a ``QwenImageEditPlusPipeline``."""

    def __init__(self, project_path: str | Path, config: dict):
        self.project_path = Path(project_path)
        self.config = config
        self.model_dir = validate_model(project_path, config)
        self.backend = config["model"]["transformer_backend"]
        self._pipe = None
        self._prompt_cache: dict = {}
        self.load_seconds: float | None = None

    # -- loading ---------------------------------------------------------

    def _load_transformer(self, torch, dtype):
        """Return the transformer, from GGUF when a quantised backend is set."""
        if self.backend == "bf16":
            return None  # let from_pretrained load the safetensors transformer

        from diffusers import GGUFQuantizationConfig, QwenImageTransformer2DModel

        weights = self.model_dir / gguf_filename(self.backend)
        return QwenImageTransformer2DModel.from_single_file(
            str(weights),
            quantization_config=GGUFQuantizationConfig(compute_dtype=dtype),
            config=str(self.model_dir),
            subfolder="transformer",
            dtype=dtype,
        )

    @property
    def pipe(self):
        """Load the pipeline on first use and cache it for the whole run."""
        if self._pipe is not None:
            return self._pipe

        import torch
        from diffusers import QwenImageEditPlusPipeline

        started = time.time()
        dtype = torch.bfloat16

        kwargs = {"dtype": dtype, "local_files_only": True}
        transformer = self._load_transformer(torch, dtype)
        if transformer is not None:
            kwargs["transformer"] = transformer

        pipe = QwenImageEditPlusPipeline.from_pretrained(str(self.model_dir), **kwargs)

        if self.config["model"].get("cpu_offload"):
            pipe.enable_model_cpu_offload()
        elif torch.cuda.is_available():
            pipe.to("cuda")

        pipe.set_progress_bar_config(disable=True)
        self._pipe = pipe
        self.load_seconds = time.time() - started
        return self._pipe

    # -- inference -------------------------------------------------------

    def edit(
        self,
        *,
        images: list,
        prompt: str,
        negative_prompt: str,
        num_inference_steps: int,
        true_cfg_scale: float,
        guidance_scale: float,
        seed: int,
        height: int,
        width: int,
    ) -> dict:
        """Run one edit pass and return the image plus timing/VRAM facts.

        *height* and *width* are always passed explicitly: with multiple
        reference images the pipeline would otherwise derive the output shape
        from a reference, so a wide source frame silently produced a wide
        result instead of matching the square working canvas.
        """
        import torch

        pipe = self.pipe
        device = "cuda" if torch.cuda.is_available() else "cpu"

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()

        generator = torch.Generator(device=device).manual_seed(int(seed))
        started = time.time()
        with torch.inference_mode():
            result = pipe(
                image=images,
                prompt=prompt,
                negative_prompt=negative_prompt,
                height=int(height),
                width=int(width),
                num_inference_steps=int(num_inference_steps),
                true_cfg_scale=float(true_cfg_scale),
                guidance_scale=float(guidance_scale),
                num_images_per_prompt=1,
                generator=generator,
            )
        elapsed = time.time() - started

        peak = int(torch.cuda.max_memory_allocated()) if torch.cuda.is_available() else None
        return {
            "image": result.images[0],
            "seconds": elapsed,
            "peak_vram_bytes": peak,
        }

    def release(self) -> None:
        """Drop the pipeline and free GPU memory."""
        if self._pipe is None:
            return
        import torch

        del self._pipe
        self._pipe = None
        self._prompt_cache.clear()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def provenance(self) -> dict:
        """Return a record of exactly which weights produced a run."""
        from services.engraving_local_config import MODEL_SOURCES

        weights_file = None
        if self.backend != "bf16":
            name = gguf_filename(self.backend)
            if name:
                weights_file = str(self.model_dir / name)

        return {
            "model_dir": str(self.model_dir),
            "transformer_backend": self.backend,
            "transformer_weights": weights_file,
            "pipeline_repo": MODEL_SOURCES["pipeline_repo"],
            "pipeline_license": MODEL_SOURCES["pipeline_license"],
            "transformer_gguf_repo": (
                MODEL_SOURCES["transformer_gguf_repo"] if self.backend != "bf16" else None
            ),
            "transformer_gguf_license": (
                MODEL_SOURCES["transformer_gguf_license"] if self.backend != "bf16" else None
            ),
            "cpu_offload": bool(self.config["model"].get("cpu_offload")),
            "load_seconds": self.load_seconds,
            "device": describe_device(),
        }
