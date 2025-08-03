import csv
from PyQt5.QtCore import QObject, pyqtSignal

class ShotlistLoadWorker(QObject):
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, csv_path):
        super().__init__()
        self.csv_path = csv_path

    def run(self):
        try:
            with open(self.csv_path, "r", newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            self.finished.emit(rows)
        except Exception as e:
            self.error.emit(str(e))