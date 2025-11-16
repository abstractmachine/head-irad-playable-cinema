import argparse
import sys
import os
from pathlib import Path

# -------------------------------------------------
# Unified parser (flat root + optional command flags)
# -------------------------------------------------

class _DispatchAction(argparse.Action):
    def __init__(self, option_strings, dest, nargs=None, **kwargs):
        self._cmd = kwargs.pop("cmd", None)
        self._func = kwargs.pop("func", None)
        super().__init__(option_strings, dest, nargs=nargs, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, "cmd", self._cmd)
        if callable(self._func):
            setattr(namespace, "func", self._func)
        setattr(namespace, self.dest, values)


def cmd_legacy(ns):
    if ns.verbose:
        print("[legacy] Parsed flat arguments")
    
    parser = build_parser()
    print("No command selected. Use one of: --list, --detect, --extract_frames, --annotate, --erase, --process\n")
    parser.print_help()
    return 2


def build_parser():
    p = argparse.ArgumentParser(
        prog="playable",
        description="Playable Cinema CLI"
    )

    # Project
    p.add_argument(
        "--project-root", "--project_root",
        dest="project_root",
        default="/Volumes/abstract-2T/project/",
        help="Root directory for the project"
    )
    
    # Core selection
    p.add_argument("--index", type=int, default=-1, help="Item index from metadata CSV")
    p.add_argument("--type", choices=["shot", "scene"], default="shot", help="Operation type (for --erase)")
    p.add_argument("--media", choices=["movie", "gameplay"], default="movie", help="Media library")
    
    # Annotation controls
    p.add_argument("--shot_index", type=int, default=1, help="Starting shot index (1-based)")
    p.add_argument("--annotation_count", type=int, default=None, help="Number of shots to annotate (default: all)")
    p.add_argument("--filelist", type=str, default=None, help="Text file with one video filename per line")
    
    # Model/runtime
    p.add_argument("--temperature", type=float, default=0.3, help="Model temperature (0.0-1.0)")
    p.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    
    # Detection method parameters
    p.add_argument("--method", choices=["adaptive","content"], default="adaptive", help="Shot detection method")
    p.add_argument("--threshold", type=float, default=3.0, help="Detection threshold (default 3.0 adaptive)")
    p.add_argument("--shot_max_length", type=float, default=-1.0, help="Max seconds per detected shot; -1 disables splitting")
    
    # Frame extraction
    p.add_argument("--frames-per-shot", type=int, default=5, dest="frames_per_shot",
                   help="Frames per shot for extraction")
    p.add_argument("--system", default="system.txt", help="System prompt file for annotation")

    # Commands (mutually exclusive)
    mode = p.add_mutually_exclusive_group(required=False)

    mode.add_argument(
        "--list",
        action="store_const",
        const=cmd_list,
        dest="func",
        help="List all items in --media library (movie or gameplay)"
    )

    mode.add_argument(
        "--detect",
        nargs="?",
        metavar="VIDEO",
        dest="video",
        action=_DispatchAction,
        cmd="detect",
        func=cmd_detect,
        help="Detect shots in VIDEO or use --index from media library"
    )

    mode.add_argument(
        "--extract_frames",
        nargs="?",
        metavar="VIDEO",
        dest="video",
        action=_DispatchAction,
        cmd="extract_frames",
        func=cmd_extract,
        help="Extract sample frames from VIDEO or use --index"
    )

    mode.add_argument(
        "--annotate",
        nargs="?",
        metavar="VIDEO",
        dest="video",
        action=_DispatchAction,
        cmd="annotate",
        func=cmd_annotate,
        help="Annotate detected shots in VIDEO or use --index"
    )

    mode.add_argument(
        "--erase",
        nargs="?",
        metavar="VIDEO",
        dest="video",
        action=_DispatchAction,
        cmd="erase",
        func=cmd_erase,
        help="Erase annotations (use --type shot|scene) in VIDEO or use --index"
    )

    mode.add_argument(
        "--process",
        nargs="?",
        metavar="VIDEO",
        dest="video",
        action=_DispatchAction,
        cmd="process",
        func=cmd_process,
        help="Detect → extract → annotate pipeline for VIDEO, --index, or --filelist"
    )

    p.set_defaults(func=cmd_legacy, cmd=None)

    return p


# -------------------------------------------------
# Helper: resolve video path from --index or VIDEO argument
# -------------------------------------------------
def _resolve_video(ns) -> str:
    """
    If ns.video is set, return it.
    Otherwise use --index + --media to load from MediaLibrary.
    """
    if ns.video:
        return ns.video

    if ns.index < 0:
        raise ValueError("Either VIDEO or --index must be provided")

    try:
        from pipeline.playable_data import Cinematheque, Gameplay
    except ImportError:
        raise RuntimeError("Cannot import playable_data; ensure pipeline module is available")

    if ns.media == "movie":
        csv_path = os.path.join(ns.project_root, "metadata", "cinematheque.csv")
        lib = Cinematheque(csv_path, ns.project_root)
    else:
        csv_path = os.path.join(ns.project_root, "metadata", "gameplay.csv")
        lib = Gameplay(csv_path, ns.project_root)

    item = lib.get(ns.index)
    if not item:
        raise ValueError(f"No item at index {ns.index} in {ns.media} library")

    video_path = lib.get_video_path(item)
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    if ns.verbose:
        print(f"[resolve] index={ns.index} media={ns.media} → {video_path}")

    return video_path


# -------------------------------------------------
# Command implementations
# -------------------------------------------------
def cmd_list(ns):
    """List all items in the media library."""
    try:
        from pipeline.playable_data import Cinematheque, Gameplay
    except ImportError:
        print("Error: Cannot import playable_data; ensure pipeline module is available")
        return 1

    if ns.media == "movie":
        csv_path = os.path.join(ns.project_root, "metadata", "cinematheque.csv")
        lib = Cinematheque(csv_path, ns.project_root)
        print(f"\n=== Cinematheque ({len(lib)} items) ===\n")
    else:
        csv_path = os.path.join(ns.project_root, "metadata", "gameplay.csv")
        lib = Gameplay(csv_path, ns.project_root)
        print(f"\n=== Gameplay ({len(lib)} items) ===\n")

    for i in range(len(lib)):
        item = lib.get(i)
        if not item:
            continue
        
        title = lib.get_title(item)
        filename = item.get('Filename') or item.get('filename', '')
        video_path = lib.get_video_path(item)
        exists = "✓" if os.path.exists(video_path) else "✗"
        
        print(f"{i:3d}  {exists}  {title}")
        if ns.verbose:
            print(f"       {filename}")
    
    print()
    return 0


def cmd_detect(ns):
    video = _resolve_video(ns)
    if ns.verbose:
        print(f"[detect] video={video} method={ns.method} threshold={ns.threshold} max_len={ns.shot_max_length}")
    return 0


def cmd_extract(ns):
    video = _resolve_video(ns)
    if ns.verbose:
        print(f"[extract] video={video} frames={ns.frames_per_shot}")
    return 0


def cmd_annotate(ns):
    video = _resolve_video(ns)
    
    try:
        from pipeline.playable_data import Cinematheque, Gameplay
        from pipeline.playable_annotator import annotate_shots
        from pipeline.ollama_client import OllamaClient
    except ImportError as e:
        raise RuntimeError(f"Cannot import required modules: {e}")

    if ns.media == "movie":
        csv_path = os.path.join(ns.project_root, "metadata", "cinematheque.csv")
        lib = Cinematheque(csv_path, ns.project_root)
    else:
        csv_path = os.path.join(ns.project_root, "metadata", "gameplay.csv")
        lib = Gameplay(csv_path, ns.project_root)

    item = lib.get(ns.index)
    if not item:
        raise ValueError(f"No item at index {ns.index} in {ns.media} library")

    shotlist = lib.load_shotlist(item)
    if not shotlist:
        raise FileNotFoundError(f"No shotlist found for {lib.get_title(item)}")

    # Use hardcoded defaults for model and context
    ollama = OllamaClient(
        model="gemma3:27b",
        num_ctx=8192,
        temperature=ns.temperature
    )

    if not ollama.test_connection():
        raise RuntimeError("Cannot connect to Ollama. Is it running?")

    frames_dir = os.path.join(ns.project_root, "frames")

    print(f"\nAnnotating: {lib.get_title(item)}")
    print(f"Video: {video}")
    print(f"Shots: {len(shotlist)} total")
    print(f"Model: gemma3:27b (ctx=8192, temp={ns.temperature})\n")

    def save_progress(updated_shotlist):
        lib.save_shotlist(item, updated_shotlist)

    annotate_shots(
        shotlist=shotlist,
        video_path=video,
        film=item,
        ollama=ollama,
        frames_dir=frames_dir,
        project_root=ns.project_root,
        limit=ns.annotation_count,
        start_index=ns.shot_index,
        verbose=ns.verbose,
        save_callback=save_progress
    )

    lib.save_shotlist(item, shotlist)
    print(f"\n✓ Annotations saved to shotlist")

    return 0


def cmd_erase(ns):
    """Erase shot or scene captions."""
    try:
        from pipeline.playable_data import Cinematheque, Gameplay
    except ImportError as e:
        raise RuntimeError(f"Cannot import required modules: {e}")

    if ns.media == "movie":
        csv_path = os.path.join(ns.project_root, "metadata", "cinematheque.csv")
        lib = Cinematheque(csv_path, ns.project_root)
    else:
        csv_path = os.path.join(ns.project_root, "metadata", "gameplay.csv")
        lib = Gameplay(csv_path, ns.project_root)

    item = lib.get(ns.index)
    if not item:
        raise ValueError(f"No item at index {ns.index} in {ns.media} library")

    title = lib.get_title(item)
    
    if ns.type == "shot":
        success = lib.erase_shot_captions(item)
        what = "shot captions"
    else:
        success = lib.erase_scene_captions(item)
        what = "scene captions"

    if success:
        print(f"✓ Erased {what} from {title}")
        return 0
    else:
        print(f"✗ Failed to erase {what} from {title}")
        return 1


def cmd_process(ns):
    if ns.filelist:
        paths = [p.strip() for p in Path(ns.filelist).read_text().splitlines() if p.strip()]
    else:
        video = _resolve_video(ns)
        paths = [video]

    if ns.verbose:
        print(f"[process] videos={paths} method={ns.method} threshold={ns.threshold} frames={ns.frames_per_shot}")
    for _v in paths:
        _ = cmd_detect(ns)
        _ = cmd_extract(ns)
        _ = cmd_annotate(ns)
    return 0


# -------------------------------------------------
# Unified main
# -------------------------------------------------
def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = build_parser()

    if not argv:
        parser.print_help()
        return 0

    ns = parser.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())