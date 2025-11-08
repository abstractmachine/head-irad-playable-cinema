# Double-buffered player: two QMediaPlayer/QVideoWidget (A/B). While A plays,
# B is loaded and prepared (seeked). Switch only when the other is ready.
import sys, csv, random, os
from PyQt5.QtWidgets import QApplication, QMainWindow, QStackedWidget
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtCore import QUrl, QTimer, Qt
from PyQt5.QtGui import QCursor

PROJECT_ROOT = "/media/playable/PLAYABLE-D/project"
CSV = os.path.join(PROJECT_ROOT, "metadata", "cinematheque.csv")
PLAY_SEC = 5.0             # default seconds between changes (modifiable by keys 0-9)
SAFETY_MS = 1000
MIN_START_MS = 2 * 60 * 1000
END_BUFFER_MS = 2 * 60 * 1000
SWITCH_POLL_MS = 200  # poll interval while waiting for preload


def load_paths(csvfile):
    out = []
    if not os.path.exists(csvfile):
        return out
    with open(csvfile, newline="") as f:
        reader = csv.DictReader(f)
        use_dict = 'filename' in (reader.fieldnames or [])
        f.seek(0)
        if use_dict:
            reader = csv.DictReader(f)
            for row in reader:
                fn = (row.get('filename') or "").strip()
                if fn and not fn.startswith("#"):
                    p = os.path.join(PROJECT_ROOT, "movies", fn)
                    if os.path.exists(p):
                        out.append(p)
        else:
            f.seek(0)
            for row in csv.reader(f):
                if not row: continue
                fn = row[0].strip()
                if fn and not fn.startswith("#"):
                    p = os.path.join(PROJECT_ROOT, "movies", fn)
                    if os.path.exists(p):
                        out.append(p)
    return out

class DoubleBufferPlayer(QMainWindow):
    def __init__(self, paths):
        super().__init__()
        self.paths = paths[:]
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.setStyleSheet("background-color: black;")
        # create two players/widgets
        self.vwidgets = [QVideoWidget(), QVideoWidget()]
        for vw in self.vwidgets:
            vw.setStyleSheet("background-color: black;")
            self.stack.addWidget(vw)
        self.players = [QMediaPlayer(self), QMediaPlayer(self)]
        for p, vw in zip(self.players, self.vwidgets):
            p.setVideoOutput(vw)
        # keep-aspect if available
        try:
            self.vwidgets[0].setAspectRatioMode(Qt.KeepAspectRatio)
            self.vwidgets[1].setAspectRatioMode(Qt.KeepAspectRatio)
        except Exception:
            pass
        # state
        self.current = 0          # index of currently-visible/playing player
        self.ready = [False, False]
        self.start_pos = [0, 0]   # ms start positions precomputed on load
        self.pending_path = [None, None]
        # timers
        self.play_timer = QTimer(self); self.play_timer.setSingleShot(True)
        self.play_timer.timeout.connect(self.request_switch)
        # play interval in milliseconds (can be changed with number keys)
        self.play_interval_ms = int(PLAY_SEC * 1000)
        # begin cycle: choose two distinct files if possible
        if not self.paths:
            return
        p0 = random.choice(self.paths)
        p1 = random.choice([p for p in self.paths if p != p0]) if len(self.paths) > 1 else p0
        self.prepare_media(0, p0)
        self.prepare_media(1, p1)
        # when first becomes ready, start it
        # connect handlers
        self.players[0].mediaStatusChanged.connect(lambda s: self.on_media_status(0, s))
        self.players[1].mediaStatusChanged.connect(lambda s: self.on_media_status(1, s))

    def prepare_media(self, idx, path):
        # set up media to load and be ready (seeked, paused)
        self.ready[idx] = False
        self.pending_path[idx] = path
        url = QUrl.fromLocalFile(os.path.abspath(path))
        self.players[idx].setMedia(QMediaContent(url))
        # start loading; mediaStatusChanged handler will set start pos and pause
        # calling play triggers metadata load on many backends
        try:
            self.players[idx].play()
        except Exception:
            pass

    def on_media_status(self, idx, status):
        from PyQt5.QtMultimedia import QMediaPlayer
        if status not in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            return
        duration_ms = self.players[idx].duration()
        if duration_ms <= 0:
            return
        # compute allowed start window
        max_start = max(0, duration_ms - END_BUFFER_MS - SAFETY_MS)
        if max_start <= 0:
            start = 0
        elif max_start <= MIN_START_MS:
            start = max_start
        else:
            start = random.randint(MIN_START_MS, max_start)
        # pause, seek to start, mark ready (stay paused)
        try:
            self.players[idx].pause()
        except Exception:
            pass
        self.players[idx].setPosition(start)
        self.start_pos[idx] = start
        self.ready[idx] = True
        # disconnect handled implicitly by ignoring later statuses
        # if this is the first that became ready and nothing is playing yet => start it
        if self.stack.currentIndex() == -1 or (not any(self.ready) and idx == self.current):
            pass
        if idx == self.current and not self.players[idx].state() == QMediaPlayer.PlayingState:
            # start current player playback
            self.stack.setCurrentIndex(idx)
            self.players[idx].play()
            # use current interval
            self.play_timer.start(self.play_interval_ms)
        # If current isn't playing yet (first-run), and idx == current, we started above.
        # Otherwise just leave prepared.

    def request_switch(self):
        # attempt to switch to the other buffer, but only when it's ready
        other = 1 - self.current
        if self.ready[other]:
            self.switch_to(other)
        else:
            # poll until ready
            QTimer.singleShot(SWITCH_POLL_MS, self.request_switch)

    def switch_to(self, other):
        # stop current, play other, and start preloading a new file into the now-inactive slot
        try:
            self.players[self.current].pause()
            self.players[self.current].setPosition(0)
        except Exception:
            pass
        # make other visible and start playback from prepared position
        self.stack.setCurrentIndex(other)
        self.players[other].play()
        # prepare next clip into the old slot
        old = self.current
        self.current = other
        self.ready[old] = False
        # pick a new random file (avoid currently visible file if possible)
        choices = [p for p in self.paths if p != self.pending_path[other]]
        next_path = random.choice(choices) if choices else self.pending_path[other]
        self.prepare_media(old, next_path)
        # start timer for next switch using current interval
        self.play_timer.start(self.play_interval_ms)

    def keyPressEvent(self, event):
        k = event.key()
        # numeric keys 0-9 change the interval (0 -> 0.5s, 1..9 -> seconds)
        if Qt.Key_0 <= k <= Qt.Key_9:
            digit = k - Qt.Key_0
            if digit == 0:
                seconds = 0.5
            else:
                seconds = float(digit)
            self.play_interval_ms = int(seconds * 1000)
            # if a timer is already running, restart it with new interval for consistency
            if self.play_timer.isActive():
                self.play_timer.start(self.play_interval_ms)
            return
        # q or space quits
        if k in (Qt.Key_Q, Qt.Key_Space):
            try:
                for p in self.players:
                    p.stop()
            except Exception:
                pass
            QApplication.quit()

if __name__ == "__main__":
    paths = load_paths(CSV)
    app = QApplication(sys.argv)
    app.setOverrideCursor(QCursor(Qt.BlankCursor))
    w = DoubleBufferPlayer(paths)
    w.showFullScreen()
    sys.exit(app.exec_())