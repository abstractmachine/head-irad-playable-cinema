# BLIP
We're going to start a quick and dirty prototype to test the BLIP models and then start training our own model for live inferencing.

## Quick Start

### Installation

If you are on macOS, make sure you first have [Homebrew](http://brew.sh) installed and from homebrew, `pyenv` should be installed (`brew install pyenv` & `brew install pyenv-virtualenv`). On Windows, wedonno how all this works, sorry :-(

1. Create Python Environment
```
% cd ~/your-folder-path-to/playable-blip
pyenv install 3.11.9
pyenv virtualenv 3.11.9 playable-blip
pyenv activate playable-blip
```

2. Install requirements
```
% pip install --upgrade -r requirements.txt
```

## Results
I've created script to load a model from Huggingface (`model-downloader.py`) and another to test the downloaded blip model using a definable folder and movie file (`blip.py`).

Here is the output of an Ubuntu + NVidia GeForece 4070 Ti 12Gb: [output-ubuntu-nvidia-geforce-rtx-4070-Ti-12Gb-2025-10-03-17-40-00.txt](./output-ubuntu-nvidia-geforce-rtx-4070-Ti-12Gb-2025-10-03-17-40-00.txt)

```
--- Inference timing ---
Samples timed: 2256
Avg latency per caption: 0.072s
```


Here is output of a M1-Macbook-Pro-Max-64GB: [output-mac-m1-max-64Gb-2025-10-01-18-11-00.txt](./output-mac-m1-max-64Gb-2025-10-01-18-11-00.txt)

And the timing scores:

```
--- Inference timing ---
Samples timed: 41
Avg latency per caption: 0.420s
```

And the second results (M4) : [output-mac-m4-24Gb-2025-10-03-16-18-00.txt](./output-mac-m4-24Gb-2025-10-03-16-18-00.txt)

```
--- Inference timing ---
Samples timed: 101
Avg latency per caption: 0.298s
```

![Blip test results](./images/Screenshot-2025-10-01-18.19.36.png)
![Blip test preview](./images/Screenshot-2025-10-01-18.19.28.png)
