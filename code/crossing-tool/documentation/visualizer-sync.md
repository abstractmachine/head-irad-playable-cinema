# Synchronizer

The synchronizer syncs up live and recorded video input with pre-recorded video playback. It eventually could sync other types of input↔output.


## Dev Notes

These are the current dev notes as I start installing the tool chain.

### PC

```
Architecture:                x86_64
  CPU op-mode(s):            32-bit, 64-bit
  Address sizes:             48 bits physical, 48 bits virtual
  Byte Order:                Little Endian
CPU(s):                      16
  On-line CPU(s) list:       0-15
Vendor ID:                   AuthenticAMD
  Model name:                AMD Ryzen 7 9700X 8-Core Processor
```

### GPU

- NVIDIA RTX PRO 4500 Blackwell 32 Gb VRAM

### Game Console

- Sony PS4 Pro
- Samsung MZ-77E500B - 870 EVO SATA III 2.5" SSD 500 GB 560MB/530MB R/W

### HDCP

The Sony had High-bandwidth Digital Content Protection enabled by default. This needs to be disabled:

```
Settings → System → Enable HDCP → Off
```

### Capture

- HDMI Splitter 1 in > 2 out
- ~~PureTools `PT-C-HBUSB` USB-3 Capture Card~~ (Junk)
- Elgato Cam Link 4k

As it turns out, Cam Link 4K works fine on our Ubuntu install, contrary to some online info.

### STTY

A weird but happens after some `ffmpeg` commands where my commandline keeps dissapearing. I fixed it with:

```
$ stty sane
```

### Install

```
sudo apt install v4l-utils
sudo apt install libusb-1.0-0-dev
```

If working:

```
$ v4l2-ctl --list-devices
ioctl: VIDIOC_ENUM_FMT
	Type: Video Capture

	[0]: 'YUYV' (YUYV 4:2:2)
		Size: Discrete 1920x1080
			Interval: Discrete 0.017s (60.000 fps)
			Interval: Discrete 0.033s (30.000 fps)

```

Test:

```
$ ffplay /dev/video0
```

This gives me the lowest latency playback, but so far with no audio.

### Audio Passthrough

```
$ sudo apt install pulseaudio-utils
```

Turn on PS4 audio passthrough:

```
$ pactl load-module module-loopback source=alsa_input.usb-Cubeternet_eEver_USB_Device-00.analog-stereo latency_msec=1
536870913
```

```
$ pactl unload-module module-loopback
```

Or by name:

```
$ pactl unload-module 536870913
```

