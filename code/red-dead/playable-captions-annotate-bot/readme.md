# Bot Auto-Notation App
This is where the project got @#%$§ crazy, but also crazy cool. We were trying to figure out the best vocabulary to use to describe a scene — specifically we were asking what the best word would be for a "dynamite plunger" — and so we asked the ChatGPT app to describe the image for us. It was only a second later, when we saw the quality of the response, that we realized that we could just ask the [Chat GPT API](https://openai.com/api/) to do all the labelling for us. We just needed to make a really good system prompt.

## Run
Start with a clean `venv` (cf. below). For help: `python notation --h`

To run:
```
$ python captions.py --blip --model models/rdr2-train-robbery-bot-notation
```

## Python Environment
To create:

```
$ cd ./code/playable-bot-notation
$ pyenv virtualenv 3.11.9 playable-bot-notation
```

To activate :

```
$ pyenv activate playable-bot-notation
```

## Dependencies
```
$ pip install PyQt5
$ pip install opencv-python
$ pip install --upgrade openai
```
