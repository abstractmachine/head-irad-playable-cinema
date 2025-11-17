import requests
import json
import base64
from typing import Optional, Dict, Any, List
import os

class OllamaClient:
    """
    Simple client to interact with local Ollama instance (vision + JSON).
    """
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "gemma3:27b", num_ctx: Optional[int] = None, temperature: float = 0.3):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx or int(os.getenv("OLLAMA_NUM_CTX", "8192"))
        self.temperature = temperature  # Default 0.3 for varied but controlled output
        self.session = requests.Session()

    def test_connection(self) -> bool:
        try:
            r = self.session.get(f"{self.base_url}/api/tags", timeout=3)
            return r.ok
        except Exception:
            return False

    def generate(self, prompt: str, system: str = "", stream: bool = False) -> Optional[str]:
        """
        Use /api/generate with JSON output and enlarged context window.
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "format": "json",
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": self.temperature
            },
            "stream": stream,
        }
        try:
            r = self.session.post(f"{self.base_url}/api/generate", json=payload, timeout=None)
            r.raise_for_status()
            data = r.json()
            return data.get("response")
        except Exception as e:
            print(f"Ollama error: {e}")
            return None

    def generate_with_images(
        self,
        prompt: str,
        image_paths: List[str],
        stream: bool = False,
        system: Optional[str] = None,
        schema: Optional[dict] = None
    ) -> Optional[str]:
        """
        Send a prompt with images; optional system prompt + JSON schema.
        Enforces format="json" with configurable temperature.
        """
        url = f"{self.base_url}/api/generate"
        images = []
        for image_path in image_paths:
            try:
                with open(image_path, 'rb') as f:
                    image_data = base64.b64encode(f.read()).decode('utf-8')
                    images.append(image_data)
            except Exception as e:
                print(f"Error reading image {image_path}: {e}")
                continue
        
        if not images:
            print("No images could be loaded")
            return None
        
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": stream,
            "images": images,
            "format": schema if schema else "json",
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": self.temperature  # Use configurable temperature
            }
        }
        
        if system:
            payload["system"] = system
        
        try:
            response = self.session.post(url, json=payload, timeout=None)
            response.raise_for_status()
            
            if stream:
                full_response = ""
                for line in response.iter_lines():
                    if line:
                        data = json.loads(line)
                        if 'response' in data:
                            full_response += data['response']
                return full_response
            else:
                data = response.json()
                return data.get('response', '')
        
        except requests.exceptions.RequestException as e:
            print(f"Error connecting to Ollama: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"Error parsing Ollama response: {e}")
            return None