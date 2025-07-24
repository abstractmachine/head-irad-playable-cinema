import re
import time
from PyQt5.QtCore import QObject, pyqtSignal
from scenedetect import open_video, SceneManager
from scenedetect.detectors import ContentDetector

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

class SceneDetectWorker(QObject):
    """Worker class for running scene detection in a separate thread"""
    finished = pyqtSignal(list)
    
    def __init__(self, video_path, method, weights):
        super().__init__()
        self.video_path = video_path
        self.method = method
        self.weights = weights
        
    def run(self):
        print("Scene detection started for:", self.video_path)
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
        print(f"Scene detection finished. Elapsed time: {elapsed_str}")
        self.finished.emit(scene_list)