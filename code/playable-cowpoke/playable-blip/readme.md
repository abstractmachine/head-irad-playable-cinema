# BLIP
We're going to start a quick and dirty prototype to test the BLIP models and then start training our own model for live inferencing.

## Quick Start

### Installation

1. Create Python Environment
```
cd ~/your-folder-path-to/playable-blip
pyenv virtualenv 3.11.9 playable-blip
pyenv activate playable-blip
```

2. Install requirements
```
% pip install --upgrade -r requirements.txt
```

## Results
I've created script to load a model from Huggingface (`model-downloader.py`) and another to test the downloaded blip model using a definable folder and movie file (`blip.py`).

Here is the file of the results: [result-2025-10-01-18-11-00.txt](./result-2025-10-01-18-11-00.txt)

![Blip test results](./images/Screenshot-2025-10-01-18.19.36.png)
![Blip test preview](./images/Screenshot-2025-10-01-18.19.28.png)