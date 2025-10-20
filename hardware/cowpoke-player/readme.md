# Player

## Raspberry Pi 5
Here are the note for the Rasperry Pi 5 video playback device.

### Hardware
- [Raspberry Pi 5 Model B 16Gb](https://www.raspberrypi.com/products/raspberry-pi-5/)
- [Rasbperry Pi Active Cooler](https://www.raspberrypi.com/products/active-cooler/)

With the help of `ChatGPT 5` we have a script [player-doublebuffer.py](../../code/playable-cowpoke/playable-player/player-doublebuffer.py) that works great with correct audio playback and no visible artifacts on a Raspberry PI 5.

### Installation
Based on the above code, here are the dependencies that need to be installed:

```
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv python3-pyqt5 python3-pyqt5.qtquick
sudo apt install -y mpv libmpv2 libmpv-dev
sudo apt install -y mesa-utils libvulkan1 gstreamer1.0-libav
sudo apt install -y gstreamer1.0-pipewire gstreamer1.0-pulseaudio
```

### Previous Installation Tests
Cf. [playable-aihat](../../code/playable-cowpoke/playable-aihat/).

Starting from the above configuration, we're adding:

```
sudo apt update
sudo apt install -y \
  python3 python3-pip \
  python3-pyqt5 python3-pyqt5.qtmultimedia libqt5multimedia5-plugins \
  qtwayland5 \
  gstreamer1.0-tools gstreamer1.0-gl \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  mesa-vulkan-drivers
sudo apt install -y gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav gstreamer1.0-gl
```

The above wasn't apparently complete so trying this:

```
sudo apt update
sudo apt install -y \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base-apps \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav gstreamer1.0-gl
```

That worked so we need to clean up the above install list.

And for audio syncing we are trying this:

```
sudo apt update
sudo apt install -y gstreamer1.0-pipewire pavucontrol
# (alternative if you're using PulseAudio explicitly)
sudo apt install -y gstreamer1.0-pulseaudio
```

```
export GST_AUDIO_SINK=pulsesink    # or: export GST_AUDIO_SINK=pipewiresink
```

Plays back fine with a simple switcher code, which creates a black screen between each switch.

## Jetson Orin Nano 2023
We are trying to test the speed with the [Jetson Nano]() currently in the [Pool numérique]() of the [HEAD – Genève]().

### ~~Installation~~
*Note: we currently are having problems with this machine*

We are testing:

- Jetson Nano 8GB
- [Seeed Studio reComputer J4012]()

These are steps we are using so far: [reComputer_Industrial_Getting_Started](https://wiki.seeedstudio.com/reComputer_Industrial_Getting_Started/). Since our Ubuntu is 22.04, we have downloaded the [Jetpack 6.1]() image for flashing.

## Jetson Nano 2019
For the pure playback, we are testing the Jetson Model `P3450`.

We are downloading the last SDK for this board here: [jetpack-sdk-463](https://developer.nvidia.com/jetpack-sdk-463), and using [Balena Etcher](https://etcher.balena.io/#download-etcher) to flash the card with this image. Our board is the 4Gb model.

### Installation
After the installing the system on a 64Gb SD Card, we chose a username/password.

I asked ChatGPT to suggest some basic tools to have installed on the machine, here is what it suggested:

```
sudo apt update && sudo apt upgrade -y
sudo apt install -y software-properties-common apt-transport-https ca-certificates
sudo apt install -y build-essential cmake pkg-config git wget curl unzip
sudo apt install -y ffmpeg gstreamer1.0-tools gstreamer1.0-plugins-{base,good,bad,ugly} libavcodec-dev libavformat-dev libswscale-dev libv4l-dev v4l-utils
sudo apt install -y python3 python3-pip python3-venv python3-dev
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install numpy opencv-python torch torchvision torchaudio transformers matplotlib tqdm
```

There were problems using `pip` for opencv, so we installed via `apt`. Apparently on [Jetson Nano]() machines this is the better way to do it:

```
python3 -m pip uninstall -y opencv-python opencv-contrib-python opencv-python-headless
sudo apt update
sudo apt install -y python3-opencv libopencv-dev
```

Which worked in our case.

And some other tools we installed:

```
sudo apt install -y htop neofetch tmux tree jq imagemagick
sudo apt install -y git-lfs openssh-client openssh-server
sudo apt install -y gedit gnome-terminal vlc chromium-browser
sudo apt autoremove -y && sudo apt clean
sudo apt install exfat-fuse exfat-utils
```

### Codium
To code, we had to do a bunch of complicated stuff to install [Codium]() on Ubuntu 18.x. 

### Fan
I also created a little daemon to start the fan at startup.

### Fore Quit
To force quit on Ubuntu: `ALT + F4`.

### SSH FS
I'm using SSH FS which took all sorts of finagling (ugh). TODO: update notes on how I did this using SSH keys. To open the remote folder run command `Focus on Connections View` or click on the extension icon that looks like a folder. The connected folder should be visible there.

### Instability
An overnight test revealed that the Nano lost the volume of the drive. Moving on to [Raspberry Pi 5](./readme.md#raspberry-pi-5)