# Raspberry Pi
These are various notes on configuring a Raspberry Pi for inferencing (i.e. interacting with an already-trained model).

## Eduroam
After lots of complicated hand-wringing and weird chatbot suggestions, it turns out connecting a Raspberry to HES-SO's Eduroam is quite easy:

1. Connect on Raspberry to a temporary phone (sharing) or over Wired ethernet
2. Go to [cat.eduroam.org](http://cat.eduroam.org)
3. Select `Connect your device to eduroam®` > `Click here to download your eduroam installer`
4. Select `HES-SO`
5. We don't want the default `Chrome` installer, we want a Linux installer:
	- Select `Choose another installer to download`
	- Select `Linux`
	- Now click on the previous button, now listed as `eduroam®` with the Linux penguin
	- `Continue`
6. This creates a `python` script. Open the terminal and navigate to the `Downloads` folder
	- `cd ~/Downloads/`
	- Run the pythons script: `python eduroam-linux-HES-SO-eduroam.py`
	- Enter login + password (twice)
7. All done (hopefully)

## Pyenv
We are going to use specific `python` environements. So to install `pyenv` I'm following the pyenv installation tutorial [Install Multiple Versions of Python on your Raspberry Pi](https://samwestby.com/tutorials/rpi-pyenv) by Sam Westby.

Here's the full list of dependencies that we need to install:

```
sudo apt-get install --yes libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev llvm libncurses5-dev libncursesw5-dev xz-utils tk-dev libgdbm-dev lzma lzma-dev tcl-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev wget curl make build-essential openssl
```

After those libraries, follow the tutorial to install and activate `pyenv`.

Then we can install the version we want of `python`:

## Example
Now that we have `pyenv` on our Raspberry Pi, we can start doing this. The following is for example taken from the [playback readme](../playable-playback/readme.md)

```
$ pyenv install 3.11.9
$ pyenv virtualenv 3.11.9 playable-playback
$ pyenv activate playable-playback
$ pip install PyQt5 ultralytics opencv-python
$ ./pythong playback.py
```
