# PI5 + AI Hat+
We are going to explore using the following configuration for live captioning via [BLIP](https://huggingface.co/docs/transformers/en/model_doc/blip):
- [Raspberry Pi 5 16GB](https://www.pi-shop.ch/raspberry-pi-5-16gb-ram)
- [Raspberry AI Hat+ 26 Tops](https://www.pi-shop.ch/raspberry-pi-ai-hat-26t)
    - [AI Hat Documentation](https://www.raspberrypi.com/documentation/accessories/ai-hat-plus.html)
- [Raspberry Pi Official 5 power supply, 27W USB-C](https://www.galaxus.ch/en/s1/product/raspberry-pi-official-5-power-supply-27w-usb-c-development-board-accessories-38955882?skipAppLink=true) (cf. [Power Delivery](#power-delivery))
- HDMI Input
    A. [HDMI > CSI Adapter](https://www.waveshare.com/wiki/HDMI_to_CSI_Adapter)
    B. [Elgato 4K Camlink](https://www.elgato.com/ch/en/p/cam-link-4k)
        - Note: easier to use with a small USB 3.0 Type A extension cable
        - HDMI Cable
- [Raspberry Pi RASP CAM FPC camera cable](https://www.galaxus.ch/en/s1/product/raspberry-pi-rasp-cam-fpc-30-camera-cable-1x-csi-1x-csi-030-m-development-board-accessories-39976873)
- [Raspberry Pi Active Cooler](https://www.raspberrypi.com/products/active-cooler/)

## Power Delivery
First surprise: once we plugged in the AI Hat+ and turned on the PI 5, we immediately got a warning from Raspbian that we needed a 5A power supply. The Pi uses 5V, and is fine on its own with the official 5V 3A (15W) power supply. But as soon as you plug in the AI Hat+, you'll apparently need the 25.5W (5.1V/5A) power supply, also sold as the 27W power supply.

## Camera Connector
Careful, the RPI5 has changed the size of its camera module connectors. It requires a smaller, 0.5mm pitch 22-pin connector, while our HDMI adaptor requires a 15-pin standard 1.0mm-pitch connector. There are official and unofficial cables for this.

## Hardware Installation
Follow instructions at [AI HAT+ Installation](https://www.raspberrypi.com/documentation/accessories/ai-hat-plus.html#ai-hat-plus-installation), and then [Getting Started with the AI HAT+](https://www.raspberrypi.com/documentation/computers/ai.html).

## EEPROM Update
```
$ sudo rpi-eeprom-update
```
Our EEPROM was from 2205, so we're okay.


## 64-Bit RPI
Make sure we are running the 64-bit variant of Raspbian:
```
$ uname -m
```

If all goes well you'll see:
```
aarch64
```

## Upgrade OS
```
playback@username: ~ $ sudo apt update && sudo apt full-upgrade
```

## PCIe Gen 3.0
We want fast PCI, so we will [activate PCIe Gen 3.0](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html). According to this link it says:

> Run the following command to open the Raspberry Pi Configuration CLI:
```
sudo raspi-config
```
> Complete the following steps to enable PCIe Gen 3.0 speeds:
> - Select Advanced Options.
> - Select PCIe Speed.
> - Choose Yes to enable PCIe Gen 3 mode.
> - Select Finish to exit.
> - Reboot your Raspberry Pi with sudo reboot for your changes to take effect.

## Hailo
```
$ sudo apt install hailo-all
```

Reboot, then test the hardware installation with :

```
$ hailortcli fw-control identify
```

## Elgato 4K Camlink
Since we don't have the right cable yet for Raspberry PI 5 (see above), we've plugged in a [Elgato 4K Camlink](). But apparently the drivers aren't setup yet, because this gave nothing:

```
$ rpicam-hello -t 10s
```

So we're going to try these instructions: [High Resolution Video Capture on the Raspberry Pi](https://blog.j2i.net/2021/11/12/high-resolution-video-capture-on-the-raspberry-pi/). 

```
$ sudo apt install v4l-utils
$ 
```

That all works. Now it appears we can switch to USB Camera in the Hailo Examples from their github, cf. [Hailo RPI5 Examples](https://github.com/hailo-ai/hailo-rpi5-examples). 

To use these examples, we go into the folder, and run first:

```
$ source setup_env.sh
```

Then one of the examples, and you can even supply the `--usb` parameter to get the `Elgato CamLink 4k` to input the HDMI video feed:

```
$ python basic_pipelines/detection_simple.py --usb
```

This works great. The `yolo` model loads and everything runs super fast thanks to the `AI Hat+ 26T`.

