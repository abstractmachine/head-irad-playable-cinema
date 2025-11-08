# playable-playback
Notes for current autonomous playback device.

## HEVC Playback
ChatGPT says that we actually need H.265/HEVC on RPI5.

## Installation

### Install PyQt5 system-wide
sudo apt-get update
sudo apt-get install python3-pyqt5

### Recreate venv with system packages access
rm -rf playable-cinematheque
python3 -m venv --system-site-packages playable-cinematheque

### Activate and install remaining deps
source playable-cinematheque/bin/activate
pip install ffmpeg-python
