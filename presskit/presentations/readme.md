# Slideshow
While we've started with a simple [Google Slides template](./istituto-svizzero/head-irad-playable-cinema-istituto-svizzero.gslides), the goal is to quickly move to a quicker and more open-source solution, using [Markdown](https://en.wikipedia.org/wiki/Markdown) + [MARP](https://marp.app). This workflow has the added bonus of being the fastest way we know to write and generate slides and PDFs. We will use the provided Google Slide template as our design guide.

## MARP
The Markdown Presentation Ecosystem converts [Markdown](https://en.wikipedia.org/wiki/Markdown) text into HTML, PDF, and Powerpoint presentations.

We are using [MARP for VSCode](https://marketplace.visualstudio.com/items?itemName=marp-team.marp-vscode) to convert our markdown into the desired formats.

## Theme
We have created our own CSS theme for the presentation/document that can be found here [cowpoke.css](./istituto-svizzero/cowpoke.css).

## Command Line Interface
We are using the [Marp CLI]() to generate our document/slides.

### Install
To install Marp on `macOS` via [Homebrew](https://brew.sh):

```
$ brew install marp-cli
```

## Create Slideshow
Using the command:

```
% marp --theme cowpoke.css slideshow.md
```

This creates the `slideshow.html` file that we run with [Live Server](https://www.google.com/search?client=safari&rls=en&q=live+server&ie=UTF-8&oe=UTF-8) from [VS Code](https://code.visualstudio.com), using Chrome as our web browser.

## Image Format
For images to be fullscreen, use this syntax in Markdown:

```
![bg Dead](./images/name-of-image.jpg)
```

This puts the image in the background layer and allows us to have text on top.

It is highly recommended to crop the image in external software (Preview, Photoshop, …) in a 16:9 ratio.