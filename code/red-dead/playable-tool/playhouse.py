from player import AbstractPlayerWindow

class PlayhouseWindow(AbstractPlayerWindow):
    def __init__(self, ui):
        super().__init__(ui)
        self.setWindowTitle("Playhouse")

    def on_request_save(self):
        pos = self.pos()
        size = self.size()
        self._pending_save_data = {
            "x": pos.x(),
            "y": pos.y(),
            "width": size.width(),
            "height": size.height(),
            "normal_seek": self.normal_seek.text(),
            "fast_seek": self.fast_seek.text()
        }

    def on_request_load(self, data):
        if "normal_seek" in data:
            self.normal_seek.setText(data["normal_seek"])
        if "fast_seek" in data:
            self.fast_seek.setText(data["fast_seek"])
        if "x" in data and "y" in data:
            self.move(data["x"], data["y"])
        if "width" in data and "height" in data:
            self.resize(data["width"], data["height"])
            