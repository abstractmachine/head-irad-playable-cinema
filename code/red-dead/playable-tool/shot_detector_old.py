DEBUG = False  # Set to True to enable debug output

import os
import csv
import re
import time

# Qt Stuff
from PyQt5.QtCore import QObject, QThread, pyqtSignal, QTimer
from scenedetect import open_video
from PyQt5.QtCore import QObject, pyqtSignal

# PySceneDetect
from scenedetect import open_video, SceneManager
from scenedetect.detectors import ContentDetector

# Our stuff
from utility import timecode_to_milliseconds, pct_to_milliseconds

def parse_detector_args(arg_string):
    """Parse command line arguments for detector options"""
    option_map = {
        '-t': 'threshold',
        '--adaptive-threshold': 'adaptive_threshold',
        '--threshold': 'threshold',
        '-c': 'min_content_val',
        '--min-content-val': 'min_content_val',
        '-w': 'weights',
        '--weights': 'weights',
        '-l': 'luma_only',
        '--luma-only': 'luma_only',
        '-k': 'kernel_size',
        '--kernel-size': 'kernel_size',
        '-m': 'min_scene_len',
        '--min-scene-len': 'min_scene_len',
        '-f': 'frame_window',
        '--frame-window': 'frame_window',
        '-s': 'size',
        '--size': 'size',
        '--fade-bias': 'fade_bias',
        '--add-last-scene': 'add_last_scene',
        '--bins': 'bins',
        '-b': 'bins',
        '--lowpass': 'lowpass',
    }
    pattern = r'(-{1,2}\w+)(?:\s+([^\s-][^-\s]*))?'
    matches = re.findall(pattern, arg_string)
    kwargs = {}
    for opt, val in matches:
        key = option_map.get(opt)
        if key:
            if key == 'weights':
                weights_match = re.findall(r'-w ([\d\.]+) ([\d\.]+) ([\d\.]+) ([\d\.]+)', arg_string)
                if weights_match:
                    kwargs['weights'] = tuple(map(float, weights_match[0]))
            elif key == 'luma_only':
                kwargs['luma_only'] = True
            elif key == 'add_last_scene':
                kwargs['add_last_scene'] = True
            elif val is not None and val != '':
                try:
                    if '.' in val:
                        kwargs[key] = float(val)
                    else:
                        kwargs[key] = int(val)
                except ValueError:
                    kwargs[key] = val
    return kwargs

def create_detector(method, detector_args):
    """Create a detector based on method and arguments"""
    unused_args = dict(detector_args)
    
    if method == "detect-adaptive":
        from scenedetect.detectors import AdaptiveDetector
        detector = AdaptiveDetector()
        if "threshold" in detector_args:
            detector.adaptive_threshold = detector_args["threshold"]
            unused_args.pop("threshold", None)
        for key in ["frame_window", "min_content_val", "weights", "luma_only", "kernel_size", "min_scene_len"]:
            if key in detector_args:
                setattr(detector, key, detector_args[key])
                unused_args.pop(key, None)
                
    elif method == "detect-content":
        from scenedetect.detectors import ContentDetector
        ctor_keys = ["threshold", "weights", "luma_only", "kernel_size", "min_scene_len", "frame_window"]
        ctor_args = {k: detector_args[k] for k in ctor_keys if k in detector_args}
        detector = ContentDetector(**ctor_args)
        for k in ctor_args:
            unused_args.pop(k, None)
            
    elif method == "detect-hist":
        from scenedetect.detectors import HistogramDetector
        ctor_keys = ["threshold", "bins", "min_scene_len"]
        ctor_args = {k: detector_args[k] for k in ctor_keys if k in detector_args}
        detector = HistogramDetector(**ctor_args)
        for k in ctor_args:
            unused_args.pop(k, None)
            
    elif method == "detect-threshold":
        from scenedetect.detectors import ThresholdDetector
        ctor_keys = ["threshold", "fade_bias", "add_last_scene", "min_scene_len"]
        ctor_args = {k: detector_args[k] for k in ctor_keys if k in detector_args}
        detector = ThresholdDetector(**ctor_args)
        for k in ctor_args:
            unused_args.pop(k, None)
    else:
        # Default to ContentDetector
        detector = ContentDetector()
    
    if unused_args:
        print(f"Warning: The following detector options were not used: {', '.join(unused_args.keys())}")
    
    return detector

# -------------- WORKERS ----------------

class ShotDetectWorker(QObject):
    """Worker class for running scene detection in a separate thread"""
    finished = pyqtSignal(list)
    
    def __init__(self, video_path, method, weights):
        super().__init__()
        self.video_path = video_path
        self.method = method
        self.weights = weights
        
    def run(self):
        print("Shot detection started for:", self.video_path)
        scene_list = []
        start_time = time.time()
        try:
            video = open_video(self.video_path)
            scene_manager = SceneManager()
            detector_args = parse_detector_args(self.weights)
            detector = create_detector(self.method, detector_args)
            scene_manager.add_detector(detector)
            scene_manager.detect_scenes(video)
            scene_list = scene_manager.get_scene_list()
        except Exception as e:
            scene_list = [f"Error: {e}"]
        elapsed = time.time() - start_time
        elapsed_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
        print(f"Shot detection finished. Elapsed time: {elapsed_str}")
        self.finished.emit(scene_list)

# -------------- MANAGER ---------------

class ShotManager(QObject):
    shotlist_generated = pyqtSignal(str)  # path to generated CSV
    shotlist_deleted = pyqtSignal()
    shotlist_loaded = pyqtSignal(list)
    shotlist_load_error = pyqtSignal(str)

    def __init__(self, detections_folder):
        super().__init__()
        self.detections_folder = detections_folder
        self.current_csv_path = None
        self.shot_worker = None
        self.thread = None

    def generate_shotlist(self, video_path, method, weights_text):
        base = os.path.basename(video_path)
        name, _ = os.path.splitext(base)
        txt_path = os.path.join(self.detections_folder, f"{name}.txt")
        with open(txt_path, "w", encoding="utf-8") as txtfile:
            txtfile.write(f"{method}\n{weights_text}\n")

        self.shot_worker = ShotDetectWorker(video_path, method, weights_text)
        self.thread = QThread()
        self.shot_worker.moveToThread(self.thread)
        self.thread.started.connect(self.shot_worker.run)
        self.shot_worker.finished.connect(self.on_scene_detected)
        self.shot_worker.finished.connect(self.thread.quit)
        self.shot_worker.finished.connect(self.shot_worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def on_scene_detected(self, scene_list):
        if not scene_list or (isinstance(scene_list[0], str) and scene_list[0].startswith("Error:")):
            self.shotlist_generated.emit("")
            return
        # Default FPS
        fps = 25
        try:
            video = open_video(self.shot_worker.video_path)
            if hasattr(video, "frame_rate"):
                fps = video.frame_rate
        except Exception:
            pass
        frame_duration = int(1000 / fps)
        start_padding = 5 * frame_duration
        end_padding = 5 * frame_duration
        csv_rows = []
        for scene in scene_list:
            start_tc = scene[0].get_timecode()
            end_tc = scene[1].get_timecode()
            start_ms = timecode_to_milliseconds(start_tc)
            end_ms = timecode_to_milliseconds(end_tc)
            padded_start_ms = start_ms + start_padding
            padded_end_ms = max(end_ms - end_padding, 0)
            padded_start_tc = milliseconds_to_timecode(padded_start_ms)
            padded_end_tc = milliseconds_to_timecode(padded_end_ms)
            csv_rows.append(["No", 0, padded_start_tc, padded_end_tc, "", ""])
        base = os.path.basename(self.shot_worker.video_path)
        name, _ = os.path.splitext(base)
        out_path = os.path.join(self.detections_folder, f"{name}.csv")
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Ignore", "Scene", "Start", "End", "Shot_Caption", "Scene_Caption"])
            writer.writerows(csv_rows)
        self.current_csv_path = out_path
        self.shotlist_generated.emit(out_path)

    def delete_scene_csv(self):
        # Delete CSV file
        if self.current_csv_path and os.path.exists(self.current_csv_path):
            os.remove(self.current_csv_path)
            # Also delete the corresponding .txt file
            base = os.path.basename(self.current_csv_path)
            name, _ = os.path.splitext(base)
            txt_path = os.path.join(self.detections_folder, f"{name}.txt")
            if os.path.exists(txt_path):
                os.remove(txt_path)
        self.scene_table.setRowCount(0)
        self.current_csv_path = None
        self.shotlist_status.emit(False)

    def delete_shotlist(self):
        if self.current_csv_path and os.path.exists(self.current_csv_path):
            os.remove(self.current_csv_path)
            base = os.path.basename(self.current_csv_path)
            name, _ = os.path.splitext(base)
            txt_path = os.path.join(self.detections_folder, f"{name}.txt")
            if os.path.exists(txt_path):
                os.remove(txt_path)
        self.current_csv_path = None
        self.shotlist_deleted.emit()

    def save_shotlist(self, rows):
        if not self.current_csv_path:
            return
        with open(self.current_csv_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["Ignore", "Scene", "Start", "End", "Shot_Caption", "Scene_Caption"])
            for row in rows:
                writer.writerow(row)

    def load_shotlist(self, path):
        try:
            with open(path, "r", encoding="utf-8") as csvfile:
                reader = csv.DictReader(csvfile)
                rows = [row for row in reader]
            self.shotlist_loaded.emit(rows)
        except Exception as e:
            self.shotlist_load_error.emit(str(e))

    def update_shot_caption(self, row_index, shot_caption_text):
        # Load, update, and save the shotlist
        if not self.current_csv_path:
            return
        with open(self.current_csv_path, "r", encoding="utf-8") as csvfile:
            reader = list(csv.reader(csvfile))
        header = reader[0]
        rows = reader[1:]
        shot_caption_idx = header.index("Shot_Caption")
        if 0 <= row_index < len(rows):
            rows[row_index][shot_caption_idx] = shot_caption_text
        self.save_shotlist(rows)
  