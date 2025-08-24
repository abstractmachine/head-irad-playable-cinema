import sys, json, shutil, subprocess, re
from pathlib import Path

def _check_metal_gpu() -> tuple[bool, str]:
    """Best-effort check for macOS Metal/MPS GPU usage by Ollama."""
    if sys.platform != "darwin":
        return False, "not macOS"

    # 1) Confirm we're calling an arm64 Ollama (avoid Rosetta)
    cli = shutil.which("ollama")
    if not cli:
        return False, "ollama CLI not found on PATH"
    try:
        p = subprocess.run(["file", cli], capture_output=True, text=True, timeout=2)
        if "arm64" not in (p.stdout + p.stderr):
            return False, "ollama binary is not arm64 (likely Rosetta/x86)"
    except Exception as e:
        return False, f"failed to inspect ollama binary: {e}"

    # 2) Parse recent server log for Metal signals
    log = Path.home() / ".ollama" / "logs" / "server.log"
    if not log.exists():
        return False, "server.log not found (is ollama serve running?)"

    try:
        # read only the trailing chunk to keep it fast
        tail = log.read_text(encoding="utf-8", errors="ignore")[-20000:].lower()
    except Exception as e:
        return False, f"could not read server.log: {e}"

    signals = {
        "ggml_metal_init": re.search(r"ggml_metal_init", tail),
        "offloaded_layers": re.search(r"offloaded\s+\d+/\d+\s+layers\s+to\s+gpu", tail),
        "device_metal": re.search(r"device=metal", tail),
        "library_metal": re.search(r"library=metal", tail),
    }
    if any(signals.values()):
        # Optional: summarize what we saw for debugging
        seen = [k for k, v in signals.items() if v]
        return True, " & ".join(seen) + " in server.log"

    return False, "no Metal markers in recent server.log (looked for ggml_metal_init/offloaded/device=Metal/library=metal)"