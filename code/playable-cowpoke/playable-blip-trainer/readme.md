# BLIP
We are going to work on the [BLIP]() trainer + playback code. This folder discusses the BLIP trainer that will also interface with other models to prepare the dataset for the training.

## Dataset
For now we are starting with solely [Breakheart Pass](Breakheart-Pass(1975){tmdb-8043}.mp4) (1975) + a let's play full run-through of the first main train robbery scene from Red Dead Redemption 2. We will begin by learning how well the [BLIP](https://huggingface.co/docs/transformers/en/model_doc/blip) detects the images.

## Training


## Anotation
We will be using [Ollama](https://ollama.com) to run [Gemma 3:27b](https://huggingface.co/google/gemma-3-27b-pt) locally on our computer.

## Ubuntu Installation
We are working with an Ubuntu machine with an NVidia GeForce RTX 4070 Ti 12Gb and CUDA v12.9.

### Pyenv
```
# 1. pyenv install 3.11.9
# 2. pyenv virtualenv 3.11.9 playable-blip-trainer
# 3. pyenv activate playable-blip-trainer
# 4. pip install -r requirements.txt
```

With all this we tested the app help menu:

```
$ python app.py
```

Which worked, but no Ollama access yet.

### Ollama
Install `Ollama`

```
curl -fsSL https://ollama.com/install.sh | sh
```

Run service:

```
$ sudo systemctl enable --now ollama
$ sudo systemctl status ollama
```

Pull model:

```
$ ollama pull gemma3:27b
```
