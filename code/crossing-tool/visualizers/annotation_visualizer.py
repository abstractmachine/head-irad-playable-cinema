#!/usr/bin/env python3
"""Annotation Visualizer — review VLM shot annotations alongside video frames.

Launched as a subprocess by cli.py (`crossing annotate visualizer`).
"""

import sys
import os
import re
import json
import time
import argparse
import faulthandler
from pathlib import Path

# Allow imports from the tool root (data/, services/, generators/)
sys.path.insert(0, str(Path(__file__).parent.parent))

# Fix Qt plugin conflict with OpenCV — import PyQt5 first
from PyQt5.QtCore import Qt, QTimer, QEvent, QThread, pyqtSignal
import threading
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QListWidgetItem, QSplitter,
    QMessageBox, QSizePolicy, QComboBox, QSlider, QStyle, QTextEdit, QFrame,
)
from PyQt5.QtGui import QFont, QPixmap, QImage, QColor, QMouseEvent

from data.metadata import get_metadata
from data.index import (
    load_mapping,
    serialize_annotation_item,
    get_embeddings_path,
    load_embeddings,
)

import cv2
import numpy as np
if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_annotation_json_path(project_path: str, filename: str, media_type: str) -> Path:
    stem = Path(filename).stem
    return Path(project_path) / "data" / "annotations" / "shots" / media_type / f"{stem}.json"


def _read_annotation_json(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _build_annotation_index(entries: list) -> dict:
    """Return {shot_index: annotation_dict} from the aggregated annotation JSON list."""
    idx = {}
    for entry in entries:
        shot = entry.get("shot")
        if not isinstance(shot, dict):
            continue
        shot_id = shot.get("shot_id")
        ann = shot.get("annotation")
        if shot_id is not None and ann is not None:
            idx[int(shot_id) - 1] = ann  # shot_id is 1-based
    return idx


def _build_entry_index(entries: list) -> dict:
    """Return {shot_index: full_entry} for use with serialize_annotation_item."""
    idx = {}
    for entry in entries:
        shot = entry.get("shot")
        if not isinstance(shot, dict):
            continue
        shot_id = shot.get("shot_id")
        if shot_id is not None:
            idx[int(shot_id) - 1] = entry  # shot_id is 1-based
    return idx


def _build_embedding_row_index(entries: list) -> dict:
    """Return {shot_index: npy_row} for aligning shot_index to embedding array rows.

    The .npy row order matches the JSON list order (same as the serialized .txt
    line order), so the row number is simply the list position of each entry.
    """
    idx = {}
    for row, entry in enumerate(entries):
        shot = entry.get("shot")
        if not isinstance(shot, dict):
            continue
        shot_id = shot.get("shot_id")
        if shot_id is not None:
            idx[int(shot_id) - 1] = row  # shot_id is 1-based
    return idx


def _is_valid_annotation(ann: dict) -> bool:
    return isinstance(ann, dict) and "setting" in ann


def _display_name(filename: str) -> str:
    name = Path(filename).stem
    name = re.sub(r"\s*\{tmdb-\d+\}", "", name).strip()
    return name


def _get_sar(video_path: str) -> tuple:
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=sample_aspect_ratio", "-of", "json", video_path],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(result.stdout)
        sar_str = data["streams"][0].get("sample_aspect_ratio", "1:1")
        if sar_str in ("", "0:1", "1:1"):
            return (1, 1)
        parts = sar_str.replace("/", ":").split(":")
        return (int(parts[0]), int(parts[1]))
    except Exception:
        return (1, 1)


# ---------------------------------------------------------------------------
# Click-to-seek slider
# ---------------------------------------------------------------------------

class ClickSeekSlider(QSlider):
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            opt = self.style().subControlRect(
                QStyle.CC_Slider, self._style_option(), QStyle.SC_SliderGroove, self
            )
            groove_width = opt.width()
            if groove_width > 0:
                ratio = (event.x() - opt.x()) / groove_width
                ratio = max(0.0, min(1.0, ratio))
                value = round(self.minimum() + ratio * (self.maximum() - self.minimum()))
                self.setValue(value)
        super().mousePressEvent(event)

    def _style_option(self):
        from PyQt5.QtWidgets import QStyleOptionSlider
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return opt


# ---------------------------------------------------------------------------
# Shot list item
# ---------------------------------------------------------------------------

class ShotAnnotationItem(QListWidgetItem):
    """List item representing a shot with its annotation status."""

    def __init__(self, shot_index: int, shot: dict, annotation):
        super().__init__()
        self.shot_index = shot_index
        self.shot = shot
        self.annotation = annotation
        self.update_display()

    def update_display(self):
        scene = self.shot.get("Scene", "?")
        start = self.shot.get("start_time", "?")
        end = self.shot.get("end_time", "?")
        if self.annotation is None:
            status = "?"
            self.setForeground(QColor("#aaaaaa"))
        elif _is_valid_annotation(self.annotation):
            status = "✓"
            self.setForeground(QColor("#88ff88"))
        else:
            status = "✗"
            self.setForeground(QColor("#ff8888"))
        try:
            scene_str = f"S{int(scene):03d}"
        except (ValueError, TypeError):
            scene_str = f"S{scene}"
        self.setText(f"[{status}] {scene_str} · {self.shot_index:04d} · {start} → {end}")


# ---------------------------------------------------------------------------
# Background annotation worker
# ---------------------------------------------------------------------------

class AnnotateWorker(QThread):
    """Runs annotate_file_shots in a background thread; emits shot_done per shot."""

    shot_done = pyqtSignal(int)   # emits shot_index on each successful annotation
    finished = pyqtSignal(str)    # emits a summary or error message when done

    def __init__(self, project_path, filename, media_type, model_name, frames_per_shot):
        super().__init__()
        self._stop_event = threading.Event()
        self.project_path = project_path
        self.filename = filename
        self.media_type = media_type
        self.model_name = model_name
        self.frames_per_shot = frames_per_shot

    def stop(self):
        self._stop_event.set()

    def run(self):
        from generators.annotate import annotate_file_shots
        try:
            summary = annotate_file_shots(
                project_path=self.project_path,
                filename=self.filename,
                media_type=self.media_type,
                model_name=self.model_name,
                frames_per_shot=self.frames_per_shot,
                skip_existing=True,
                on_shot_done=lambda i: self.shot_done.emit(i),
                stop_event=self._stop_event,
            )
            updated = summary.get("updated", 0)
            skipped = summary.get("skipped", 0)
            self.finished.emit(f"✓ Done  {updated} annotated  {skipped} skipped")
        except Exception as exc:
            self.finished.emit(f"✗ {exc}")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class AnnotationValidator(QMainWindow):
    """Read-only viewer for VLM shot annotations alongside video frames."""

    def __init__(self, project_path: str, filenames: list, current_index: int = 0,
                 media_type: str = "movies"):
        super().__init__()
        self.project_path = project_path
        self.filenames = filenames
        self.current_movie_index = current_index
        self.filename = filenames[current_index]
        self.media_type = media_type
        self.shots: list = []
        self.annotation_index: dict = {}  # shot_index → annotation dict
        self._annotation_entry_index: dict = {}  # shot_index → full entry (for txt render)
        self._embedding_row_index: dict = {}  # shot_index → npy row (for vector render)
        self._embeddings = None  # np.ndarray | None, loaded lazily per film
        self._embeddings_loaded: bool = False
        self._mapping = None
        self._mapping_loaded = False
        self._mapping_error: str = ""
        self.current_shot_index = 0
        self.cap = None
        self.is_playing = False
        self.current_frame_number = 0
        self.playback_timer = None
        self._updating_slider = False
        self._updating_combo = False
        self._play_start_time = 0.0
        self._play_start_frame = 0
        self._current_shot_end_frame = 0
        self._annotate_worker = None
        try:
            import prefs as _prefs
            self._model_name = _prefs.get("model_annotate", "gemma4-e4b")
            self._frames_per_shot = int(_prefs.get("annotate_frames_per_shot", 3))
        except Exception:
            self._model_name = "gemma4-e4b"
            self._frames_per_shot = 3

        self._open_video()
        self._load_data()

        self.playback_timer = QTimer()
        self.playback_timer.timeout.connect(self._advance_frame)
        interval = int(1000 / self.frame_rate) if self.frame_rate > 0 else 42
        self.playback_timer.setInterval(interval)

        self.setWindowTitle(
            f"Annotation Validator — {_display_name(self.filename)}  "
            f"({current_index + 1}/{len(filenames)})"
        )
        self._init_ui()
        self._jump_to_shot(0)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _open_video(self):
        self.video_path = (
            Path(self.project_path) / "media" / "videos"
            / self.media_type / self.filename
        )
        if not self.video_path.exists():
            QMessageBox.critical(self, "Error", f"Video file not found:\n{self.video_path}")
            sys.exit(1)
        self.cap = cv2.VideoCapture(str(self.video_path))
        if not self.cap.isOpened():
            QMessageBox.critical(self, "Error", f"Could not open video:\n{self.video_path}")
            sys.exit(1)
        self.frame_rate = self.cap.get(cv2.CAP_PROP_FPS)
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        raw_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.video_native_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.sar_num, self.sar_den = _get_sar(str(self.video_path))
        self.video_native_width = int(round(raw_w * self.sar_num / self.sar_den))

    def _load_data(self):
        from data.shotlist import read_shotlist
        try:
            self.shots = read_shotlist(self.project_path, self.filename, self.media_type)
        except FileNotFoundError as e:
            QMessageBox.critical(self, "Error", str(e))
            sys.exit(1)
        # Ensure frame numbers are ints
        for s in self.shots:
            for k in ("start_frame", "end_frame"):
                v = s.get(k)
                if isinstance(v, str):
                    try:
                        s[k] = int(v)
                    except ValueError:
                        s[k] = 0

        ann_path = _get_annotation_json_path(self.project_path, self.filename, self.media_type)
        ann_entries = _read_annotation_json(ann_path)
        self.annotation_index = _build_annotation_index(ann_entries)
        self._annotation_entry_index = _build_entry_index(ann_entries)
        self._embedding_row_index = _build_embedding_row_index(ann_entries)
        # Reset the per-film embedding cache whenever we switch films
        self._embeddings = None
        self._embeddings_loaded = False

    def _reload_for_movie(self, index: int):
        self.current_movie_index = index
        self.filename = self.filenames[index]
        if self.cap is not None:
            self.cap.release()
        self._open_video()
        self._load_data()
        interval = int(1000 / self.frame_rate) if self.frame_rate > 0 else 42
        self.playback_timer.setInterval(interval)
        self.setWindowTitle(
            f"Annotation Validator — {_display_name(self.filename)}  "
            f"({index + 1}/{len(self.filenames)})"
        )
        self.timeline_slider.setMaximum(max(0, self.total_frames - 1))
        self._populate_shot_list()
        self._jump_to_shot(0)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _init_ui(self):
        root = QWidget()
        root.setStyleSheet("background-color: #808080; color: white;")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter, stretch=1)

        # ---- COL 1: video ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(2)

        self.frame_label = QLabel()
        self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setScaledContents(False)
        self.frame_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.frame_label.setMinimumSize(1, 1)
        left_layout.addWidget(self.frame_label, stretch=1)

        self.timeline_slider = ClickSeekSlider(Qt.Horizontal)
        self.timeline_slider.setMinimum(0)
        self.timeline_slider.setMaximum(max(0, self.total_frames - 1))
        self.timeline_slider.setValue(0)
        self.timeline_slider.setFocusPolicy(Qt.NoFocus)
        self.timeline_slider.valueChanged.connect(self._on_timeline_seek)
        self.timeline_slider.setToolTip("Scrub timeline  [←/→ frame  Shift+←/→ 1s]")
        left_layout.addWidget(self.timeline_slider)

        # ---- COL 2: annotation panel ----
        mid = QWidget()
        mid_layout = QVBoxLayout(mid)
        mid_layout.setContentsMargins(2, 2, 2, 2)
        mid_layout.setSpacing(4)

        repr_row = QHBoxLayout()
        repr_lbl = QLabel("View:")
        repr_lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        repr_row.addWidget(repr_lbl)
        self.ann_repr_combo = QComboBox()
        self.ann_repr_combo.setFocusPolicy(Qt.NoFocus)
        for _mode in ("fields", "json", "txt", "vector", "mapping"):
            self.ann_repr_combo.addItem(_mode)
        self.ann_repr_combo.setCurrentIndex(0)
        self.ann_repr_combo.currentIndexChanged.connect(self._on_repr_changed)
        repr_row.addWidget(self.ann_repr_combo, stretch=1)
        mid_layout.addLayout(repr_row)

        self.ann_display = QTextEdit()
        self.ann_display.setReadOnly(True)
        self.ann_display.setFont(QFont("Monospace", 9))
        self.ann_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.ann_display.setStyleSheet(
            "QTextEdit { background-color: #4a4a4a; color: white; border: none; }"
        )
        mid_layout.addWidget(self.ann_display, stretch=1)

        # ---- COL 3: shotlist + controls ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(2, 2, 2, 2)
        right_layout.setSpacing(4)

        # Movie selector
        movie_row = QHBoxLayout()
        movie_lbl = QLabel("Movie:")
        movie_lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        movie_row.addWidget(movie_lbl)
        self.movie_combo = QComboBox()
        self.movie_combo.setFocusPolicy(Qt.NoFocus)
        for fn in self.filenames:
            self.movie_combo.addItem(_display_name(fn), fn)
        self.movie_combo.setCurrentIndex(self.current_movie_index)
        self.movie_combo.currentIndexChanged.connect(self._on_movie_changed)
        movie_row.addWidget(self.movie_combo, stretch=1)
        right_layout.addLayout(movie_row)

        self.shot_list = QListWidget()
        self.shot_list.setFont(QFont("Monospace", 8))
        self.shot_list.itemClicked.connect(self._on_shot_clicked)
        self.shot_list.installEventFilter(self)
        self.shot_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        right_layout.addWidget(self.shot_list, stretch=1)

        # Status / frame info
        self.status_label = QLabel()
        self.status_label.setFont(QFont("Monospace", 8))
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            "background-color: #5a5a5a; padding: 4px; border: 1px solid #888;"
        )
        right_layout.addWidget(self.status_label)

        # Play / Continue controls row
        playback_row = QHBoxLayout()
        self.play_pause_btn = QPushButton("▶ Play")
        self.play_pause_btn.setFocusPolicy(Qt.NoFocus)
        self.play_pause_btn.clicked.connect(self._toggle_play_pause)
        self.play_pause_btn.setToolTip("Play / Pause  [Space]")
        playback_row.addWidget(self.play_pause_btn)

        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setFocusPolicy(Qt.NoFocus)
        self.continue_btn.setCheckable(True)
        self.continue_btn.setChecked(False)
        self.continue_btn.setToolTip(
            "Continue: when OFF playback stops at the last frame of the current shot"
        )
        playback_row.addWidget(self.continue_btn)
        right_layout.addLayout(playback_row)

        action_row = QHBoxLayout()
        self.annotate_btn = QPushButton("⚡ Auto-Annotate")
        self.annotate_btn.setFocusPolicy(Qt.NoFocus)
        self.annotate_btn.setCheckable(True)
        self.annotate_btn.setChecked(False)
        self.annotate_btn.setToolTip(
            "Start / stop background LLM annotation of all unannotated shots in this film"
        )
        self.annotate_btn.clicked.connect(self._toggle_auto_annotate)
        action_row.addWidget(self.annotate_btn)

        self.remove_ann_btn = QPushButton("🗑 Remove")
        self.remove_ann_btn.setFocusPolicy(Qt.NoFocus)
        self.remove_ann_btn.setToolTip(
            "Delete all shot annotations for this film (cannot be undone)"
        )
        self.remove_ann_btn.clicked.connect(self._remove_annotations)
        action_row.addWidget(self.remove_ann_btn)
        right_layout.addLayout(action_row)

        # Keyboard hint
        hint = QLabel("↑↓ shot  Space play  ←→ frame  Shift+←→ 1s  Home/End movie")
        hint.setFont(QFont("Monospace", 7))
        hint.setStyleSheet("color: #bbb;")
        right_layout.addWidget(hint)

        splitter.addWidget(left)
        splitter.addWidget(mid)
        splitter.addWidget(right)
        mid.setMinimumWidth(220)
        right.setMinimumWidth(300)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)

        self._populate_shot_list()
        self.setFocus()

    # ------------------------------------------------------------------
    # Shot list population
    # ------------------------------------------------------------------

    def _populate_shot_list(self):
        self.shot_list.clear()
        for i, shot in enumerate(self.shots):
            ann = self.annotation_index.get(i)
            self.shot_list.addItem(ShotAnnotationItem(i, shot, ann))

    def _refresh_shot_item(self, index: int):
        item = self.shot_list.item(index)
        if isinstance(item, ShotAnnotationItem):
            item.update_display()

    # ------------------------------------------------------------------
    # Frame display
    # ------------------------------------------------------------------

    def _get_frame(self, frame_number: int):
        if frame_number < 0 or frame_number >= self.total_frames:
            return None
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
        ret, frame = self.cap.read()
        return frame if ret else None

    def _display_frame(self, frame):
        if frame is None:
            self.frame_label.setText("(frame unavailable)")
            return
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        if (self.sar_num, self.sar_den) != (1, 1):
            display_w = int(round(frame_rgb.shape[1] * self.sar_num / self.sar_den))
            frame_rgb = cv2.resize(frame_rgb, (display_w, frame_rgb.shape[0]),
                                   interpolation=cv2.INTER_LINEAR)
        h, w, ch = frame_rgb.shape
        qimg = QImage(frame_rgb.data, w, h, 3 * w, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        if not pixmap.isNull():
            scaled = pixmap.scaled(self.frame_label.size(),
                                   Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.frame_label.setPixmap(scaled)

    def _update_frame_info(self):
        tc = ""
        if self.frame_rate > 0:
            secs = self.current_frame_number / self.frame_rate
            h = int(secs // 3600)
            m = int((secs % 3600) // 60)
            s = secs % 60
            tc = f"  {h:02d}:{m:02d}:{s:06.3f}"
        self._frame_info_text = f"f{self.current_frame_number}{tc}"
        self._refresh_status_label()

    def _refresh_status_label(self):
        """Rebuild the status label from current shot + frame state."""
        if not hasattr(self, "_status_shot_text"):
            return
        frame_line = getattr(self, "_frame_info_text", "")
        text = self._status_shot_text
        if frame_line:
            text = text + "\n" + frame_line
        self.status_label.setText(text)

    # ------------------------------------------------------------------
    # Timeline slider
    # ------------------------------------------------------------------

    def _update_timeline_slider(self):
        self._updating_slider = True
        self.timeline_slider.setValue(self.current_frame_number)
        self._updating_slider = False

    def _on_timeline_seek(self, value: int):
        if self._updating_slider:
            return
        if self.is_playing:
            self._stop_playback()
        self.current_frame_number = value
        frame = self._get_frame(value)
        if frame is not None:
            self._display_frame(frame)
        self._update_frame_info()

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def _toggle_play_pause(self):
        if self.is_playing:
            self._stop_playback()
        else:
            self._start_playback()

    def _start_playback(self):
        self.is_playing = True
        self.play_pause_btn.setText("⏸ Pause")
        self._play_start_frame = self.current_frame_number
        QTimer.singleShot(0, self._begin_video_timer)

    def _begin_video_timer(self):
        if not self.is_playing:
            return
        self._play_start_time = time.perf_counter()
        self.playback_timer.start()

    def _stop_playback(self):
        self.is_playing = False
        self.play_pause_btn.setText("▶ Play")
        self.playback_timer.stop()

    def _advance_frame(self):
        if not self.is_playing:
            return
        elapsed = time.perf_counter() - self._play_start_time
        target = self._play_start_frame + int(elapsed * self.frame_rate)
        target = min(target, self.total_frames - 1)

        # When Continue is OFF, clamp to the end of the current shot
        if not self.continue_btn.isChecked():
            end = getattr(self, "_current_shot_end_frame", self.total_frames - 1)
            if target >= end:
                self.current_frame_number = end
                self._update_timeline_slider()
                frame = self._get_frame(end)
                if frame is not None:
                    self._display_frame(frame)
                self._update_frame_info()
                self._stop_playback()
                return

        if target <= self.current_frame_number:
            return
        self.current_frame_number = target
        if self.current_frame_number >= self.total_frames:
            self._stop_playback()
            return
        self._update_timeline_slider()
        frame = self._get_frame(self.current_frame_number)
        if frame is not None:
            self._display_frame(frame)
        self._update_frame_info()

    # ------------------------------------------------------------------
    # Shot navigation
    # ------------------------------------------------------------------

    def _jump_to_shot(self, index: int):
        if not self.shots:
            return
        index = max(0, min(index, len(self.shots) - 1))
        if self.is_playing:
            self._stop_playback()

        self.current_shot_index = index
        shot = self.shots[index]
        sf = int(shot.get("start_frame", 0))
        ef = int(shot.get("end_frame", sf))
        self._current_shot_end_frame = ef

        self.current_frame_number = sf
        self._update_timeline_slider()
        frame = self._get_frame(sf)
        if frame is not None:
            self._display_frame(frame)
        self._update_frame_info()

        self.shot_list.setCurrentRow(index)
        self._update_annotation_panel(index, shot)

    def _on_shot_clicked(self, item: ShotAnnotationItem):
        self._jump_to_shot(item.shot_index)

    def _update_annotation_panel(self, shot_index: int, shot: dict):
        n = len(self.shots)
        ann_count = sum(1 for a in self.annotation_index.values() if _is_valid_annotation(a))
        fail_count = sum(1 for a in self.annotation_index.values() if not _is_valid_annotation(a))
        unannotated = n - len(self.annotation_index)

        mode = self.ann_repr_combo.currentText() if hasattr(self, "ann_repr_combo") else "fields"
        if mode == "json":
            text = self._render_annotation_json(shot_index)
        elif mode == "txt":
            text = self._render_annotation_txt(shot_index)
        elif mode == "vector":
            text = self._render_annotation_vector(shot_index)
        elif mode == "mapping":
            text = self._render_annotation_mapping()
        else:
            text = self._render_annotation_fields(shot_index)
        self.ann_display.setPlainText(text)

        self._status_shot_text = (
            f"Shot {shot_index} / {n - 1}  ·  Scene {shot.get('Scene', '?')}\n"
            f"✓ {ann_count}  ✗ {fail_count}  ? {unannotated}"
        )
        self._refresh_status_label()

    def _render_annotation_fields(self, shot_index: int) -> str:
        ann = self.annotation_index.get(shot_index)
        if ann is None:
            return "(not annotated)"
        if _is_valid_annotation(ann):
            lines = []
            for key, val in ann.items():
                label = key.replace("_", " ").capitalize()
                if isinstance(val, list):
                    if val:
                        lines.append(f"{label}:")
                        for v in val:
                            lines.append(f"  • {v}")
                    else:
                        lines.append(f"{label}:\n  —")
                else:
                    lines.append(f"{label}:\n  {val or '—'}")
                lines.append("")
            return "\n".join(lines).strip()
        raw = ann.get("model_output") or ann.get("error") or ""
        full = ann.get("model_output_full") or ""
        if raw:
            return f"⚠ FAILED — model output:\n\n{raw[:2000]}"
        if full:
            return f"⚠ FAILED — full output:\n\n{full[:2000]}"
        return "⚠ FAILED — no output recorded"

    def _render_annotation_json(self, shot_index: int) -> str:
        ann = self.annotation_index.get(shot_index)
        if ann is None:
            return "(not annotated)"
        try:
            return json.dumps(ann, indent=2, ensure_ascii=False)
        except Exception as exc:
            return f"(JSON serialization error: {exc})"

    def _render_annotation_txt(self, shot_index: int) -> str:
        entry = self._annotation_entry_index.get(shot_index)
        if entry is None:
            return "(not annotated)"
        mapping, err = self._get_mapping()
        if mapping is None:
            return f"⚠ Mapping unavailable:\n\n{err}"
        try:
            result = serialize_annotation_item(entry, mapping)
        except Exception as exc:
            return f"⚠ Serialization error:\n\n{exc}"
        if not result:
            ann = self.annotation_index.get(shot_index)
            if not _is_valid_annotation(ann):
                return "⚠ Cannot serialize: annotation is missing or invalid"
            return "(empty — all fields are empty or filtered by mapping)"
        return result

    def _get_mapping(self):
        if not self._mapping_loaded:
            self._mapping_loaded = True
            try:
                self._mapping = load_mapping(self.project_path)
                self._mapping_error = ""
            except Exception as exc:
                self._mapping = None
                self._mapping_error = str(exc)
        return self._mapping, self._mapping_error

    def _load_embeddings_cached(self):
        """Lazy-load the embeddings .npy for the current film; returns None if absent."""
        if not self._embeddings_loaded:
            self._embeddings_loaded = True
            self._embeddings = load_embeddings(
                self.project_path, self.filename, self.media_type
            )
        return self._embeddings

    def _render_annotation_vector(self, shot_index: int) -> str:
        row = self._embedding_row_index.get(shot_index)
        if row is None:
            return "(not annotated — no embedding row)"

        embeddings = self._load_embeddings_cached()
        if embeddings is None:
            emb_path = get_embeddings_path(
                self.project_path, self.filename, self.media_type
            )
            return (
                "(no embeddings file for this film)\n\n"
                f"Expected at:\n  {emb_path}\n\n"
                "Run: crossing index embed <film title>"
            )

        if row >= len(embeddings):
            return (
                f"⚠ Row {row} out of range — "
                f"embeddings shape is {embeddings.shape}"
            )

        vec = embeddings[row]
        dim = int(vec.shape[0])
        dtype = str(vec.dtype)
        norm = float(np.linalg.norm(vec))

        header = (
            f"shape: ({dim},)  dtype: {dtype}  l2-norm: {norm:.6f}\n"
            f"embedding row: {row}  shot index: {shot_index}\n"
        )

        # Format all values in rows of 8
        def _fmt_row(vals) -> str:
            return "  ".join(f"{v:+.5f}" for v in vals)

        COLS = 8
        rows = []
        for off in range(0, dim, COLS):
            rows.append("  " + _fmt_row(vec[off : off + COLS]))
        return header + "\n" + "[\n" + "\n".join(rows) + "\n]"

    def _render_annotation_mapping(self) -> str:
        mapping, err = self._get_mapping()
        if mapping is None:
            return f"⚠ Mapping unavailable:\n\n{err}"
        fields = mapping.get("fields", [])
        include_labels = mapping.get("include_labels", True)
        separator = mapping.get("separator", " | ")
        skip_empty = mapping.get("skip_empty", True)
        lines = [
            "fields:",
        ]
        for f in fields:
            lines.append(f"  - {f}")
        lines += [
            "",
            f"include_labels: {include_labels}",
            f"separator:      {separator!r}",
            f"skip_empty:     {skip_empty}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Movie selector
    # ------------------------------------------------------------------

    def _on_repr_changed(self, _index: int):
        if not self.shots:
            return
        self._update_annotation_panel(
            self.current_shot_index, self.shots[self.current_shot_index]
        )

    def _on_movie_changed(self, index: int):
        if self._updating_combo:
            return
        if self.is_playing:
            self._stop_playback()
        self._reload_for_movie(index)

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def keyPressEvent(self, event):
        key = event.key()
        mod = event.modifiers()
        if key == Qt.Key_Space:
            self._toggle_play_pause()
        elif key == Qt.Key_Up:
            self._jump_to_shot(self.current_shot_index - 1)
        elif key == Qt.Key_Down:
            self._jump_to_shot(self.current_shot_index + 1)
        elif key == Qt.Key_Left:
            if mod & Qt.ShiftModifier:
                self._step_seconds(-1)
            else:
                self._step_frame(-1)
        elif key == Qt.Key_Right:
            if mod & Qt.ShiftModifier:
                self._step_seconds(1)
            else:
                self._step_frame(1)
        elif key == Qt.Key_Home:
            self._switch_movie(-1)
        elif key == Qt.Key_End:
            self._switch_movie(1)
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self.shot_list and event.type() == QEvent.KeyPress:
            self.keyPressEvent(event)
            return True
        return super().eventFilter(obj, event)

    def _step_frame(self, direction: int):
        if self.is_playing:
            self._stop_playback()
        target = max(0, min(self.total_frames - 1, self.current_frame_number + direction))
        self.current_frame_number = target
        self._update_timeline_slider()
        frame = self._get_frame(target)
        if frame is not None:
            self._display_frame(frame)
        self._update_frame_info()

    def _step_seconds(self, direction: int):
        if self.is_playing:
            self._stop_playback()
        frames = max(1, int(round(self.frame_rate)))
        self._step_frame(frames * direction)

    def _switch_movie(self, direction: int):
        new_idx = self.current_movie_index + direction
        if 0 <= new_idx < len(self.filenames):
            self._updating_combo = True
            self.movie_combo.setCurrentIndex(new_idx)
            self._updating_combo = False
            self._reload_for_movie(new_idx)

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Auto-annotate
    # ------------------------------------------------------------------

    def _toggle_auto_annotate(self, checked: bool):
        if checked:
            if self._annotate_worker is not None and self._annotate_worker.isRunning():
                return
            self.annotate_btn.setText("⏹ Stop")
            worker = AnnotateWorker(
                project_path=self.project_path,
                filename=self.filename,
                media_type=self.media_type,
                model_name=self._model_name,
                frames_per_shot=self._frames_per_shot,
            )
            worker.shot_done.connect(self._on_shot_annotated)
            worker.finished.connect(self._on_annotate_finished)
            self._annotate_worker = worker
            worker.start()
        else:
            if self._annotate_worker is not None:
                self._annotate_worker.stop()
            self.annotate_btn.setText("⚡ Auto-Annotate")

    def _on_shot_annotated(self, shot_index: int):
        """Called from AnnotateWorker after each successful shot; refresh UI."""
        ann_path = _get_annotation_json_path(self.project_path, self.filename, self.media_type)
        ann_entries = _read_annotation_json(ann_path)
        self.annotation_index = _build_annotation_index(ann_entries)
        self._annotation_entry_index = _build_entry_index(ann_entries)
        ann = self.annotation_index.get(shot_index)
        item = self.shot_list.item(shot_index)
        if isinstance(item, ShotAnnotationItem):
            item.annotation = ann
            item.update_display()
        if shot_index == self.current_shot_index:
            self._update_annotation_panel(shot_index, self.shots[shot_index])

    def _on_annotate_finished(self, message: str):
        """Called when the worker thread exits; reset button state."""
        self._annotate_worker = None
        self.annotate_btn.setChecked(False)
        self.annotate_btn.setText("⚡ Auto-Annotate")
        # Full reload in case any shots were written while the list was out of sync
        self._load_data()
        self._populate_shot_list()
        # Print errors to console; success is visible from the shot list colours
        if message.startswith("✗"):
            print(f"[Auto-Annotate] {message}", file=sys.stderr)

    def _remove_annotations(self):
        """Delete the annotation JSON for the current film after confirmation."""
        from PyQt5.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self,
            "Remove annotations",
            f"Delete all shot annotations for\n{self.filename}?\n\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        from generators.annotate import remove_file_annotations
        removed = remove_file_annotations(self.project_path, self.filename, self.media_type)
        if removed:
            print(f"[Remove] Deleted annotations: {self.filename}", file=sys.stderr)
        # Reload so the shot list reflects the cleared state
        self._load_data()
        self._populate_shot_list()

    def closeEvent(self, event):
        if self._annotate_worker is not None and self._annotate_worker.isRunning():
            self._annotate_worker.stop()
            self._annotate_worker.wait(2000)
        if self.cap is not None:
            self.cap.release()
        event.accept()


# ---------------------------------------------------------------------------
# Entry point (invoked as a subprocess by cli.py)
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Review VLM shot annotations alongside video frames"
    )
    parser.add_argument("query", nargs="?", help="Filename substring to match")
    parser.add_argument("--tmdb", type=int, help="TMDb ID")
    parser.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    parser.add_argument("--project", help="Project path")
    parser.add_argument("--filenames", nargs="+",
                        help="Explicit list of video filenames (passed by cli.py)")
    parser.add_argument("--all", action="store_true",
                        help="Validate all films with annotation files")
    args = parser.parse_args()

    # Resolve project path
    if args.project:
        project_path = args.project
    else:
        cwd = Path.cwd()
        if (cwd / "media").exists():
            project_path = str(cwd)
        elif (cwd.parent / "media").exists():
            project_path = str(cwd.parent)
        else:
            print("✗ Could not find project path. Use --project or run from the project directory.",
                  file=sys.stderr)
            sys.exit(1)

    # Resolve filenames
    if args.filenames:
        filenames = args.filenames
    elif getattr(args, "all", False):
        entries = get_metadata(project_path, media_type=args.media)
        filenames = [e["filename"] for e in entries if e.get("filename")]
        if not filenames:
            print("✗ No films found in metadata.", file=sys.stderr)
            sys.exit(1)
    elif args.tmdb:
        entries = get_metadata(project_path, media_type=args.media)
        filenames = [e["filename"] for e in entries if e.get("tmdb") == str(args.tmdb)]
        if not filenames:
            print(f"✗ No file found with TMDb ID: {args.tmdb}", file=sys.stderr)
            sys.exit(1)
    elif args.query:
        entries = get_metadata(project_path, query=args.query, media_type=args.media)
        if not entries:
            print(f"✗ No file found matching '{args.query}'", file=sys.stderr)
            sys.exit(1)
        if len(entries) > 1:
            print(f"✗ Multiple files match '{args.query}':", file=sys.stderr)
            for e in entries:
                print(f"  - {e['filename']}", file=sys.stderr)
            print("Use --tmdb <id> or a more specific query.", file=sys.stderr)
            sys.exit(1)
        filenames = [entries[0]["filename"]]
    else:
        print("✗ Must provide a query, --tmdb, --filenames, or --all", file=sys.stderr)
        sys.exit(1)

    # All selected filenames are valid (no annotation JSON required to open)
    valid = filenames

    faulthandler.enable()

    app = QApplication(sys.argv)
    app.setStyleSheet(
        """
        QWidget          { background-color: #808080; color: white; }
        QPushButton      { background-color: #666; color: white; border: 1px solid #999; padding: 3px 8px; border-radius: 3px; }
        QPushButton:hover      { background-color: #777; }
        QPushButton:pressed    { background-color: #555; }
        QPushButton:checked    { background-color: #ff00ff; border-color: #ff66ff; }
        QPushButton:disabled   { color: #aaa; border-color: #777; }
        QComboBox        { background-color: #666; color: white; border: 1px solid #999; padding: 2px 6px; }
        QComboBox QAbstractItemView { background-color: #666; color: white; selection-background-color: #ff00ff; }
        QListWidget      { background-color: #5a5a5a; color: white; border: 1px solid #888; }
        QListWidget::item:selected { background-color: #ff00ff; color: white; }
        QListWidget::item:hover    { background-color: #6a6a6a; }
        QSlider::groove:horizontal { background: #555; height: 6px; border-radius: 3px; }
        QSlider::handle:horizontal { background: #ccc; width: 14px; height: 14px; margin: -4px 0; border-radius: 7px; }
        QSlider::sub-page:horizontal { background: #ff00ff; border-radius: 3px; }
        QScrollBar:vertical        { background: #666; width: 10px; }
        QScrollBar::handle:vertical { background: #999; border-radius: 4px; min-height: 20px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QLabel           { background-color: transparent; color: white; }
        QSplitter::handle { background-color: #666; }
        QMessageBox      { background-color: #808080; color: white; }
        QTextEdit        { background-color: #5a5a5a; color: white; border: 1px solid #888; }
        """
    )

    validator = AnnotationValidator(project_path, valid, 0, args.media)
    screen = QApplication.primaryScreen()
    avail = screen.availableGeometry()
    validator.setGeometry(avail)
    validator.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
