import os
import tempfile
import base64
import mimetypes
import time
import cv2

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QTextEdit

from gui.prompt import parse_system_prompt_files

def encode_image(image_array):
    # Create temp file, close handle before writing with OpenCV
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        temp_jpg_path = tmp.name

    # Write image with OpenCV (file is closed)
    cv2.imwrite(temp_jpg_path, image_array)
    mime_type, _ = mimetypes.guess_type(temp_jpg_path)

    # Read and encode
    with open(temp_jpg_path, "rb") as image_file:
        encoded = f"data:{mime_type};base64," + base64.b64encode(image_file.read()).decode("utf-8")

    # Try to remove, retry if needed (Windows)
    for _ in range(3):
        try:
            os.remove(temp_jpg_path)
            break
        except PermissionError:
            time.sleep(0.1)
    return encoded

class ApiWorker(QObject):
    finished = pyqtSignal()
    result = pyqtSignal(object)

    def __init__(self, frames, metadata, row_data, subtitles_window, project_folder=None, movie_filename=None):
        super().__init__()
        self.frames = frames
        self.metadata = metadata
        self.row_data = row_data
        self.subtitles_window = subtitles_window
        self.project_folder = project_folder
        self.movie_filename = movie_filename

    def run(self):        
        images_payload = []
        for frame in self.frames:
            images_payload.append({
                "type": "image_url",
                "image_url": {
                    "url": encode_image(frame),
                    "detail": "high"
                }
            })
        
        # add image-count to metadata
        self.metadata["image-count"] = len(self.frames)

        # Use the new parse_system_prompt_files function
        parsed_prompt = parse_system_prompt_files(
            self.project_folder,
            self.metadata,
            self.row_data,
            self.subtitles_window
        )
        
        # Check result of parsing
        if parsed_prompt == "" or parsed_prompt is None:
            parsed_prompt = "Describe the scene in these images."

        api_key_path = os.path.join(self.project_folder, "preferences", "openai_api_key.txt")
        try:
            with open(api_key_path, "r", encoding="utf-8") as f:
                api_key = f.read().strip()
        except Exception as e:
            caption = f"Error reading API key: {e}"
            self.result.emit(caption)
            self.finished.emit()
            return

        import gui.openai as openai
        messages = [
            {"role": "system", "content": parsed_prompt},
            {"role": "user", "content": images_payload}
        ]
        
        try:
            response = openai.OpenAI(api_key=api_key).chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.2,
                max_tokens=300,
            )
            caption = response.choices[0].message.content
        except Exception as e:
            caption = f"Error generating caption: {e}"

        self.result.emit(caption)
        self.finished.emit()

class SystemPromptEdit(QTextEdit):
    def __init__(self, parent=None, save_callback=None):
        super().__init__(parent)
        self.save_callback = save_callback

    def focusOutEvent(self, event):
        if self.save_callback:
            self.save_callback()
        super().focusOutEvent(event)