# Dead Crossing Presskit

This folder contains the source for the Dead Crossing PDF presskit.

## One-liner

A frontier cabin where visitors play through an iconic Western videogame while a machine-learning system continuously remixes more than three hundred Western films into a live cinematic remix.

## Pitch

Dead Crossing is an installation built as a frontier cabin, a custom controller, and a traversable archive of Western cinema. Visitors enter a physical structure, navigate a popular Western videogame, and trigger a real-time editing system that cross-indexes gameplay with hundreds of Western films. The result is a live remix cinema that treats the American West as a mythology made of recurring images, gestures, and icons.

## Presskit

- Presskit PDF: `./dead-crossing.pdf`
- Markdown source: `./dead-crossing.md`
- Images: `./images/`
- Stylesheets: `./styles/`

## Team

- Douglas Edric Stanley — Project Lead
- Faust Perillaud — Research Assistant, training and labelling
- Guillaume Stagnaro — Cowpoke Controller Developer

### Financing

This project was financed with a research grant from the Network of Expertise in Design and Visual Arts / Réseau de compétences Design et Arts visuels.

### HEAD – Genève

- Anthony Masure — Dean of Research, IRAD, HEAD – Genève, HES-SO
- Christelle Granite-Noble — Administrative Coordination, IRAD, HEAD – Genève, HES-SO

## Content

This presskit is built from the following files:

- `dead-crossing.md` — presskit source in Markdown
- `styles/` — CSS files for Pandoc and print layout
- `images/` — images used by the presskit
- `build.sh` — build script for generating the PDF

## Install

### Requirements

- Ubuntu 22.04 or later. Should also work on macOS. Is Windoze still a thing?
- `pandoc`
- `python3`
- `python3-venv`
- `weasyprint` and its system dependencies

### System packages

```bash
sudo apt update
sudo apt install -y pandoc python3 python3-venv python3-pip
sudo apt install -y libcairo2 libpango-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info fonts-dejavu fonts-liberation
pandoc --version
weasyprint --version
```

### Python

```bash
cd presskit
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install weasyprint
```

## Build

```bash
./build.sh
```

### Manual Build

```bash
pandoc dead-crossing.md \
  --from markdown+hard_line_breaks \
  --pdf-engine=weasyprint \
  --resource-path=.:images:styles \
  --css=styles/base.css \
  --css=styles/print.css \
  --css=styles/cover.css \
  -o dead-crossing.pdf
```
