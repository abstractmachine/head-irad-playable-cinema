# playable-nickelodeon
The Playable Nickelodeon is a hardware based playback device that brings the entire project closer to the autonomous, edge-computing goals of this project. These goals align with the [DataCraft](https://datacrafty.ch) research project where our long-term goals are to find/invent/hack various means for creating smaller, offline, and/or autonomous AI inferencing and training strategies for our art & design projects.

This code is designed to run on a low-cost Raspbery Pi 5 device, ideally with an internal SSD card with sufficient size to hold the entire collection of videos that will be played back by the device.

A local network protocol allows the real-time inferencing system running on its own hardware to send `"{"action":"play}"` requests to this Nickelodeon player, hence offloading the power and computation requirements of two different part of the entire system. In this way, the video playback can be optimized for low-cost hardware computation, and the inferencing can equally be chosen for the specific hardware needs of the real-time video inferencing system, using larger graphics cards but equally on locally maintained hardware.

## Demo
[<img src="./images/playable-nickelodeon-week-playback-test-a.png" height="320" alt="Nickelodeon weeklong playback test">](https://youtu.be/lhw2UKQBqmM) [<img src="./images/playable-nickelodeon-week-playback-test-b.png" height="320" alt="Nickelodeon weeklong playback test">](https://youtu.be/sbAsKG1wDwo)

Links: [https://youtu.be/lhw2UKQBqmM](https://youtu.be/lhw2UKQBqmM), [https://youtu.be/lhw2UKQBqmM](https://youtu.be/lhw2UKQBqmM)

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