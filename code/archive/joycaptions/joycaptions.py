from transformers import AutoProcessor, AutoModelForVision2Seq
from PIL import Image
import torch

# Device setup MAC M
device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
# Device setup for CUDA (CUDA = NVIDIA GPUs)
# device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(device)

# Load model and processor
model = AutoModelForVision2Seq.from_pretrained("fancyfeast/llama-joycaption-beta-one-hf-llava").to(device)
processor = AutoProcessor.from_pretrained("fancyfeast/llama-joycaption-beta-one-hf-llava")

# Load image
image = Image.open("cowpeoples.jpg")
prompt = "Describe this image in detail."

# Preprocess
inputs = processor(prompt, images=image, return_tensors="pt").to(device)

# Generate
outputs = model.generate(**inputs, max_length=128)

# Decode and print result
print(processor.decode(outputs[0], skip_special_tokens=True))