# PySceneDetect-based shot & scene detection.
# Requires: pip install scenedetect

from typing import List, Dict, Optional
import os
import math

try:
    from scenedetect import VideoManager, SceneManager, StatsManager
    from scenedetect.detectors import AdaptiveDetector, ContentDetector
    from scenedetect.frame_timecode import FrameTimecode
except ImportError:
    VideoManager = None

def _ensure(video_path: str):
    if not os.path.exists(video_path):
        raise FileNotFoundError(video_path)
    if VideoManager is None:
        raise RuntimeError("scenedetect not installed (pip install scenedetect)")

def _format_tc(ftc: FrameTimecode) -> str:
    return ftc.get_timecode()  # HH:MM:SS.mmm

def _parse_window_time(t: Optional[str], base: FrameTimecode) -> Optional[FrameTimecode]:
    if not t:
        return None
    if ":" in t:
        return FrameTimecode(timecode=t, fps=base.framerate)
    return FrameTimecode(timecode=float(t), fps=base.framerate)

def _split_shot(
    s_tc: FrameTimecode,
    e_tc: FrameTimecode,
    max_len: float,
    fps: float
) -> List[tuple[FrameTimecode, FrameTimecode]]:
    """Evenly split [s,e] into N parts so that each duration <= max_len."""
    dur = max(0.0, e_tc.get_seconds() - s_tc.get_seconds())
    if max_len <= 0 or dur <= max_len:
        return [(s_tc, e_tc)]
    n = max(2, int(math.ceil(dur / max_len)))
    part = dur / n
    chunks: List[tuple[FrameTimecode, FrameTimecode]] = []
    s0 = s_tc.get_seconds()
    for k in range(n):
        cs = FrameTimecode(timecode=s0 + k * part, fps=fps)
        ce = FrameTimecode(timecode=min(s0 + (k + 1) * part, e_tc.get_seconds()), fps=fps)
        if ce.get_seconds() - cs.get_seconds() > 1e-6:
            chunks.append((cs, ce))
    return chunks

def detect_shots(
    video_path: str,
    method: str = "adaptive",
    threshold: Optional[float] = 3.0,          # DEFAULT CHANGED: explicit 3.0
    min_scene_len_frames: int = 12,
    downscale_factor: int = 2,
    luma_only: bool = False,
    start: Optional[str] = None,
    end: Optional[str] = None,
    shot_max_length: float = -1.0,
    verbose: bool = False
) -> List[Dict]:
    """
    AdaptiveDetector (PySceneDetect 0.6.x) does NOT support downscale_factor or luma_only.
    ContentDetector supports: threshold, min_scene_len, luma_only, downscale_factor, kernel_size.
    Also supports optional splitting of long shots by --shot_max_length.
    """
    _ensure(video_path)
    vm = VideoManager([video_path])
    vm.start()
    try:
        base_tc = vm.get_base_timecode()
        fps = float(base_tc.framerate)
        start_tc = _parse_window_time(start, base_tc) if start else None
        end_tc = _parse_window_time(end, base_tc) if end else None
        if start_tc or end_tc:
            vm.set_duration(start_time=start_tc, end_time=end_tc)

        stats = StatsManager()
        sm = SceneManager(stats)

        if method == "adaptive":
            det = AdaptiveDetector(
                adaptive_threshold=threshold,
                min_scene_len=min_scene_len_frames
                # window_width / weights keep defaults
            )
        elif method == "content":
            det = ContentDetector(
                threshold=threshold,
                min_scene_len=min_scene_len_frames,
                luma_only=luma_only,
                downscale_factor=downscale_factor,
                kernel_size=3
            )
        else:
            raise ValueError(f"Unknown method: {method}")

        sm.add_detector(det)
        sm.detect_scenes(vm)
        scenes = sm.get_scene_list()
    finally:
        vm.release()

    # Fallback to full window if no cuts
    if not scenes:
        if verbose:
            print("No cuts detected; creating single shot.")
        vm2 = VideoManager([video_path])
        vm2.start()
        full_end = vm2.get_base_timecode() + vm2.get_duration()
        vm2.release()
        s_tc = start_tc or base_tc
        e_tc = end_tc or full_end
        scenes = [(s_tc, e_tc)]

    # Apply optional splitting
    final_scenes: List[tuple[FrameTimecode, FrameTimecode]] = []
    for i, (s_tc, e_tc) in enumerate(scenes):
        dur = max(0.0, e_tc.get_seconds() - s_tc.get_seconds())
        chunks = _split_shot(s_tc, e_tc, shot_max_length, fps)
        if len(chunks) > 1:
            approx = dur / len(chunks)
            print(f"  [SPLIT] Shot {i} {dur:.2f}s > max {shot_max_length:.2f}s → {len(chunks)} parts (~{approx:.2f}s each)")
            if verbose:
                for j, (cs, ce) in enumerate(chunks, 1):
                    print(f"          └─ part {j}: {cs.get_timecode()} → {ce.get_timecode()} ({ce.get_seconds()-cs.get_seconds():.2f}s)")
        final_scenes.extend(chunks)

    # Build shot rows
    shots: List[Dict] = []
    for i, (s_tc, e_tc) in enumerate(final_scenes):
        shots.append({
            "Ignore": "",
            "Scene": "",
            "Start": _format_tc(s_tc),
            "End": _format_tc(e_tc),
            "Shot_Caption": "",
            "Scene_Caption": ""
        })
        if verbose:
            dur = e_tc.get_seconds() - s_tc.get_seconds()
            print(f"  [SHOT {i}] {s_tc.get_seconds():.3f}s → {e_tc.get_seconds():.3f}s ({dur:.3f}s)")

    if verbose:
        print(f"Detected {len(shots)} shots.")
    return shots

def detect_scenes(
    shotlist: List[Dict],
    gap_sec: float = 5.0,
    min_scene_shots: int = 5,
    verbose: bool = False
) -> List[Dict]:
    """
    Simple gap-based scene grouping atop existing shotlist.
    """
    def _parse_tc(tc: str) -> float:
        if not tc:
            return 0.0
        hh, mm, rest = tc.split(":")
        ss = float(rest)
        return int(hh)*3600 + int(mm)*60 + ss

    if not shotlist:
        return shotlist

    current = 1
    count_in = 0
    last_end = _parse_tc(shotlist[0].get("End","0"))
    for shot in shotlist:
        start = _parse_tc(shot.get("Start","0"))
        end = _parse_tc(shot.get("End","0"))
        gap = start - last_end
        if gap >= gap_sec and count_in >= min_scene_shots:
            current += 1
            count_in = 0
            if verbose:
                print(f"  [SCENE CUT] gap={gap:.2f}s → Scene {current}")
        shot["Scene"] = str(current)
        count_in += 1
        last_end = max(last_end, end)

    if verbose:
        print(f"Assigned {current} scenes.")
    return shotlist
