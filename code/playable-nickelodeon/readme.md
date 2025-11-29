# playable-playback
Notes for current autonomous playback device.

## Demo
[![Nickelodeon weeklong playback test](./images/playable-nickelodeon-week-playback-test-a.png)](https://youtu.be/lhw2UKQBqmM)
[![Nickelodeon weeklong playback test](./images/playable-nickelodeon-week-playback-test-b.png)](https://youtu.be/lhw2UKQBqmM)

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

## Communication

### Ping
nc -w 1 127.0.0.1 6666 <<< '{"type":"ping"}'

### List films
nc -w 1 127.0.0.1 6666 <<< echo '{"type":"list"}'

### Play random
nc -w 1 127.0.0.1 6666 <<< echo '{"type":"random"}'

### Play by index
nc -w 1 127.0.0.1 6666 <<< echo '{"type":"play", "index":5, "start":60000}'

### Status
nc -w 1 127.0.0.1 6666 <<< echo '{"type":"status"}'

### Gremlin on
nc -w 1 127.0.0.1 6666 <<< echo '{"type":"gremlin", "action":"on"}'

### Gremlin speed
nc -w 1 127.0.0.1 6666 <<< echo '{"type":"gremlin", "action":"speed", "seconds":3}'