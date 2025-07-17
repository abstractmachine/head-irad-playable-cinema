# Playable Cinema Multi-Tool
This is a tool that combines previous work (`playable-captions-annotate-bot` & `playable-captions-playback`) into a single multi-tool for *annotating* (a.k.a. BLIP *captioning*), *playback* (a.k.a. *inferencing*) captions, and various other uses such as identifying scene and movement changes.

## Run
Start with a clean `venv` (cf. above).

To run:
```
$ pyenv activate playable-tool
$ python app.py
```

## Create Pyenv
This is how to create the appropriate `pyenv`:
```
$ cd ./code/playable-tool
$ pyenv virtualenv 3.11.9 playable-tool
```

## Requirements
This will use the `requirements.txt` file to install all the required libraries.
```
pip install -r requirements.txt
```
