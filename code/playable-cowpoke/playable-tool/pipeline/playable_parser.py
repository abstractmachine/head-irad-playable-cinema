import argparse
import sys
from pathlib import Path

# -------------------------------------------------
# Legacy (flat) parser retained (still callable directly)
# -------------------------------------------------
def parse_arguments():
    parser = argparse.ArgumentParser(description="BLIP trainer/annotation tool")
    _add_legacy_root_options(parser)
    if len(sys.argv) == 1:
        parser.print_help()
        parser.exit()
    return parser.parse_args()


def _add_legacy_root_options(parser):
    # Project
    parser.add_argument(
        "--project-root", "--project_root",
        dest="project_root",
        default="/Volumes/abstract-2T/project/",
        help="Root directory for the project"
    )
    # Core selection
    parser.add_argument("--index", type=int, default=-1, help="Item index from metadata CSV")
    parser.add_argument("--action", choices=["annotate", "erase"], help="Action to perform")
    parser.add_argument("--type", choices=["shot", "scene"], default="shot", help="Operation type")
    parser.add_argument("--media", choices=["movie", "gameplay"], default="movie", help="Media library")
    # Modes
    parser.add_argument("--detect", action="store_true", help="Run detection (depends on --type)")
    # Annotation controls
    parser.add_argument("--shot_index", type=int, default=1, help="Starting shot index (1-based)")
    parser.add_argument("--annotation_count", type=int, default=None, help="Number of shots to annotate (default: all)")
    parser.add_argument("--filelist", type=str, default=None, help="Text file with one video filename per line")
    # Model/runtime
    parser.add_argument("--model", type=str, default="gemma3:27b", help="Ollama model name")
    parser.add_argument("--num-ctx", type=int, default=8192, help="Ollama context window")
    parser.add_argument("--temperature", type=float, default=0.3, help="Model temperature (0.0-1.0)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    # Detection method parameters
    parser.add_argument("--method", choices=["adaptive","content"], default="adaptive", help="Shot detection method")
    parser.add_argument("--threshold", type=float, default=3.0, help="Detection threshold (default 3.0 adaptive)")
    parser.add_argument("--shot_max_length", type=float, default=-1.0, help="Max seconds per detected shot; -1 disables splitting")


# -------------------------------------------------
# Unified parser (flat root + optional subcommands)
# -------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="playable",
        description="Playable Cinema CLI (legacy root options OR subcommands)"
    )

    # Add all legacy root options so invoking without subcommand stays clean
    _add_legacy_root_options(p)

    # Optional subcommands (not required)
    sub = p.add_subparsers(dest="cmd")  # no required=True -> legacy stays intact

    # Shared parent for subcommands (reuse some root flags)
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--project-root", "--project_root",
                        dest="project_root",
                        default="/Volumes/abstract-2T/project/",
                        help="Root directory")
    parent.add_argument("--verbose", action="store_true")
    parent.add_argument("--model", type=str, default="gemma3:27b")
    parent.add_argument("--num-ctx", type=int, default=8192)
    parent.add_argument("--temperature", type=float, default=0.3)

    # detect-shots
    d = sub.add_parser("detect-shots", help="Detect shots in a video", parents=[parent])
    d.add_argument("video")
    d.add_argument("--method", choices=["adaptive","content"], default="adaptive")
    d.add_argument("--threshold", type=float, default=3.0)
    d.add_argument("--shot_max_length", type=float, default=-1.0)
    d.set_defaults(func=cmd_detect)

    # extract-frames
    e = sub.add_parser("extract-frames", help="Extract sample frames", parents=[parent])
    e.add_argument("video")
    e.add_argument("-n", type=int, default=5, help="Frames per shot")
    e.set_defaults(func=cmd_extract)

    # annotate-shots
    a = sub.add_parser("annotate-shots", help="Annotate detected shots", parents=[parent])
    a.add_argument("video")
    a.add_argument("--system", default="system.txt")
    a.add_argument("--shot_index", type=int, default=1)
    a.add_argument("--annotation_count", type=int, default=None)
    a.set_defaults(func=cmd_annotate)

    # process pipeline
    pr = sub.add_parser("process", help="Detect → extract → annotate pipeline", parents=[parent])
    pr.add_argument("--video")
    pr.add_argument("--filelist")
    pr.add_argument("--frames-per-shot", type=int, default=5)
    pr.add_argument("--method", choices=["adaptive","content"], default="adaptive")
    pr.add_argument("--threshold", type=float, default=3.0)
    pr.set_defaults(func=cmd_process)

    return p


# -------------------------------------------------
# Command implementations (placeholders)
# -------------------------------------------------
def cmd_detect(ns):
    if ns.verbose:
        print(f"[detect] video={ns.video} method={ns.method} threshold={ns.threshold} max_len={ns.shot_max_length}")
    return 0


def cmd_extract(ns):
    if ns.verbose:
        print(f"[extract] video={ns.video} frames={ns.n}")
    return 0


def cmd_annotate(ns):
    if ns.verbose:
        print(f"[annotate] video={ns.video} start={ns.shot_index} count={ns.annotation_count} system={ns.system}")
    return 0


def cmd_process(ns):
    if ns.filelist:
        paths = [p.strip() for p in Path(ns.filelist).read_text().splitlines() if p.strip()]
    else:
        paths = [ns.video] if ns.video else []
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

    # If user called with no args -> help (legacy style)
    if not argv:
        parser.print_help()
        return 0

    ns = parser.parse_args(argv)

    # Subcommand path
    if ns.cmd:
        func = getattr(ns, "func", None)
        return func(ns) if callable(func) else 0

    # Legacy path (no subcommand)
    if ns.verbose:
        print("[legacy] Parsed flat arguments")
    # Placeholder legacy behavior:
    if ns.detect:
        if ns.verbose:
            print(f"[legacy-detect] method={ns.method} threshold={ns.threshold} max_len={ns.shot_max_length}")
    if ns.action == "annotate":
        if ns.verbose:
            print(f"[legacy-annotate] index_start={ns.shot_index} count={ns.annotation_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())