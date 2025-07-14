# Blip Trainer
This takes the `/Dataset` folder from the `../playable-annotate/annotate.py` script, creates a Dataset of `jpg` + `txt` entries, and starts training using the `Blip-2` model.

## Pyenv
```
> cd ./code/playable-trainer-blip
> pyenv virtualenv 3.11.9 playable-trainer-blip
```

To activate :

```
> pyenv activate playable-trainer-blip
```

## Dependencies
```
$ pyenv virtualenv 3.11.9 playable-caption
$ pyenv activate playable-caption
$ pip install opencv-python
```

