# PyTorch Build for NVIDIA Blackwell (sm_120) on Ubuntu 24.04

## Overview

This document describes the process used to build PyTorch from source with native support for NVIDIA Blackwell GPUs (`sm_120`), ahead of official binary releases.

This setup was developed as part of the *Playable Cinema* project to enable real-time GPU-based image analysis, captioning, and embedding workflows.

---

## System Configuration

* **OS**: Ubuntu 24.04
* **GPU**: NVIDIA RTX PRO 4500 (Blackwell)
* **Driver / CUDA**: CUDA 13.2 (driver-level)
* **Python**: 3.12 (venv)
* **Compiler**: GCC/G++ 13.3
* **PyTorch**: built from source (`sm_120`)

---

## Rationale

At the time of setup:

* Official PyTorch builds do **not** include support for `sm_120`
* Precompiled wheels fail with:

  * `no kernel image is available for execution on the device`
* Even nightly builds lack compiled kernels for Blackwell

→ Solution: **compile PyTorch from source with explicit architecture support**

---

## 1. System Dependencies

```bash
sudo apt update
sudo apt install -y \
  build-essential \
  cmake \
  ninja-build \
  git \
  python3-venv \
  python3-dev \
  libopenblas-dev \
  libomp-dev
```

---

## 2. Python Environment

```bash
python3 -m venv ~/venvs/playable-cinema
source ~/venvs/playable-cinema/bin/activate

pip install --upgrade pip
```

Ensure no conflicting CUDA/PyTorch packages:

```bash
pip uninstall torch torchvision torchaudio -y
pip list | grep -i nvidia   # should be empty
```

---

## 3. Compiler Alignment (Critical)

CUDA requires strict host compiler consistency.

Install GCC 13:

```bash
sudo apt install gcc-13 g++-13 libstdc++-13-dev
```

Set both compilers:

```bash
sudo update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-13 100
sudo update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-13 100

sudo update-alternatives --config gcc
sudo update-alternatives --config g++
```

Verify:

```bash
gcc --version
g++ --version
```

→ both must be **13.x**

---

## 4. Fix `cc1plus` Path (Ubuntu 24.04)

Ubuntu installs GCC internals in `/usr/libexec`, but CUDA expects `/usr/lib`.

Create symlink:

```bash
sudo mkdir -p /usr/lib/gcc/x86_64-linux-gnu/13

sudo ln -s /usr/libexec/gcc/x86_64-linux-gnu/13/cc1plus \
/usr/lib/gcc/x86_64-linux-gnu/13/cc1plus
```

---

## 5. PyTorch Source

```bash
git clone https://github.com/pytorch/pytorch
cd pytorch
git submodule update --init --recursive
```

---

## 6. Python Build Dependencies

```bash
pip install -r requirements.txt
```

---

## 7. Build Configuration

Set environment variables:

```bash
export TORCH_CUDA_ARCH_LIST="12.0"
export USE_CUDA=1
export USE_CUDNN=1
export MAX_JOBS=$(nproc)

export CC=/usr/bin/gcc-13
export CXX=/usr/bin/g++-13
```

---

## 8. Build PyTorch

```bash
python setup.py install
```

Expected build time: **30–120 minutes**

---

## 9. Import Path Caveat

Do **not** run Python inside the `pytorch/` source directory.

```bash
cd ~
```

Otherwise Python imports the source tree instead of compiled extensions.

---

## 10. Validation

### CUDA availability

```bash
python -c "import torch; print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0))"
```

Expected:

```text
True
NVIDIA RTX PRO 4500 Blackwell
```

---

### Kernel execution test

```bash
python - << 'EOF'
import torch, time

x = torch.randn(2000, 2000, device="cuda")
start = time.time()
y = torch.matmul(x, x)
torch.cuda.synchronize()
print("Time:", time.time() - start)
EOF
```

Expected:

* No errors
* Execution time ≈ **0.04s**
* Confirms native GPU kernel execution

---

## Key Issues Encountered

### 1. PyTorch binaries lack `sm_120`

→ requires source build

### 2. GCC mismatch (13 vs 14)

→ breaks CUDA compilation (`posix_spawnp` error)

### 3. `cc1plus` path mismatch

→ Ubuntu vs CUDA filesystem expectations

### 4. Import conflict inside repo

→ Python loads source instead of compiled extension

---

## Outcome

This setup provides:

* Native Blackwell (`sm_120`) kernel support
* Full CUDA execution without fallback
* Stable GPU compute for:

  * image captioning
  * embedding generation
  * real-time inference pipelines

---

## Notes

* This environment is **ahead of official PyTorch releases**
* Avoid overwriting with:

  ```bash
  pip install torch
  ```
* Treat this setup as a **controlled experimental baseline**

---

## Related Files

* `requirements/requirements_blackwell.txt` → Python environment snapshot
* `requirements/system_info.txt` → hardware + compiler info

---

## Summary

Building PyTorch from source with:

```bash
TORCH_CUDA_ARCH_LIST="12.0"
```

and a properly aligned GCC toolchain enables full GPU support for Blackwell hardware prior to official ecosystem support.

This configuration forms the computational baseline for GPU-driven components of the *Playable Cinema* project.
