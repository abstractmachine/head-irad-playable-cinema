# Raspberry Pi
These are various notes on configuring a Raspberry Pi for inferencing (i.e. interacting with an already-trained model).


## Clean Install from Scratch
This is a full step-by-step for installing the Raspberry Pi 5 playback inferencing software.

## SD Card
We are using a `SanDisk 64GB Extreme Pro MicroSDXC UHS-I Card`. Sppeds are announced at `200 MB/s` write and `90 MB/s` read. We formatted it ExFAT.

## Raspbian
Model: Raspberry Pi 5
OS: Raspberry Pi OS (64-bit)

## English
We have selected `Use English language`, which might create a problem with Swiss keyboards, but we want to avoid folders named `Téléchargement` and other weirdities in the command line so we'll first choose English, reboot, then choose the right keyboard if that creates problems.

## Login
See private sheet.

## Updates
For now, we have skipped the automatic updates.

## Eduroam
After lots of complicated hand-wringing and weird chatbot suggestions, it turns out connecting a Raspberry to HES-SO's Eduroam is quite easy:

1. Connect on Raspberry to a temporary phone (sharing) or over Wired ethernet
2. Go to [cat.eduroam.org](http://cat.eduroam.org)
3. Select `Connect your device to eduroam®` > `Click here to download your eduroam installer`
4. Select `HES-SO`
5. We don't want the default `Chrome` installer, we want a Linux installer:
	- Select `Choose another installer to download`
	- Select `Linux`
	- Now click on the previous button, now listed as `eduroam®` with the Linux penguin
	- `Continue`
6. This creates a `python` script. Open the terminal and navigate to the `Downloads` folder
	- `cd ~/Downloads/`
	- Run the pythons script: `python eduroam-linux-HES-SO-eduroam.py`
	- Enter login + password (twice)
7. All done (hopefully)

## ~~Pyenv~~
I had bad luck with `pyenv` during the installation of the dependencies, so I used a system-wide Python installation of the libraries.

## Dependencies
See various `Python` projects (ex: [Playable-Playback](../playable-playback/readme.md)) for their dependencies. 

## System Wide Dependencies Installation
Here is the installs we used:

```
$ sudo apt update
$ sudo apt install python3-opencv
$ sudo python3 -m pip install torch --break-system-packages
$ sudo python3 -m pip install ultralytics --break-system-packages
```

## Video
In the `playable-playback` folder, the `git` repository does not have a video for inferencing (interpreting), so you will have to download/install a `video.mp4` file into the `playback` folder.