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
There are various detection alogorithms. Cf. [PySceneDetect Detectors Docs](https://www.scenedetect.com/docs/latest/cli.html#detectors). Each of these methods has its own list of default and adjustable options. For example, `detect-adaptive` has a `-t` (`threshold`) option that defaults to `3.0`, whereas `detect-content` has a `-t` threshold option that defaults to `27.0`. `-w` uses four values, for example `-w 1.0 1.0 1.0 0.0`. We can enter any, all, or none of these options next to the `Detect` button.

![Detection method options field](detection-method-options.png)

#### [detect-adaptive](https://www.scenedetect.com/docs/latest/cli.html#detect-adaptive)
Options:
- adaptive_threshold (`-t`, `--threshold`)
    - The threshold for triggering a cut (float).
- min_content_val (`-c`, `--min-content-val`)
    - Minimum content value to trigger a cut (float).
- frame_window (`-f`, `--frame-window`)
    - Size of the rolling window (int).
- weights (`-w`, `--weights`)
    - Tuple of 4 floats: (delta_hue, delta_sat, delta_lum, delta_edges).
- luma_only (`-l`, `--luma-only`)
    - Boolean flag to use only luma channel.
- kernel_size (`-k`, `--kernel-size`)
    - Size of kernel for edge detection (int).
- min_scene_len (`-m`, `--min-scene-len`)
    - Minimum scene length (int, float, or timecode string).

Examples:

- `-t 2.5` (sets adaptive_threshold)
- `-c 16.0` (sets min_content_val)
- `-f 4` (sets frame_window)
- `-w 1.0 1.0 1.0 0.0` (sets weights)
- `-l` (sets luma_only to True)
- `-k 5` (sets kernel_size)
- `-m 100` (sets min_scene_len to 100 frames)
- `-m 3.5s` (sets min_scene_len to 3.5 seconds)
- `-m 00:01:52.778` (sets min_scene_len to a timecode)

#### [detect-content](https://www.scenedetect.com/docs/latest/cli.html#detect-content)
Options:
- threshold (`-t`, `--threshold`)
- weights (`-w`, `--weights`)
- luma_only (`-l`, `--luma-only`)
- kernel_size (`-k`, `--kernel-size`)
- min_scene_len (`-m`, `--min-scene-len`)
- frame_window (`-f`, `--frame-window`)

#### [detect-hash](https://www.scenedetect.com/docs/latest/cli.html#detect-hash)
Not yet implemented

#### [detect-hist](https://www.scenedetect.com/docs/latest/cli.html#detect-hist)
Options:
- threshold (`-t`, `--threshold`)
- bins (`-b`, `--bins`)
- min_scene_len (`-m`, `--min-scene-len`)

#### [detect-threshold](https://www.scenedetect.com/docs/latest/cli.html#detect-threshold)
Options:
- threshold (`-t`, `--threshold`)
- fade_bias (`--fade-bias`)
- add_last_scene (`--add-last-scene`)
- min_scene_len (`-m`, `--min-scene-len`)

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
