# Cowpoke Controller — firmware installation and development

This guide explains how to prepare a Mac, build the controller firmware from
source in Visual Studio Code, install it, and return to a working version after
an experiment. Each stage has a checkpoint; complete them in order.

The Cowpoke Controller uses [GP2040-CE](https://gp2040-ce.info/), an open-source
C++ firmware project for RP2040 microcontrollers. Its browser-based configurator
stores settings separately from the firmware program.

## Status of this guide

**Draft for validation on the Cowpoke hardware.** The source build described
here has succeeded on an Apple Silicon Mac. Installing that local build and
testing all Cowpoke controls have not yet been validated.

The existing prototype reports the official **v0.7.12 Pico** firmware, build
`0014e4a`.

**TODO:** publish the verified Cowpoke
pin assignments and a configuration containing its analog-stick, encoder, and
button settings. Compiling the stock Pico firmware alone does not recreate
those settings. Until they are published, the configuration steps below require
a backup from a working Cowpoke Controller.

## 1. Gather the hardware and software

- [ ] Apple Silicon Mac
- [ ] The RP2040 Cowpoke circuit and a USB cable that carries data.
- [ ] Access to the circuit's small **BOOT** and **S2** buttons.
- [ ] [Visual Studio Code](https://code.visualstudio.com/).
- [ ] [Homebrew](https://brew.sh/), installed using its official instructions.
- [ ] Apple's Command Line Tools. In Terminal, run `xcode-select -p`; if absent: `xcode-select --install`.

This version of the guide targets Apple Silicon (`uname -m` returns `arm64`).
Intel Macs require the x86_64 Arm compiler archive and different Homebrew paths.

### Understand the three connection modes

| Mode | How to enter | What appears on the Mac |
| --- | --- | --- |
| Controller | Connect normally | A game controller, e.g. `GP2040-CE (PS4)` |
| Configuration | Hold S2 while connecting, then release | Configurator at `http://192.168.7.1`; USB name may be `TinyUSB Device` |
| Firmware loading (BOOTSEL) | Hold BOOT while connecting, then release | A removable drive named `RPI-RP2` |

On the current prototype, RUN, S1, and S2 are the three small blue buttons along
the bottom of the left circuit board. S2 is the rightmost. BOOT is the separate
small blue button near the directional buttons. RUN resets the microcontroller.

## 2. Install the development tools

- [ ] Open VS Code. Choose **Terminal → New Terminal**. Use its default macOS
      shell (zsh) for the commands in this guide.
- [ ] Install the supporting tools:

```sh
brew install git node python@3.13 ninja picotool
```

| Tool | Purpose |
| --- | --- |
| Git | Download source code and track your changes |
| Node.js / npm | Build the browser configurator embedded in the firmware |
| Python 3.13 | Generate firmware configuration code |
| Ninja | Run compilation tasks |
| picotool | Inspect, back up, and restore RP2040 firmware |

- [ ] Check that the tools respond:

```sh
git --version
node --version
npm --version
/opt/homebrew/bin/python3.13 --version
ninja --version
picotool version
```

**Checkpoint:** each command prints a version. Firmware development does not
require the controller to be connected yet.

## 3. Create a workspace and download the source

The examples use `~/Developer/cowpoke-firmware`. Choose an empty folder so this
procedure does not overwrite an existing development project.

- [ ] Create and enter the workspace:

```sh
mkdir -p ~/Developer/cowpoke-firmware
cd ~/Developer/cowpoke-firmware
mkdir -p toolchains backups releases
```

- [ ] Download the firmware at the known release:

```sh
git clone --branch v0.7.12 --depth 1 \
  https://github.com/OpenStickCommunity/GP2040-CE.git
git -C GP2040-CE submodule update --init --recursive
git -C GP2040-CE switch -c cowpoke/development
```

The initial clone may report a detached HEAD. The last command creates a branch
where you can keep your changes.

- [ ] Confirm the source revision:

```sh
git -C GP2040-CE rev-parse HEAD
```

Expected: `0014e4ae2a312332e2582f6708dcc7d6bec5de8c`.

- [ ] Download the Pico SDK and its required libraries:

```sh
git clone --branch 2.1.1 --depth 1 \
  https://github.com/raspberrypi/pico-sdk.git
git -C pico-sdk submodule update --init lib/mbedtls lib/lwip lib/tinyusb
```

The SDK supplies RP2040 hardware support. The firmware also has its own pinned
USB libraries; keep both sets of dependencies at the revisions selected above.

- [ ] In VS Code, choose **File → Open Folder…** and select
      `~/Developer/cowpoke-firmware`. Open a new integrated terminal there.

This guide uses VS Code as the editor and its integrated Terminal for builds.
It does not depend on automatic Pico project import or an extension downloading
different SDK/compiler versions. The Microsoft C/C++ extension is optional.

## 4. Install the compiler and compatible build tools

The Arm compiler creates code for the RP2040, rather than for the Mac.

- [ ] From the workspace root, download the Apple Silicon compiler:

```sh
curl -fL --retry 2 \
  -o toolchains/arm-gnu-toolchain-14.2.rel1-darwin-arm64-arm-none-eabi.tar.xz \
  https://armkeil.blob.core.windows.net/developer/Files/downloads/gnu/14.2.rel1/binrel/arm-gnu-toolchain-14.2.rel1-darwin-arm64-arm-none-eabi.tar.xz
```

- [ ] Check the archive before extracting:

```sh
shasum -a 256 toolchains/arm-gnu-toolchain-14.2.rel1-darwin-arm64-arm-none-eabi.tar.xz
```

For the archive used to validate this guide, the SHA-256 is:

```text
c7c78ffab9bebfce91d99d3c24da6bf4b81c01e16cf551eb2ff9f25b9e0a3818
```

- [ ] Extract it:

```sh
tar -xf toolchains/arm-gnu-toolchain-14.2.rel1-darwin-arm64-arm-none-eabi.tar.xz \
  -C toolchains
```

- [ ] Install CMake into a project-local Python environment:

```sh
/opt/homebrew/bin/python3.13 -m venv toolchains/build-tools
toolchains/build-tools/bin/pip install cmake==3.31.10
```

This release of GP2040-CE contains a build-system expression rejected by CMake
4.5. Using CMake 3.31.10 allows the original source to build without editing it.

## 5. Describe the build environment

- [ ] In VS Code, create `env.sh` at the workspace root with this content:

```sh
# Run "source env.sh" while the Terminal is at the workspace root.
export COWPOKE_ROOT="$PWD"
export PICO_SDK_PATH="$COWPOKE_ROOT/pico-sdk"
export PICO_TOOLCHAIN_PATH="$COWPOKE_ROOT/toolchains/arm-gnu-toolchain-14.2.rel1-darwin-arm64-arm-none-eabi"
export PATH="$COWPOKE_ROOT/toolchains/build-tools/bin:$PICO_TOOLCHAIN_PATH/bin:/opt/homebrew/bin:$PATH"
export GP2040_BOARDCONFIG=Pico
export PICO_BOARD=pico
export PIP_CONSTRAINT="$COWPOKE_ROOT/python-constraints.txt"
```

These settings tell CMake where to find the compiler and SDK, and select the
same `Pico` build configuration used on the current prototype. They affect only
the Terminal session in which the file is sourced.

- [ ] Create `python-constraints.txt` alongside it:

```text
setuptools==80.9.0
grpcio-tools==1.71.0
grpcio==1.71.0
protobuf==5.29.6
```

The upstream code generator still imports `pkg_resources`, removed from newer
setuptools. These constraints retain compatible dependencies. Python 3.13
allows the pinned grpcio-tools package to install without compiling it itself.

- [ ] Load and check the environment:

```sh
source env.sh
cmake --version
arm-none-eabi-gcc --version
```

**Checkpoint:** CMake reports **3.31.10**; the compiler reports **14.2.1**.
Return to the workspace root and source `env.sh` again whenever you start a new
Terminal session.

## 6. Build the web configurator

- [ ] Install the locked JavaScript dependencies and compile the interface:

```sh
cd "$COWPOKE_ROOT/GP2040-CE/www"
npm ci
npm run build
cd "$COWPOKE_ROOT"
```

This builds the browser interface and generates `GP2040-CE/lib/httpd/fsdata.c`
for embedding in the firmware. A Vite warning about large JavaScript chunks is
not a failed build; check the command's final result.

**Checkpoint:** both commands finish successfully and `fsdata.c` exists.

## 7. Configure and compile the firmware

- [ ] Generate the build instructions:

```sh
cmake -S GP2040-CE -B build/Pico -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
  -DPython3_EXECUTABLE=/opt/homebrew/bin/python3.13 \
  -DSKIP_SUBMODULES=TRUE \
  -DSKIP_WEBBUILD=TRUE \
  -DFETCHCONTENT_UPDATES_DISCONNECTED=ON
```

The skip options mean the submodules and web interface have already been
prepared in previous steps. CMake may still download other dependencies on
the first configuration.

- [ ] Compile:

```sh
cmake --build build/Pico --parallel 8
```

- [ ] Inspect the resulting firmware file:

```sh
picotool info -a build/Pico/GP2040-CE_0.7.12_Pico.uf2
```

**Checkpoint:** the UF2 exists and picotool reports GP2040-CE v0.7.12, SDK 2.1.1,
board `pico`, and a Release build. The UF2 is the file installed on the circuit;
the ELF alongside it is useful for development/debugging.

Matching the source and tool versions does not guarantee byte-for-byte identity
with the official downloadable UF2, which uses a different build environment.

## 8. Back up an existing Cowpoke Controller

Skip this section only when working with a new, empty circuit.

- [ ] Hold S2 while connecting the controller to the Mac, then release.
- [ ] Allow the accessory connection if macOS asks.
- [ ] Open `http://192.168.7.1` in a browser.
- [ ] Export all sections from **Configuration → Data Backup and Restoration**.
      Save the downloaded file in your private `backups` folder.
- [ ] Unplug, then hold BOOT while reconnecting. Release it when `RPI-RP2`
      appears in Finder.
- [ ] Connect only the board you intend to back up, then run:

```sh
picotool info -a
picotool save -a -v backups/cowpoke-original.uf2
```

Use a new filename if that backup already exists.

**Checkpoint:** picotool reports a successful verified save. This full flash
backup preserves the firmware and stored configuration. The web settings export
excludes PS4 credentials; the full flash image can contain them. Keep full device
backups out of a public repository.

## 9. Install the firmware

- [ ] Confirm the current controller is backed up, or that this is a new circuit.
- [ ] Enter BOOT mode: hold BOOT while connecting, then release.
- [ ] In Finder, copy `build/Pico/GP2040-CE_0.7.12_Pico.uf2` onto `RPI-RP2`.
- [ ] Wait for the drive to disappear as the board reboots.

Do not use `flash_nuke.uf2` for an ordinary same-version development update.
Flash-nuke erases configuration and is a separate recovery/reset operation.
Consult the official installation instructions if the board previously ran
unrelated firmware.

An ordinary update normally retains the stored settings. Consequently, a
successful update on an existing controller does not prove a new board can be
configured from scratch.

## 10. Apply the Cowpoke configuration

For an existing controller, use S2 to open the configurator again and check that
the button, analog, encoder and mode settings survived. Restore the saved web
configuration if required.

For a fresh circuit, apply the verified configuration for that PCB revision.
This guide needs the following public project material before that procedure
can be completed independently:

- [ ] PCB revision and a table mapping GPIOs to every button and switch.
- [ ] Both thumbsticks' analog connections, orientation, inversion, and dead zones.
- [ ] Rotary encoder connections and configuration.
- [ ] A PS-button mapping or installer-accessible combination.
- [ ] Confirmed S2/configurator access after a fresh install.
- [ ] A reviewed settings export without authentication material, or a dedicated
      `Cowpoke` source configuration providing the same defaults.

Do not substitute the stock Pico pin table for the actual Cowpoke wiring.

## 11. Test the controller

- [ ] Connect normally and inspect its USB identity on macOS:

```sh
ioreg -p IOUSB -w 0
```

In PS4 mode the prototype appears as `GP2040-CE (PS4)`. If the USB identity is
`TinyUSB Device`, check whether it is still in configuration mode.

- [ ] In a gamepad tester or application that supports the selected input mode,
      check every button individually.
- [ ] Check both sticks over their full travel, including their centered values.
- [ ] Check encoder movement in both directions and its push switch if fitted.
- [ ] Reconnect and confirm the selected mode and settings persist.
- [ ] Confirm S2 still provides access to configuration.

### PS4 authentication

PS4 protocol support and PS4 authentication are separate. A Mac identifying the
device as a PS4 controller does not confirm authentication with a console.

GP2040-CE supports uploaded authentication credentials and compatible USB host
authentication hardware. Follow the
[official compatibility documentation](https://gp2040-ce.info/faq/faq-console-compatibility/)
for the chosen method. The public firmware build does not require credentials;
configure authentication separately. Do not commit private keys or full device
backups to the public repository.

- [ ] Select PS4 input mode and Console identification for direct console use.
- [ ] Configure the chosen authentication method.
- [ ] Connect to the PS4, send the PS input and select a user.
- [ ] Test for at least ten minutes to check for the approximately eight-minute
      authentication timeout.

## 12. Make one behavior change at a time

Commit a known working baseline before modifying behavior. Record whether each
change is a setting stored on the controller or a source-code change: those are
separate parts of a reproducible release.

| Area | Source starting point |
| --- | --- |
| Board defaults | `GP2040-CE/configs/Pico/BoardConfig.h` |
| Input processing | `GP2040-CE/src/gp2040.cpp` |
| Analog sticks | `GP2040-CE/src/addons/analog.cpp` |
| Barrel encoder | `GP2040-CE/src/addons/rotaryencoder.cpp` |
| Button sequences | `GP2040-CE/src/addons/input_macro.cpp` |
| Browser interface | `GP2040-CE/www/src/` |

For a C++ change, rebuild from the workspace root:

```sh
cmake --build build/Pico --parallel 8
```

For a web interface change, rebuild the embedded interface first:

```sh
cd "$COWPOKE_ROOT/GP2040-CE/www"
npm run build
cd "$COWPOKE_ROOT"
cmake --build build/Pico --parallel 8
```

Reconfigure with the command in section 7 if the build configuration changes.
Install the new UF2 using section 9, then repeat the affected control tests.
Keep successful builds with their source revision and matching settings.

## 13. Recover the original state

To restore a full backup to the same controller:

- [ ] Enter BOOT mode.
- [ ] From the workspace root, run:

```sh
picotool load -v backups/cowpoke-original.uf2
picotool reboot
```

This replaces the firmware and configuration with the saved state. A backup
from another PCB revision may contain incorrect pin assignments.

If a saved image is unavailable, reinstall the official matching Pico UF2 from
the [v0.7.12 release](https://github.com/OpenStickCommunity/GP2040-CE/releases/tag/v0.7.12),
then reapply the verified Cowpoke configuration. A firmware-only download does
not contain the prototype's custom settings.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No `RPI-RP2` drive | Use BOOT rather than S2; check the data cable and macOS accessory permission |
| No board found by picotool | Put it in BOOT mode and connect only the intended board |
| Configurator unavailable | Enter with S2, allow the accessory, and use `http://192.168.7.1` |
| CMake 4.x error | Source `env.sh` from the workspace root; confirm CMake 3.31.10 is selected |
| `No module named pkg_resources` | Check `PIP_CONSTRAINT`; install setuptools 80.9.0 into `build/Pico/venv` |
| grpcio-tools starts compiling itself | Check that CMake uses `/opt/homebrew/bin/python3.13` |
| Missing `pico_lwip`, `dhserver.c`, or `rndis_reports.c` | Initialize the SDK submodules in section 3 |
| Missing `fsdata.c` | Complete the web build in section 6 |
| Firmware installs but controls differ | Check the saved configuration and actual PCB wiring |
| PS4 stops responding after several minutes | Check authentication, independently of USB identity |

## References and validation record

- [GP2040-CE v0.7.12 source](https://github.com/OpenStickCommunity/GP2040-CE/tree/v0.7.12)
- [Official development environment](https://gp2040-ce.info/development/build-environment/)
- [Official compilation guide](https://gp2040-ce.info/development/compile-firmware/)
- [Official installation and reset procedure](https://gp2040-ce.info/installation/)
- [Configuration backup and restoration](https://gp2040-ce.info/web-configurator/menu-pages/data-backup-restoration/)
- [picotool documentation](https://github.com/raspberrypi/picotool)

2026-11-19: web and Release firmware compilation succeeded on Apple Silicon,
using SDK 2.1.1, Arm 14.2.Rel1, CMake 3.31.10, and Python 3.13. The resulting UF2
was inspected with picotool. The manual checklist above is derived from those
builds; it still requires a complete user walkthrough on a clean workspace and
installation/testing on the Cowpoke circuit before it can be marked validated.
