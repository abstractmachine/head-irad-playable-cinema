# Playable Cinema
![Playable Cinema Research Project, IRAD, HEAD – Genève](./presskit/images/cowpoke-controller/playable-cinema-cowpoke-controller-widow.jpg)

## Project
Cowpokes riding through the ghost town of an abandoned Western. Isolated cabin, solitary frames, staring out onto the flickering plains of a mythology fading to red. A crossroads. Two mediums, revolvers drawn, caught in a deadly standoff.

[![Cowpoke Cabin - Side View](./presskit/images/cowpoke-cabin/head-irad-dead-crossing-cabin-cube-video.png)](https://youtu.be/FBftxKD_kOU)

Cf. [Dead Crossing](https://youtu.be/FBftxKD_kOU) installation (YouTube). Camera: Faust Perillaud. Edit: Douglas Edric Stanley

Playable Cinema is a research project exploring how artificial intelligence can ride the invisible frontier between cinema history and interactive gameplay. By using machine learning to analyze classical western cinema, the project constructs a generative database where fragments of film history and the streams of live gameplay bleed into one another; where the joystick becomes an editing tool for an infinite fever dream looping through the ghosts of cinematic history.

![Inside Inside, Douglas Edric Stanley, installation view](./presskit/images/inside-inside/insideinside-DouglasEdricStanley-Installation.jpg)

The project began by building a dataset that connects patterns between dystopian cinema and the award-winning indie game *Inside* (Playdead, 2016). This installation has been exibited in various locations and contexts (San Francisco, Lausanne, Marseille, Genève, …). This testbed served as a prototype for a larger-scale dataset comparing Western films and the iconic western video game, *Red Dead Redemption 2*.

![Playable Cinema Research Project, IRAD, HEAD – Genève](./presskit/images/cowpoke-controller/playable-cinema-cowpoke-controller-bar.jpg)

Dead Crossing is the latest physical manifestation of the Playable Cinema research project. Inside of an unfinished wooden cabin, visitors explore the video game world of Red Dead Redemption 2. As they move through its three-dimensional virtual terrain, AI models synchronize their movements with shots echoing from the history of Western cinema.

In this current form, the project explores how hybrid strategies can emerge new curatorial methodologies as generative tools collaborate with humans in assembling the haunted archive of our shared hallucinations of the West.

![Playable Cinema Research Project, IRAD, HEAD – Genève](./presskit/images/cowpoke-controller/playable-cinema-cowpoke-controller-cheat.jpg)

![Playable Cinema Research Project, IRAD, HEAD – Genève](./presskit/images/cowpoke-controller/playable-cinema-cowpoke-controller-drunkard.jpg)

## Team
- [Douglas Edric Stanley](https://abstractmachine.net), Project Lead, concept, design, development, production, documentation
- [Faust Perillaud](https://2024.head-geneve.show/en/projects/spectral-yard-fp-100e1), Research Assistant, training & labelling, production, documentation, photography
- [Guillaume Stagnaro](https://www.stagnaro.net), [Cowpoke Controller](./hardware/cowpoke-controller/) development
- [Colin Castellano](https://ebenisterie-castellano.ch), Wood construction consulting & production

## Software
A [training and playback tool](./code/crossing-tool/readme.md) is currently in development.

![Crossing Tool](./code/crossing-tool/documentation/images/visualizers/visualizer-shotlist.png)

## Demo
You can watch a short excerpt of the software synchronizing in real-time images from gameplay and shots from historical western cinema:

[![](./code/archive/playable-tool/images/head-irad-playable-cinema-sync-test-train-robbery.png)](https://youtu.be/-g9P9GaXHlI)
[Sync Demo - Train Robbery](https://youtu.be/-g9P9GaXHlI) ([https://youtu.be/-g9P9GaXHlI](https://youtu.be/-g9P9GaXHlI))

[![](./code/archive/playable-tool/images/playable-cinema-dual-sync-test-carriage-entering-stables.png)](https://youtu.be/cOG3Zf-KX_0)
[Sync Demo - Carriage Entering Stables](https://youtu.be/cOG3Zf-KX_0) ([https://youtu.be/cOG3Zf-KX_0](https://youtu.be/cOG3Zf-KX_0))

## Cabin
A [cabin](./hardware/cowpoke-cabin/readme.md) has been constructed that integrates all the parts of the project.

![Cowpoke Cabin - Side View](./presskit/images/cowpoke-cabin/head-irad-dead-crossing-cabin-cube.jpg)

Photo by Faust Perillaud.

Blueprints to build this cabin can be found in the repository [hardware/cowpoke-cabin](./hardware/cowpoke-cabin/blueprints/)

![Cowpoke Cabin - Isometric Named](./hardware/cowpoke-cabin/images/Cabin-Names-2025-11-20-a.png)

## Hardware
A [physical game controller](./hardware/cowpoke-controller/readme.md) is currently in developement. This allows visitors to interact with the installation.

![Cowpoke Controller render](./hardware/cowpoke-controller/images/cowpoke-controller-front-body_brass-stock_walnut-screws_brass+black.png)

CAD model files and KiCAD circuit diagram can be found in [hardware/cowpoke-controller](./hardware/cowpoke-controller/)

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
- [Valentin Dubois](https://www.hesge.ch/head/en/directory/valentin-dubois), [HEAD – Genève](https://www.hesge.ch/head/en), Head of [Materials + Prototyping Pool](https://www.hesge.ch/head/en/studies-and-research/pools/materials-prototyping-pool)
- [Alexandre Simian](https://www.hesge.ch/head/annuaire/alexandre-simian), Technician [Wood Workshop](https://www.hesge.ch/head/en/wood-workshop), [HEAD – Genève](https://www.hesge.ch/head/en)
- [Sébastien Pitteloud](https://www.hesge.ch/head/annuaire/sebastien-pitteloud), Technician [Wood Workshop](https://www.hesge.ch/head/en/wood-workshop), [HEAD – Genève](https://www.hesge.ch/head/en)
- [Charles Cuccu](https://www.hesge.ch/head/annuaire/newuser5cc3f9459ff5c), Régisseur [HEAD – Genève](https://www.hesge.ch/head/en)

## Financing
This project was financed with a research grant from the [Network of Expertise in Design and Visual Arts](https://www.hesge.ch/head/en/programs-research/research) / [Réseau de compétences Design et Arts visuels](https://www.hesge.ch/head/formations-recherche/recherche).

![Playable Cinema Research Project, IRAD, HEAD – Genève](./presskit/images/cowpoke-controller/playable-cinema-cowpoke-controller-bath.jpg)