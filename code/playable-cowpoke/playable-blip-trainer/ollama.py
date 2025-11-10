import requests
import json
import base64
from typing import Optional, Dict, Any, List

class OllamaClient:
    """
    Simple client to interact with local Ollama instance.
    """
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llava:latest"):
        self.base_url = base_url
        self.model = model
    
    def generate(self, prompt: str, stream: bool = False) -> Optional[str]:
        """
        Send a prompt to the model and get a response.
        Returns the response text or None if there was an error.
        """
        url = f"{self.base_url}/api/generate"
        
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": stream
        }
        
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            
            if stream:
                # Handle streaming response
                full_response = ""
                for line in response.iter_lines():
                    if line:
                        data = json.loads(line)
                        if 'response' in data:
                            full_response += data['response']
                return full_response
            else:
                # Handle non-streaming response
                data = response.json()
                return data.get('response', '')
                
        except requests.exceptions.RequestException as e:
            print(f"Error connecting to Ollama: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"Error parsing Ollama response: {e}")
            return None
    
    def generate_with_images(self, prompt: str, image_paths: List[str], stream: bool = False) -> Optional[str]:
        """
        Send a prompt with images to the model and get a response.
        Images are encoded as base64.
        Returns the response text or None if there was an error.
        """
        url = f"{self.base_url}/api/generate"
        
        # Encode images as base64
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
        
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": images,
            "stream": stream
        }
        
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            
            if stream:
                # Handle streaming response
                full_response = ""
                for line in response.iter_lines():
                    if line:
                        data = json.loads(line)
                        if 'response' in data:
                            full_response += data['response']
                return full_response
            else:
                # Handle non-streaming response
                data = response.json()
                return data.get('response', '')
                
        except requests.exceptions.RequestException as e:
            print(f"Error connecting to Ollama: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"Error parsing Ollama response: {e}")
            return None
    
    def test_connection(self) -> bool:
        """
        Test if we can connect to Ollama and the model is available.
        """
        try:
            response = self.generate("Hi")
            return response is not None
        except Exception as e:
            print(f"Connection test failed: {e}")
            return False