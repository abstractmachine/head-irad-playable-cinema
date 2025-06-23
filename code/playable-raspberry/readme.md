# Eduroam
After lots of complicated hand-wringing and weird chatbot suggestions, it turns out connecting a Raspberry to HES-SO's Eduroam is quite easy:

1. Connect on Raspberry to a temporary phone (sharing) or over Wired ethernet
2. Go to [cat.eduroam.org](http://cat.eduroam.org)
3. Select `Connect your device to eduroam®` > `Click here to download your eduroam installer`
4. Select `HES-SO`
5. We don't want the default `Chrome` installer, we want a Linux installer:
	a. Select `Choose another installer to download`
	b. Select `Linux`
	c. Now click on the previous button, now listed as `eduroam®` with the Linux penguin
	d. `Continue`
6. This creates a `python` script. Open the terminal and navigate to the `Downloads` folder
	a. `cd ~/Downloads/`
	b. Run the pythons script: `python eduroam-linux-HES-SO-eduroam.py`
	c. Enter login + password (twice)
7. All done (hopefully)