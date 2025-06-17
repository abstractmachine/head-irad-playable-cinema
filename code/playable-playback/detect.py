from ultralytics import YOLO

model = YOLO("model.pt")  # your locally trained model
for result in model("inside.mp4", stream=True):
	result.show()          # shows frame with bounding boxes
	