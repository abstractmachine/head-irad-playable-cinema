# Cowpoke Console
These are the notes for the basic installation steps of setting up the Cowpoke Console. These steps assume starting from scratch as the machine was delivered, i.e. with the following configuration:

## Docs
See `./baseline-system` for more info on installs, especially `torch` build for `Blackwell` architecture.

## PC Build
- Fractal Design Ridge, ITX Case, black
- AMD Ryzen 7 9700X, 3.8 - 5.5 GHz, 8 Cores / 16 Threads,
- High End CPU Cooler
- ASUS ROG STRIX X870-I GAMING WIFI
- 64 GB DDR5-RAM, 5600 MHz, 2 x 32 GB
- PNY NVIDIA RTX PRO 4500 Blackwell, 32 GB GDDR7 ECC
- 2 TB Samsung 9100 PRO PCIe 5 M.2 SSD
- Ubuntu 24.04 LTS, Nvidia CUDA Toolkit

## BIOS
- ROG STRIX X870-I Gaming WIFI
- BIOS Ver. 1644 (Up-to-date as of 2026-03-27)
- AMD Ryzen 7 9700X 8-Core Processor
- Speed: 3800 MHz
- Memory: 65536 MB (DDR5 4800 MHz)
- DIMM_A: Kingston 32768 MB 4800MHz
- DIMM_B: Kingston 32768 MB 4800MHz
- DOCP: Disabled
- NVME: M.2_1: Samsung SSD 9100 PRO 2TB
- CPU Temperature: 65° C
- CPU Core Voltage 1.352 V
- Motherboard Temperature: 38° C
- FAN Profile
    - CPU_FAN: 2080 RPM
    - CHIPSET FAN: 2315 RPM
    - VRM_H5_FAN: N/A
    - CHA_FAN: 1558 RPM
    - AIO_PUMP: N/A

### Settings
- Set the main PCIe x16 slot to Gen4
- enabled Above 4G Decoding and Re-Size BAR

* Note: The Fractal Ridge uses a PCIe 4.0 riser, so forcing Gen4 is the conservative stability choice. Above 4G Decoding and Re-Size BAR support the GPU’s address space correctly.

### Installs
```
sudo apt update
sudo apt install nvidia-driver-580-open
sudo apt install mesa-utils
sudo apt install ffmpeg
```

### Python
```
sudo apt install python3-venv python3-pip
python3 -m venv ~/venvs/playable-cinema
source ~/venvs/playable-cinema/bin/activate
```
