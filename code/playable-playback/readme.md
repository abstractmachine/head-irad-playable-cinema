# Playback
## Playable Cinema

This is the playback tester that allows us to run video playthroughs downloaded from YouTube and test this gameplay recording against our models. This is basically our [inferencing](https://huggingface.co/docs/huggingface_hub/en/package_reference/inference_client) tester.

## Run
To run:

```
> cd ./code/playable-playback
> python playback.py
```

## Required Files
- `model.pt`
- `video.mp4`


The training (cf. [playable-trainer](../playable-trainer/) ) is executed either locally or on Google Colab and then placed in this folder as `model.pt`. The playback app assumes the local video file is named `video.mp4`.

## Pyenv
Currently we are using the following `pyenv` configuration :

```
> cd ./code/playable-playback
> pyenv virtualenv 3.11.9 playable-playback
```

To activate :

```
> pyenv activate playable-playback
```

## Dependencies
```
> pyenv virtualenv 3.11.9 playable-cinema
> pyenv activate playable-cinema
> pip install PyQt5 ultralytics opencv-python
```
