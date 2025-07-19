# Playable Cinema Multi-Tool
This is a tool that combines previous work (`playable-captions-annotate-bot` & `playable-captions-playback`) into a single multi-tool for *annotating* (a.k.a. BLIP *captioning*), *playback* (a.k.a. *inferencing*) captions, and various other uses such as identifying scene and movement changes.

## Run
Start with a clean `venv` (cf. above).

To run:
```
$ pyenv activate playable-tool
$ python app.py
```

## PySceneDetect
This tool leans heavily on [PySceneDetect](https://www.scenedetect.com) for identification of scenes and shots.

### Detectors
There are various detection alogorithms. Cf. [PySceneDetect Detectors Docs](https://www.scenedetect.com/docs/latest/cli.html#detectors):

- [detect-adaptive](https://www.scenedetect.com/docs/latest/cli.html#detect-adaptive)
- [detect-content](https://www.scenedetect.com/docs/latest/cli.html#detect-content)
- [detect-hash](https://www.scenedetect.com/docs/latest/cli.html#detect-hash)
- [detect-hist](https://www.scenedetect.com/docs/latest/cli.html#detect-hist)
- [detect-threshold](https://www.scenedetect.com/docs/latest/cli.html#detect-threshold)

Each of these methods has its own list of default and adjustable options. For example, `detect-adaptive` has a `-t` (`threshold`) option that defaults to `3.0`, whereas `detect-content` has a `-t` threshold option that defaults to `27.0`. `-w` uses four values, for example `-w 1.0 1.0 1.0 0.0`. We can enter any, all, or none of these options next to the `Detect` button.

See above links for more info on each method's options.

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
