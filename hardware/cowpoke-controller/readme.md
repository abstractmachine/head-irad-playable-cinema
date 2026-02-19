# Controller
Joystick, gamepad, joypad, stick, controller, call it whatever you want: we are building a custom controller for this project.

<img alt="Cowpoke Controller 2025-11-02" src="./images/cowpoke-controller-front-body_brass-stock_walnut-screws_brass+black.png" height="300vw" />

This Cowpoke Controller is a physically modified PS4 controller designed to keep the player locked into Red Dead Redemption 2 while providing a tactile, diegetic way to move between curated chapter saves via the central barrel.

## Design
After many failed [experiments](./experiments/) by Douglas, Faust proposed this drawing which placed us on the right path of exploring the shape of [Derringer](https://en.wikipedia.org/wiki/Derringer) pistols. The previous failed experiments had been based on the [Colt](https://en.wikipedia.org/wiki/Colt_Single_Action_Army) family of pistols along with all it's bretheren.

<img alt="Cowpoke Controller drawing - Faust Perillaud" src="./images/faust-drawing.png" height="320" /> <img alt="Cowpoke Controller drawing - Faust Perillaud" src="./images/faust-drawing-isolated.png" height="320" />

## Early Mockup
<img alt="Cowpoke Controller Mockup" src="./images/Cowpoke-Controller-2025-09-12-a.jpg" height="320" />

## Layers
<img alt="Cowpoke Controller metal frame" src="./images/head-irad-cowpoke-controller-0353.png" height="240" /> <img alt="Cowpoke Controller layers" src="./images/cowpoke-controller-no-frame-front.png" height="240" /> <img alt="Cowpoke Controller layers" src="./images/cowpoke-controller-no-frame-layers.png" height="240" />


<img alt="Cowpoke Controller circuit design" src="./images/cowpoke-controller-0CB1ED5798D6-1.jpg" height="320" />

## User tests
Quick animation of hand movement test:

[![Cowpoke Controller user tests - 2025-09-16](./images/cowpoke-controller-user-tests-9243.gif)](https://youtu.be/peajP1k5_gU)

Cf. https://youtu.be/peajP1k5_gU

Excerpts of other hand tests:

<img alt="Cowpoke Controller user tests - 2025-09-15" height="320" src="./images/head-irad-cowpoke-controller-9206.png" /> <img alt="Cowpoke Controller user tests - 2025-09-16" height="320" src="./images/cowpoke-controller-user-tests-9264.jpg" /> <img alt="Cowpoke Controller user tests - 2025-09-16" height="320" src="./images/cowpoke-controller-user-tests-9227.jpg" /> <img alt="Cowpoke Controller user tests - 2025-09-16" height="320" src="./images/cowpoke-controller-user-tests-9261.jpg" />

Based on these tests, we are advancing with the 1.1x size, i.e. starting with the general outline of the Derringer frame, but multiplied 1.1x.

I guess we can call this the Douglas-measurement-unit, or, *en français*, *le Douglas-étalon* :


<img src="./images/head-irad-cowpoke-controller-9204.png" height="320" /> <img src="./images/head-irad-cowpoke-controller-9205.png" height="320" />

## Production
Here are some images of the latest production runs (circ. January 2026), as well as the latest Rhino-model before being sent out for CNC machining:

### Test Run
<img alt="Cowpoke Controller Test Production" height="240" src="./images/cowpoke-controller-signal-2026-01-12.gif" />
<img alt="Cowpoke Controller Test Production" height="240" src="./images/cowpoke-controller-0243.gif" />
<img alt="Cowpoke Controller Test Production" height="240" src="./images/cowpoke-controller-0247.gif" />
<img alt="Cowpoke Controller Test Production" height="240" src="./images/cowpoke-controller-0248.gif" />
<img alt="Cowpoke Controller Test Production" height="240" src="./images/cowpoke-controller-production-test-run-tooling-marks.jpg" />

### Circuit
<img src="./images/cowpoke-controller-circuit-2026-01-12-131653.jpg" height="240" />
<img src="./images/cowpoke-controller-circuit.jpg" height="240" />

<img alt="Cowpoke Controller internals Front - 2026-02-17" height="240" src="./images/cowpoke-controller-2026-02-17-gs-front.png" />
<img alt="Cowpoke Controller internals Front - 2026-02-17" height="240" src="./images/cowpoke-controller-deringer-2026-02-17-gs-snapshot.png" />

## Material Choices
As production advances to the final stages, we had to make a choice on the final materials. Since the only workable solution we've found (so far) for producing the thumbsticks is via some sort of FDM or SLA method, we have landed so far on black. Meaning we now have three materials/color choices: brass (base) + walnut (handles) + black. The black is for both the thumbsticks and the four black screws on the front faceplate. The other two screws on the handles are brass, creating a sort of materials negative effect.

Here again is an image the selected materials :

<img alt="Cowpoke Controller - brass body - walnut handles - brass + black screws" src="./images/cowpoke-controller-diagonal-body_brass-stock_walnut-screws_brass+black-buttons_brass.png" height="320" />

We did explore pseudo-ivory solutions, inspired by the historical [Remington Model 95](https://en.wikipedia.org/wiki/Remington_Model_95).

![Double Derringer](./images/double-derringer.jpg)

In our A/B testing, everyone preferred — of course! — renders mimicing this iconic object. But ultimately we went with materials that are more coherent with the overall installation (cf. [Cowpoke Cabin](../cowpoke-cabin/)). Here's the design grid Douglas produced to explore all the materials choices:

![Cowpoke Controller Design Grid - ABS vs Walnut](images/cowpoke-controller-design-grid-front-abs-vs-walnut.png)
![Cowpoke Controller Design Grid - Materials](images/cowpoke-controller-design-grid-diagonals.png)

### Ivory-replacement
For future reference: it turns out there are materials that have been designed to more ethically reproduce the look, feel & function of ivory: cf. [elforyn.de](https://www.elforyn.de/en/products/elforyn/)

## Thumb Piece
There is a historical "thumb piece" on the handle of Colt + Derringer pistols that we are mimicing because it add not only the historical vernacular, but also some elegant historical solutions (ergonomics + manufacturing).

<img alt="Cowpoke Controller user tests - 2025-09-16" height="240" src="./images/cowpoke-controller-user-tests-0352.jpg" />
<img alt="Cowpoke Controller user tests - 2025-09-16" height="240" src="./images/cowpoke-controller-user-tests-0399.PNG" />
<img alt="Cowpoke Controller user tests - 2025-09-16" height="240" src="./images/cowpoke-controller-user-tests-9248.jpg" />

## GP2040-CE
Thanks to a suggestion by [ChatGPT 4o](https://openai.com/index/hello-gpt-4o/), we will start with the [GP2040-CE](https://gp2040-ce.info), a [Raspberry Pi](https://www.raspberrypi.com)-based project from [OpenStickCommunity](https://github.com/OpenStickCommunity). This project was designed to convert a Raspberry Pi into a low-latency USB controller. According to the [GP2040-CE Gitbub page](https://github.com/OpenStickCommunity/GP2040-CE), it can already emulate PS4 and PS5 controllers.

## USB Adapter
Originally, we thought we would be using the GP2040 in standard HID PC Joystick mode, which required using a [Brook Wingman FGC2 USB](https://www.amazon.de/Brook-FGC2-Controller-Adapter-kompatibel-PS5-Spielen-Schwarz-Wei%C3%9F/dp/B0DLWDY7MK) adaptor to emulate a PS4 controlller. But it turns out it is fairly easy to directly emulate a PS4 controller using the [OpenStickCommunity](https://github.com/OpenStickCommunity) solutions. Using a GP2040 allows both options. As we near the end of production, so far the PS4 emulation seems to be the final choice.

## Process
[Guillaume Stagnaro](https://www.stagnaro.net) is finishing development on the controller circuit + final 3D CAD model. We are using [KiCAD](https://www.kicad.org) for the PCB development, and [JLC-PCB](https://jlcpcb.com/) + [LCSC](https://www.lcsc.com) for the circuit board production, including their pick-in-place solutions to order as well as place the entire circuit components on the board in one integrated solution. Guillaume is using [easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py) to synchronize his work in KiCAD with the massive library of components at [LCSC](https://www.lcsc.com).

<img alt="Guillaume Stagnaro @ Pool numérique, HEAD – Genève" src="./images/head-irad-cowpoke-controller-9232.png">
<img alt="Guillaume Stagnaro @ Pool numérique, HEAD – Genève" src="./images/head-irad-cowpoke-controller-9228.png">

We will also use their [JLC-3DP](https://jlc3dp.com) service to cutout the physical controller frame in ~~stainless steel~~ brass (frame) and solid walnut wood (stock handles). Guillaume is using a hybrid KiCAD + [Rhino3D](https://www.rhino3d.com) solution to integrate these two layers (circuit design + object design). Douglas is using [Shapr3D](https://www.shapr3d.com) based on Guillaume's [STEP](https://en.wikipedia.org/wiki/ISO_10303-21) files generated in Rhino3D, and constant test printing using [PrusaSlicer](https://www.prusa3d.com/page/prusaslicer_424/) and a [Prusa MK4S](https://www.prusa3d.com) / [Prusa One](https://www.prusa3d.com/product/prusa-core-one-kit).

## H59 Brass
There are several brass alloys, designed to avoid corrosion. We will be using the [H59](https://jlccnc.com/help/article/Brass-H59-CNC-Machining) ratio of 59% copper. H62 just looks too pink to work for this project (cf. [H59 vs H62](https://www.machiningminghe.com/difference-between-h59-and-h62-brass/)).

# CAD Models
Although we have successfully used the mockups to make our choices concerning dimensions and fundamental shapes, there is still a lot of fine tuning required with the KiCAD circuit diagram and component selection and layout. Here is a folder with some of the latest mockups used to make our design decisions: [Cowpoke Controller models](./models/).

Here is the latest downloadable [STEP](https://en.wikipedia.org/wiki/ISO_10303-21) model file: [models/cowpoke-controller-2026-02-17-gs.stp](./models/cowpoke-controller-2026-02-17-gs.stp)

## Production Problems
We have encountered an interesting snag in production of this controller. There is a certain ambiguity as to the functional use of this controller and whether or not it falls into the category of *“controlled products”*. Cf:

[<img alt="Controlled Substances category" src="./images/cnc-controlled-product.png" height="480" />](./images/cnc-controlled-product.png) [<img alt="Controlled Substances category" src="./images/cnc-controlled-products-reply.png" height="480" />](./images/cnc-controlled-products-reply.png)

## Circuit
Currently the circuit has the following components:

- USB-C connector
- 2 x Thumbpads
- ~~Audio amplifier~~
- ~~Audio speaker~~
- Rotary encoder
- 12 buttons
    - 4 x North, South, East, West side buttons
    - 4 x Triangle, Circle, Square, Cross
    - 4 x Triggers (R1, R2, L1, L2)
- ~~[Foster Vibration Actuator](https://www.foster-electric.com/products/vibration_actuator/)~~
- 4 x Magnets (for trigger auto-recentering)
- {TBD…}

## Original Brief

Key Features:

- Controls
    - Normal PS4 Button/Joystick commands
    - Restricted Controls
        - All standard navigation or “exit game” buttons are disabled/removed.
        - Prevents leaving the RDR2 environment or entering unrelated PS4 menus.
    - Rotating Central Barrel
        - A sculptural element inspired by the cylinder of a Colt revolver.
        - The operator “spins the barrel” to jump between cinematic events, creating dynamic re-cuts of RDR2
        - Rotating the barrel triggers the specific sequence of button presses needed to:
            - Open RDR2’s Load Game menu.
            - Navigate through a list of pre-saved game moments.
            - Confirm selection with any button press, except cancel (exit menu selection)
        - Curated Save Slots
            - Each slot represents a “cinematic moment” (action scene, landscape, encounter).
            - Acts as a filmic shot library, letting the operator quickly switch contexts during a live montage or performance

## Early Prototypes
These were all mostly-failed attempts at trying to better understand how a Colt is assembled. Interesting stuff, but nothing conclusive. These files can be found in the git repository under models. They are included here to show the direction of this part of the project, but not its final form.

(TODO: Add archive images)