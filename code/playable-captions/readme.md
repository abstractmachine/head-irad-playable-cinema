# Captions
Our colleague [Vytas Jankauskas](https://vjnks.com) suggested we explore an AI captioning system such as [SmallCap](https://github.com/RitaRamo/smallcap?tab=readme-ov-file) that has been trained on the [Coco Dataset](http://cocodataset.org) to generate captions — or image descriptions — for images. He smartly suggested that we could create a database of captions for our images and find similar captions in the real-time gameplay of [Red Dead Redemption](https://en.wikipedia.org/wiki/Red_Dead_Redemption_2). Smart idea. We will definitely try this.

## Pyenv
```
> cd ./code/playable-playback
> pyenv virtualenv 3.11.9 playable-caption
```

To activate :

```
> pyenv activate playable-caption
```

## Dependencies
```
$ pyenv virtualenv 3.11.9 playable-caption
$ pyenv activate playable-caption
$ pip install opencv-python
$ pip install Pillow
$ pip install PyQt5
$ pip install torch transformers accelerate
```
## JoyCaption
Faust found what looks like a cool captioning project: [JoyCaption](https://github.com/fpgaminer/joycaption?tab=readme-ov-file) (github), (cf. [Huggingface model](https://huggingface.co/fancyfeast/llama-joycaption-beta-one-hf-llava)).