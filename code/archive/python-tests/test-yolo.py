# pyenv global 3.11.9

from ultralytics import YOLO

model = YOLO("model.pt")			# your local Roboflow-trained model
for result in model("inside.mp4", stream=True):
	result.show()					# shows frame with detections drawn
	
	