# Playable Cinema

**Playable Cinema** is an experimental research project that explores how machine learning can be used to reconfigure moving images from cinema and video games into interactive audiovisual experiences. Using computer vision techniques such as semantic segmentation and visual inference, the project began by building a dataset that connects patterns between dystopian cinema and the award-winning indie game *Inside* (Playdead, 2016). This testbed serves as a prototype for a larger-scale dataset comparing Western films and *Red Dead Redemption 2*.

## Team
- [Douglas Edric Stanley](https://abstractmachine.net), Project Lead
- [Faust Perillaud](https://2024.head-geneve.show/en/projects/spectral-yard-fp-100e1), Research Assistant, training & labelling
- [Guillaume Stagnaro](https://www.stagnaro.net), [Cowpoke Controller](./hardware/cowpoke-controller/) Developer

## Software
A [training and playback tool](./code/playable-cowpoke/playable-tool/) is currently in development.

![Playable-Cinema-Tool](./code/playable-cowpoke/playable-tool/screenshots/playable-cinema-tool-2025-09-09.png)

## Cabin
A [cabin](./hardware/cowpoke-cabin/readme.md) is currently in production that integrates all the parts of the project.

![Cowpoke Cabin - Side View](./hardware/cowpoke-cabin/images/cowpoke-cabin-2025-09-30-side-view.png)

Blueprints to build this cabin can be found in this repository at: [cowpoke-cabin](./hardware/cowpoke-cabin/blueprints/)

## Hardware
There is a [physical object](./hardware/cowpoke-controller/readme.md) is currently in developement.

![Cowpoke Controller render](./hardware/cowpoke-controller/images/cowpoke-controller-render-2025-09-15.png)

CAD model files and KiCAD circuit diagram can be found at [cowpoke-controller](./hardware/cowpoke-controller/)

## Westerns
There is a [list of the western films](./cineclub/README.md) we are using to train the deep learning models of this project.

## Current Status & Timeline
- **Test Phase (Q2 2025):** Annotating sequences from *Inside* using Roboflow and SAM tools.
- **Training Prototype Model (Q3 2025):** Building initial inference pipeline and testing labeling workflow.
- **Scaling Up (Q3 2025):** Transitioning to a larger dataset involving Western films and *Red Dead Redemption 2*.
- **Deployment (Q4 2025):** Developing interactive installation, annotation tool and real-time inference API.

## External Resources
- [Project overview & context](https://abstractmachine.net/en/posts/inside-inside)  
- [Teaser video (Inside Inside)](https://vimeo.com/589844238)  
- [GIFF Festival installation (2021)](https://www.giff.ch/archives/2021/)

## HEAD – Genève
- [Anthony Masure](https://www.anthonymasure.com), Dean of Research, [IRAD](https://www.hesge.ch/head/en/programs-research/research), [HEAD – Genève](https://www.hesge.ch/head/en), [HES-SO](https://www.hes-so.ch/)
- [Christelle Granite-Noble](https://www.hesge.ch/head/annuaire/christelle-granite-noble), Administrative Coordination, [IRAD](https://www.hesge.ch/head/en/programs-research/research), [HEAD – Genève](https://www.hesge.ch/head/en), [HES-SO](https://www.hes-so.ch/)

## Financing
This project was financed with a research grant from the [Network of Expertise in Design and Visual Arts](https://www.hesge.ch/head/en/programs-research/research) / [Réseau de compétences Design et Arts visuels](https://www.hesge.ch/head/formations-recherche/recherche).