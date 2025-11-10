import os
import cv2
from typing import List, Dict, Optional
from ollama import OllamaClient

def has_scenes(shotlist: List[Dict]) -> bool:
    """
    Check if the shotlist has valid scene information.
    Returns True if at least one shot has a non-empty Scene field.
    """
    for shot in shotlist:
        if 'Scene' in shot and shot['Scene'].strip():
            return True
    return False

def get_unique_scenes(shotlist: List[Dict]) -> List[str]:
    """
    Get a list of unique scene IDs from the shotlist.
    """
    scenes = set()
    for shot in shotlist:
        if 'Scene' in shot and shot['Scene'].strip():
            scenes.add(shot['Scene'].strip())
    return sorted(list(scenes), key=lambda x: int(x) if x.isdigit() else 0)

def parse_timecode(tc: str) -> float:
    """
    Parse timecode strings like HH:MM:SS.mmm
    Returns seconds as float.
    """
    parts = tc.split(':')
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return 0.0

def extract_frame_at_time(video_path: str, timestamp: float, output_path: str) -> bool:
    """
    Extract a frame from video at the given timestamp.
    Returns True if successful.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return False
    
    # Seek to timestamp (in milliseconds)
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
    ret, frame = cap.read()
    
    if ret and frame is not None:
        cv2.imwrite(output_path, frame)
        cap.release()
        return True
    
    cap.release()
    return False

def extract_frames_for_shot(video_path: str, start_tc: str, end_tc: str, output_dir: str, movie_base_name: str, shot_index: int) -> List[str]:
    """
    Extract 5 evenly spaced frames from a shot.
    Divides by 7 and skips first and last segments.
    Only extracts frames that don't already exist.
    Returns list of image paths.
    """
    os.makedirs(output_dir, exist_ok=True)
    
    start_time = parse_timecode(start_tc)
    end_time = parse_timecode(end_tc)
    duration = end_time - start_time
    
    if duration <= 0:
        return []
    
    # Divide by 7, extract frames at 2/7, 3/7, 4/7, 5/7, 6/7
    segment = duration / 7.0
    timestamps = [start_time + segment * i for i in range(2, 7)]
    
    image_paths = []
    for i, timestamp in enumerate(timestamps):
        image_path = os.path.join(output_dir, f"{movie_base_name}_shot_{shot_index:04d}_frame_{i:02d}.png")
        
        # Check if frame already exists
        if os.path.exists(image_path):
            print(f"  Frame already exists: {os.path.basename(image_path)}")
            image_paths.append(image_path)
        else:
            if extract_frame_at_time(video_path, timestamp, image_path):
                print(f"  Extracted frame: {os.path.basename(image_path)}")
                image_paths.append(image_path)
    
    return image_paths

def load_system_prompt(project_root: str, film: Dict, image_count: int = 5) -> str:
    """
    Load and format the system prompt from prompts/system.txt
    """
    prompt_path = os.path.join(project_root, "prompts", "system.txt")
    
    if not os.path.exists(prompt_path):
        print(f"Warning: System prompt not found at {prompt_path}")
        return ""
    
    with open(prompt_path, 'r', encoding='utf-8') as f:
        prompt = f.read()
    
    # Replace placeholders
    prompt = prompt.replace("{title}", film.get('title', 'Unknown'))
    prompt = prompt.replace("{year}", film.get('year', 'Unknown'))
    prompt = prompt.replace("{director}", film.get('director', 'Unknown'))
    prompt = prompt.replace("{image-count}", str(image_count))
    
    return prompt

def annotate_shot(shot: Dict, index: int, video_path: str, film: Dict, ollama: OllamaClient, frames_dir: str, project_root: str, print_prompt: bool = False) -> str:
    """
    Generate a caption for a single shot using Ollama.
    Extracts 5 frames and sends them for annotation.
    """
    # Check if we should ignore this shot
    if shot.get('Ignore', '').strip().lower() == 'yes':
        return ""
    
    start_tc = shot.get('Start', '')
    end_tc = shot.get('End', '')
    
    if not start_tc or not end_tc:
        return ""
    
    # Get movie base name (filename without extension)
    movie_filename = film.get('filename', '')
    movie_base_name = os.path.splitext(movie_filename)[0]
    
    # Extract frames
    image_paths = extract_frames_for_shot(video_path, start_tc, end_tc, frames_dir, movie_base_name, index)
    
    if not image_paths:
        return ""
    
    # Load system prompt
    system_prompt = load_system_prompt(project_root, film, len(image_paths))
    
    # Print system prompt for first shot only
    if print_prompt:
        print("\n" + "-" * 80)
        print("SYSTEM PROMPT:")
        print("-" * 80)
        print(system_prompt)
        print("-" * 80 + "\n")
    
    # Send to Ollama with images
    response = ollama.generate_with_images(system_prompt, image_paths)
    
    return response or ""

def annotate_scene(scene_id: str, shots: List[Dict]) -> str:
    """
    Generate a caption for an entire scene.
    """
    return ""

def annotate_shots(shotlist: List[Dict], video_path: str, film: Dict, ollama: OllamaClient, frames_dir: str, project_root: str, limit: int = None) -> List[Dict]:
    """
    Annotate each shot individually with Shot_Caption.
    Returns the modified shotlist.
    
    Args:
        limit: If set, only process this many shots (for testing)
    """
    processed = 0
    for i, shot in enumerate(shotlist):
        # Skip ignored shots
        if shot.get('Ignore', '').strip().lower() == 'yes':
            shot['Shot_Caption'] = ""
            continue
        
        # Apply limit for testing
        if limit and processed >= limit:
            shot['Shot_Caption'] = ""
            continue
        
        print(f"Processing shot {i+1}/{len(shotlist)}...")
        # Only print prompt for first shot
        caption = annotate_shot(shot, i, video_path, film, ollama, frames_dir, project_root, print_prompt=(processed == 0))
        shot['Shot_Caption'] = caption
        
        if caption:
            print(f"Ollama response for shot {i}:")
            print(caption)
            print("-" * 80)
        
        processed += 1
    
    return shotlist

def annotate_scenes(shotlist: List[Dict]) -> List[Dict]:
    """
    Annotate each scene with Scene_Caption.
    All shots in the same scene get the same caption.
    Returns the modified shotlist.
    """
    if not has_scenes(shotlist):
        raise ValueError("Cannot annotate scenes: No scene information found in shotlist")
    
    # Group shots by scene
    scenes = {}
    for shot in shotlist:
        scene_id = shot.get('Scene', '').strip()
        if scene_id:
            if scene_id not in scenes:
                scenes[scene_id] = []
            scenes[scene_id].append(shot)
    
    # Annotate each scene
    for scene_id, shots in scenes.items():
        caption = annotate_scene(scene_id, shots)
        for shot in shots:
            shot['Scene_Caption'] = caption
    
    return shotlist