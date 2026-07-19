# Cowpoke Lectern — Starter README

Project: A Raspberry Pi 5 based physical lectern module that uses an RPI AI Camera (IMX500 compute module) running a custom YOLO11 model to detect printed engravings on catalogue pages and trigger a projector (beamer) video loop and controls a programmable LED string inside a Banker's Lamp.

**Status:** Starter README — hardware assembled and base OS installed. Software and model integration in progress.

**Overview**
- Purpose: a museum/installation lectern which recognizes engraved icons printed in a catalog and plays a video loop (via an integrated beamer) and controls a programmable LED string inside a Banker's Lamp.
- Primary components:
    - Raspberry Pi 5 (host)
    - RPI AI Camera (IMX500 compute module)
    - M.2 NVMe hat + 1 TB SSD
    - programmable LED string inside a green Banker's Lamp
    - 30° slanted reading surface (lectern)
    - beamer connected to the Pi for video output.

Hardware
- Raspberry Pi 5 (64-bit desktop image)
- RPI AI Camera (IMX500 compute module)
- M.2 HAT (for NVMe SSD) with 1 TB NVMe drive sized/powered for Raspberry Pi power profile
- Banker's Lamp
- addressable LED string wired to the Pi's GPIO
- Beamer connected via HDMI output from the Pi
- Printed catalog(s) sized to fit the 30° slanted lectern surface

Software & Model
- OS: Raspberry Pi OS (64-bit Desktop) installed via Raspberry Pi Imager.
- Camera: IMX500 camera stack/software (follow vendor/Raspberry Pi camera documentation for drivers and runtime). The camera will run inference on the IMX500 compute module and/or on the Pi depending on the chosen runtime.
- Model: custom YOLO11 detection model trained to recognize engraved icons on printed pages. Training strategy includes synthetic re-orientations and noise to match printed icon variations.
- Inference/runtime: choose a lightweight runtime compatible with the IMX500 compute module (or run on the Pi CPU/GPU as needed). Example runtimes: OpenVINO, TensorRT, ONNX Runtime, or vendor SDK — pick what's supported for the camera compute module.

Installation (starter steps)
1. Flash OS
   - Use the Raspberry Pi Imager to flash the official Raspberry Pi OS (64-bit Desktop) to your chosen drive. See: https://www.raspberrypi.com/software/

2. Hardware: M.2 HAT + SSD
   - Install the M.2 HAT following [M.2 HAT docs and Raspberry Pi PCIe documentation](https://www.raspberrypi.com/documentation/accessories/m2-hat-plus.html)

3. Camera: IMX500
   - Install the IMX500 camera follwing the [IMX500/AI Camera install guide](https://www.raspberrypi.com/documentation/accessories/ai-camera.html)

4. System updates and base packages
   - After first boot, update the system and install essentials:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3 python3-venv python3-pip git ffmpeg
```

5. Model & app
   - Create a Python virtual environment and install required packages for inference and projector control.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -U pip
# Example packages; replace with the ones your model/runtime requires
pip install opencv-python torch torchvision onnxruntime
```

6. Lamp & beamer integration
   - Wire the programmable LED string to the Pi (via GPIO + level shifter or a USB/serial LED controller) and test simple color patterns.
   - Connect the beamer to the Pi's video output and verify the projector is recognized; configure the desktop or direct framebuffer output for the video loop.

Development notes
- Model training: create synthetic augmentations of original engravings (rotation, scale, noise, printing artifacts) to train YOLO11. Use standard YOLO dataset format (labels + images). Validate on photos of printed test pages.
- Detection robustness: calibrate camera exposure, distance, and lighting to minimize false negatives. Consider adding a homography or alignment pass if pages are placed at variable angles.
- Power budgeting: confirm the M.2 HAT + NVMe + Pi5 + camera + LED string operate within your power supply. Use a power supply rated for the combined peak draw.

Next steps
- Train and test the YOLO11 model with synthetic augmentations and printed test pages.
- Integrate the inference runtime with the IMX500 camera and prototype detection-to-video mapping.
- Use Ultralytics Python libraries to convert the `model.pt` to the final package to upload to the AI Camera module (IMX500)
- Wire and test the lamp LED controller and projector output from the Pi.