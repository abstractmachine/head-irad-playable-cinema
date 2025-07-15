from ultralytics import YOLO
import torch

device = "mps" if torch.backends.mps.is_available() else "cpu"
print("Using device:", device)

model = YOLO("yolov8n.pt").to(device)
model.train(data="playable-cinema-dataset/data.yaml", epochs=50, device=device)