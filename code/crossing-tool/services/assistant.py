"""services/assistant.py — minimal single-turn assistant: linguistic interface to MCP.

Design:
- Dataset queries (movies, films, years, etc.) are detected deterministically.
  Python calls MCP directly, filters results, and passes them to the LLM for
  formatting only. The LLM never retrieves or filters data.
- Non-dataset queries are sent to the LLM with tool schemas; if it calls a tool,
  we execute it and feed the result back for a final formatted response.
- No loops, no planning, no memory, no CPU fallback.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# MCP tool schemas (used for non-dataset queries where LLM decides)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_movies",
            "description": (
                "Return film metadata for the configured crossing project as a JSON string."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "media_type": {
                        "type": "string",
                        "enum": ["movies", "gameplay"],
                        "description": (
                            "Which media library to query. "
                            "Use 'movies' for films (default) or 'gameplay' for gameplay recordings."
                        ),
                    }
                },
                "required": [],
            },
        },
    },
]

_MAX_TOOL_RESULT_CHARS = 12_000


# ---------------------------------------------------------------------------
# Deterministic dataset query classifier
# ---------------------------------------------------------------------------

_DATASET_KEYWORDS = {
    "movie", "movies", "film", "films", "cinema", "title", "titles",
    "database", "list", "between", "year", "years", "director", "directors",
    "release", "released", "all", "show", "find", "search",
}


def _is_dataset_query(text: str) -> bool:
    """Return True if the query is about project data and requires MCP."""
    words = set(re.sub(r"[^\w\s]", " ", text.lower()).split())
    return bool(words & _DATASET_KEYWORDS)


# ---------------------------------------------------------------------------
# Python-side filtering (LLM never filters data)
# ---------------------------------------------------------------------------

def _extract_year_range(query: str):
    """Return (year_min, year_max) extracted from the query, or (None, None)."""
    years = [int(m.group()) for m in re.finditer(r'\b(19|20)\d{2}\b', query)]
    if len(years) >= 2:
        return min(years), max(years)
    if len(years) == 1:
        return years[0], years[0]
    return None, None


def _filter_entries(entries: list, query: str) -> list:
    """Apply deterministic Python-side filtering based on the query."""
    year_min, year_max = _extract_year_range(query)
    if year_min is None:
        return entries
    filtered = []
    for e in entries:
        year = e.get("year")
        if not year:
            continue
        try:
            y = int(str(year)[:4])
            if year_min <= y <= year_max:
                filtered.append(e)
        except (ValueError, TypeError):
            continue
    return filtered


# ---------------------------------------------------------------------------
# VRAM pre-check
# ---------------------------------------------------------------------------

def _check_vram(model_path: str, model_name: str) -> None:
    """Raise RuntimeError if there is not enough free VRAM for the model.

    Estimates VRAM from the size of safetensors weight files + 20% overhead.
    """
    try:
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError(
                f"Insufficient GPU memory for model '{model_name}': no CUDA device available"
            )

        p = Path(model_path)
        if p.is_dir():
            disk_bytes = sum(f.stat().st_size for f in p.rglob("*.safetensors"))
            if disk_bytes == 0:
                disk_bytes = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        elif p.is_file():
            disk_bytes = p.stat().st_size
        else:
            return  # unknown path — let load proceed

        required_bytes = int(disk_bytes * 1.2)
        free_bytes, _ = torch.cuda.mem_get_info(0)

        if free_bytes < required_bytes:
            free_gb = free_bytes / (1 << 30)
            need_gb = required_bytes / (1 << 30)
            raise RuntimeError(
                f"Insufficient GPU memory for model '{model_name}': "
                f"need ~{need_gb:.1f} GB free, only {free_gb:.1f} GB available. "
                "Free up VRAM (e.g. stop other running models) and try again."
            )
    except RuntimeError:
        raise
    except Exception:
        pass  # non-fatal — let the load attempt itself catch OOM


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def _resolve_model_path(model_name: str, project_path: str) -> str:
    project_local = Path(project_path) / "models" / model_name
    if project_local.exists():
        return str(project_local)
    explicit = Path(model_name).expanduser()
    if explicit.exists():
        return str(explicit)
    return model_name


def _load_model(model_path: str, model_name: str):
    """Load a text-only causal LM onto GPU. No CPU fallback."""
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise ImportError(
            "transformers and torch are required for the assistant command"
        ) from exc

    local_only = Path(model_path).exists()

    tokenizer = AutoTokenizer.from_pretrained(
        model_path,
        local_files_only=local_only,
        trust_remote_code=True,
    )

    try:
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            local_files_only=local_only,
            trust_remote_code=True,
            dtype=torch.bfloat16,
            device_map="auto",
        )
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "out of memory" in msg or "cuda" in msg:
            raise RuntimeError(
                f"Insufficient GPU memory for model '{model_name}'"
            ) from exc
        raise

    return tokenizer, model


# ---------------------------------------------------------------------------
# MCP stdio client
# ---------------------------------------------------------------------------

def _call_mcp_tool(tool_name: str, arguments: dict) -> str:
    """Launch the MCP server as a subprocess and call a single tool via stdio."""
    script = Path(__file__).resolve().parent.parent / "crossing_mcp.py"

    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    def _send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def _recv() -> dict:
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed stdout unexpectedly")
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    try:
        _send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "crossing-assistant", "version": "0.1.0"},
            },
        })
        _recv()
        _send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        _send({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
        response = _recv()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    result = response.get("result", {})
    content = result.get("content", [])
    if isinstance(content, list) and content:
        return content[0].get("text", json.dumps(content))
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Tool call parsing (for non-dataset queries)
# ---------------------------------------------------------------------------

def _parse_tool_call(text: str):
    """Return (tool_name, arguments) or (None, None) if no tool call found."""
    # Qwen3 style: <tool_call>{"name": "...", "arguments": {...}}</tool_call>
    match = re.search(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)
    if match:
        try:
            payload = json.loads(match.group(1).strip())
            return payload.get("name"), payload.get("arguments") or {}
        except json.JSONDecodeError:
            pass

    match2 = re.search(
        r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{[^}]*\})\s*\}',
        text,
    )
    if match2:
        try:
            return match2.group(1), json.loads(match2.group(2))
        except json.JSONDecodeError:
            pass

    return None, None


# ---------------------------------------------------------------------------
# LLM generation helper
# ---------------------------------------------------------------------------

def _generate(tokenizer, model, messages: list, max_new_tokens: int,
              model_name: str, tools=None) -> str:
    """Apply chat template and run model.generate. Returns decoded response text."""
    import torch

    template_kwargs: dict = {"add_generation_prompt": True, "return_tensors": "pt"}
    if tools is not None:
        template_kwargs["tools"] = tools

    encoded = tokenizer.apply_chat_template(messages, **template_kwargs)

    try:
        if hasattr(encoded, "input_ids"):
            encoded = encoded.to(model.device)
            with torch.no_grad():
                output_ids = model.generate(**encoded, max_new_tokens=max_new_tokens, do_sample=False)
            input_len = encoded["input_ids"].shape[-1]
        else:
            tensor = encoded.to(model.device)
            with torch.no_grad():
                output_ids = model.generate(tensor, max_new_tokens=max_new_tokens, do_sample=False)
            input_len = tensor.shape[-1]
    except RuntimeError as exc:
        if "out of memory" in str(exc).lower():
            raise RuntimeError(
                f"Insufficient GPU memory to run model '{model_name}'. "
                "Free up VRAM (e.g. stop other running models) and try again."
            ) from exc
        raise

    return tokenizer.decode(output_ids[0][input_len:], skip_special_tokens=True).strip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_query(query_text: str, model_name: str, project_path: str, verbose: bool = False) -> str:
    """Run a single-turn query and return a formatted text response.

    Dataset queries (containing movie/film/year/list/etc. keywords):
      - Python calls MCP directly — no LLM tool decision required
      - Python filters the result (e.g. by year range)
      - LLM receives only the filtered data and formats it for display

    Non-dataset queries:
      - Sent to LLM with tool schemas
      - If LLM calls a tool, execute it and feed result back for formatting
      - If LLM answers directly, return its response
    """
    model_path = _resolve_model_path(model_name, project_path)
    _check_vram(model_path, model_name)
    tokenizer, model = _load_model(model_path, model_name)

    if _is_dataset_query(query_text):
        # ---- Dataset query: Python drives MCP, LLM only formats ----
        if verbose:
            print(f"[assistant] dataset query detected", file=sys.stderr)
            print(f"[assistant] calling MCP tool: list_movies(media_type='movies')", file=sys.stderr)
        raw = _call_mcp_tool("list_movies", {"media_type": "movies"})
        try:
            entries = json.loads(raw)
            if isinstance(entries, dict) and "error" in entries:
                raise RuntimeError(f"MCP error: {entries['error']}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Failed to parse MCP response: {exc}") from exc

        filtered = _filter_entries(entries, query_text)
        if verbose:
            year_min, year_max = _extract_year_range(query_text)
            year_str = f" (year filter: {year_min}–{year_max})" if year_min else ""
            print(f"[assistant] MCP returned {len(entries)} entries → {len(filtered)} after Python filtering{year_str}", file=sys.stderr)

        data_str = json.dumps(filtered, indent=2)
        if len(data_str) > _MAX_TOOL_RESULT_CHARS:
            data_str = data_str[:_MAX_TOOL_RESULT_CHARS] + "\n... [truncated]"

        messages = [
            {
                "role": "user",
                "content": (
                    f"The user asked: {query_text}\n\n"
                    f"Here is the data retrieved from the database:\n{data_str}\n\n"
                    "Format this as a clean, human-readable response. "
                    "Do not add any information not present in the data above."
                ),
            }
        ]
        return _generate(tokenizer, model, messages, max_new_tokens=512, model_name=model_name)

    else:
        # ---- General query: LLM decides whether to call a tool ----
        if verbose:
            print(f"[assistant] general query — sending to LLM with tool schemas", file=sys.stderr)
        messages = [{"role": "user", "content": query_text}]
        response_text = _generate(
            tokenizer, model, messages, max_new_tokens=512,
            model_name=model_name, tools=TOOLS,
        )

        tool_name, tool_args = _parse_tool_call(response_text)
        if tool_name is None:
            return response_text

        if verbose:
            print(f"[assistant] LLM called tool: {tool_name}({tool_args})", file=sys.stderr)
        tool_result = _call_mcp_tool(tool_name, tool_args)
        if len(tool_result) > _MAX_TOOL_RESULT_CHARS:
            tool_result = tool_result[:_MAX_TOOL_RESULT_CHARS] + "\n... [truncated]"

        messages.append({"role": "assistant", "content": response_text})
        messages.append({"role": "tool", "name": tool_name, "content": tool_result})
        return _generate(
            tokenizer, model, messages, max_new_tokens=1024,
            model_name=model_name, tools=TOOLS,
        )
