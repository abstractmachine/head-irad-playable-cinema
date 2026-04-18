#!/usr/bin/env python3
"""Annotation Visualizer — merged into ShotlistVisualizer (shot_visualizer.py).

This module is kept as a thin shim for backward compatibility.
The full annotator + shot-list UI now lives in shot_visualizer.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from visualizers.shot_visualizer import main  # noqa: E402



if __name__ == "__main__":
    main()
