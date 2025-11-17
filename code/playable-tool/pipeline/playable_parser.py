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
    
    try:
        from pipeline.playable_data import Cinematheque, Gameplay
        from pipeline.playable_detector import detect_shots
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
    
    print(f"\nDetecting shots: {title}")
    print(f"Video: {video}")
    print(f"Method: {ns.method} (threshold={ns.threshold}, max_length={ns.shot_max_length}s)\n")
    
    # Detect shots
    shotlist = detect_shots(
        video_path=video,
        method=ns.method,
        threshold=ns.threshold,
        shot_max_length=ns.shot_max_length,
        verbose=ns.verbose
    )
    
    if not shotlist:
        print("✗ No shots detected")
        return 1
    
    # Save shotlist
    lib.save_shotlist(item, shotlist)
    print(f"\n✓ Detected {len(shotlist)} shots, saved to shotlist")
    
    return 0


def cmd_extract(ns):
    """Extract sample frames from detected shots."""
    video = _resolve_video(ns)
    
    try:
        from pipeline.playable_data import Cinematheque, Gameplay
        import cv2
        import numpy as np
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
        raise FileNotFoundError(f"No shotlist found for {lib.get_title(item)}. Run --detect first.")

    title = lib.get_title(item)
    frames_dir = os.path.join(ns.project_root, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    
    movie_base = os.path.splitext(item.get('Filename') or item.get('filename', ''))[0]
    
    print(f"\nExtracting frames: {title}")
    print(f"Shots: {len(shotlist)}")
    print(f"Frames per shot: {ns.frames_per_shot}\n")
    
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video}")
    
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = 0
    
    def _tc_to_frame(tc_str: str) -> int:
        """Convert HH:MM:SS:FF to frame number."""
        parts = tc_str.split(':')
        if len(parts) != 4:
            return 0
        h, m, s, f = map(int, parts)
        return int((h * 3600 + m * 60 + s) * fps + f)
    
    for i, shot in enumerate(shotlist):
        start_tc = shot.get('Start') or shot.get('TC In') or ''
        end_tc = shot.get('End') or shot.get('TC Out') or ''
        
        if not start_tc or not end_tc:
            if ns.verbose:
                print(f"  Shot {i+1}: skipped (missing timecodes)")
            continue
        
        start_frame = _tc_to_frame(start_tc)
        end_frame = _tc_to_frame(end_tc)
        duration = end_frame - start_frame
        
        if duration <= 0:
            if ns.verbose:
                print(f"  Shot {i+1}: skipped (invalid duration)")
            continue
        
        # Extract evenly spaced frames
        frame_indices = np.linspace(start_frame, end_frame, ns.frames_per_shot, dtype=int)
        shot_frames = []
        
        for idx, frame_num in enumerate(frame_indices):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
            ret, frame = cap.read()
            
            if not ret:
                continue
            
            # Save frame
            frame_filename = f"{movie_base}_shot{i:04d}_frame{idx:02d}.jpg"
            frame_path = os.path.join(frames_dir, frame_filename)
            cv2.imwrite(frame_path, frame)
            shot_frames.append(frame_path)
        
        total_frames += len(shot_frames)
        if ns.verbose:
            print(f"  Shot {i+1}: extracted {len(shot_frames)} frames")
    
    cap.release()
    print(f"\n✓ Extracted {total_frames} frames total to {frames_dir}")
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
    """Run detect → extract → annotate pipeline for one or more videos."""
    try:
        from pipeline.playable_data import Cinematheque, Gameplay
    except ImportError as e:
        raise RuntimeError(f"Cannot import required modules: {e}")
    
    # Determine which videos to process
    if ns.filelist:
        filelist_path = Path(ns.filelist)
        if not filelist_path.exists():
            raise FileNotFoundError(f"Filelist not found: {ns.filelist}")
        
        video_names = [p.strip() for p in filelist_path.read_text().splitlines() if p.strip()]
        
        if ns.media == "movie":
            csv_path = os.path.join(ns.project_root, "metadata", "cinematheque.csv")
            lib = Cinematheque(csv_path, ns.project_root)
        else:
            csv_path = os.path.join(ns.project_root, "metadata", "gameplay.csv")
            lib = Gameplay(csv_path, ns.project_root)
        
        indices = []
        for name in video_names:
            idx = lib.find_by_filename(name)
            if idx is not None:
                indices.append(idx)
            elif ns.verbose:
                print(f"Warning: '{name}' not found in {ns.media} library, skipping")
        
        if not indices:
            print("Error: No valid videos found in filelist")
            return 1
    elif ns.video:
        indices = [ns.index] if ns.index >= 0 else None
        if indices is None:
            print("Error: --process with VIDEO path requires --index to save results")
            return 1
    elif ns.index >= 0:
        indices = [ns.index]
    else:
        raise ValueError("Either VIDEO, --index, or --filelist must be provided for --process")
    
    total = len(indices)
    success_count = 0
    failed = []
    
    print(f"\n{'='*60}")
    print(f"Processing {total} video(s) in {ns.media} library")
    print(f"Pipeline: detect (method={ns.method}, threshold={ns.threshold})")
    print(f"          → extract (frames={ns.frames_per_shot})")
    print(f"          → annotate (temp={ns.temperature})")
    print(f"{'='*60}\n")
    
    for count, idx in enumerate(indices, start=1):
        ns_copy = argparse.Namespace(**vars(ns))
        ns_copy.index = idx
        ns_copy.video = None
        
        try:
            video = _resolve_video(ns_copy)
            
            if ns.media == "movie":
                csv_path = os.path.join(ns.project_root, "metadata", "cinematheque.csv")
                lib = Cinematheque(csv_path, ns.project_root)
            else:
                csv_path = os.path.join(ns.project_root, "metadata", "gameplay.csv")
                lib = Gameplay(csv_path, ns.project_root)
            
            item = lib.get(idx)
            title = lib.get_title(item) if item else f"index {idx}"
            movie_filename = item.get('Filename') or item.get('filename', '')
            movie_base = os.path.splitext(movie_filename)[0]
            
            print(f"\n[{count}/{total}] {title}")
            print(f"{'─'*60}")
            
            # Step 1: Detect (skip if shotlist exists)
            shotlist = lib.load_shotlist(item)
            if shotlist:
                print(f"→ Shotlist exists ({len(shotlist)} shots), skipping detection")
            else:
                if ns.verbose:
                    print("→ Detecting shots...")
                result = cmd_detect(ns_copy)
                if result != 0:
                    raise RuntimeError("Detection failed")
                shotlist = lib.load_shotlist(item)
            
            if not shotlist:
                raise RuntimeError("No shotlist available after detection")
            
            # Step 2: Extract (skip if frames already exist)
            frames_dir = os.path.join(ns.project_root, "frames")
            # Check if frames exist for first shot
            first_frame_pattern = f"{movie_base}_shot0000_frame00.jpg"
            first_frame_path = os.path.join(frames_dir, first_frame_pattern)
            
            if os.path.exists(first_frame_path):
                print(f"→ Frames already exist, skipping extraction")
            else:
                if ns.verbose:
                    print("→ Extracting frames...")
                result = cmd_extract(ns_copy)
                if result != 0:
                    raise RuntimeError("Frame extraction failed")
            
            # Step 3: Annotate (always run - overwrites existing captions)
            if ns.verbose:
                print("→ Annotating shots...")
            result = cmd_annotate(ns_copy)
            if result != 0:
                raise RuntimeError("Annotation failed")
            
            success_count += 1
            print(f"✓ Completed successfully")
            
        except Exception as e:
            failed.append((idx, title, str(e)))
            print(f"✗ Failed: {e}")
            if ns.verbose:
                import traceback
                traceback.print_exc()
    
    # Summary
    print(f"\n{'='*60}")
    print(f"SUMMARY: {success_count}/{total} succeeded")
    if failed:
        print(f"\nFailed videos:")
        for idx, title, error in failed:
            print(f"  [{idx}] {title}: {error}")
    print(f"{'='*60}\n")
    
    return 0 if success_count == total else 1