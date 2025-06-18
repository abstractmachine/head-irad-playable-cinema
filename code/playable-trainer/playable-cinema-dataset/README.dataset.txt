# Playable Cinema > 2025-06-17 2:47pm
https://universe.roboflow.com/testplayablecin/playable-cinema

Provided by a Roboflow user
License: CC BY 4.0

# Playable Cinema

**Playable Cinema** is an experimental research project that explores how machine learning can be used to reconfigure moving images from cinema and video games into interactive audiovisual experiences. Using computer vision techniques such as semantic segmentation and visual inference, the project builds a dataset that connects patterns between dystopian cinema and the award-winning indie game *Inside* (Playdead, 2016). This testbed serves as a prototype for a larger-scale dataset comparing Western films and *Red Dead Redemption 2*.

---

## 📁 Class Types (example labels from *Inside*)

- `boy`
- `lamp`
- `pig`
- `tree`
- `truck`
- `mist`
- `farm-infrastructure`
- `ground`

Each object is manually segmented to match scene-level semantics and environmental context. Labeling aims to support inference of spatial composition, narrative rhythm, and visual motifs.

---

## 🔧 Current Status & Timeline

- **Test Phase (Q2 2025):** Annotating sequences from *Inside* using Roboflow and SAM tools.
- **Training Prototype Model (Q3 2025):** Building initial inference pipeline and testing labeling workflow.
- **Scaling Up (Q3 2025):** Transitioning to a larger dataset involving Western films and *Red Dead Redemption 2*.
- **Deployment (Q4 2025):** Developing interactive installation and real-time inference API.

---

## 🌍 External Resources

- [Project overview & context](https://abstractmachine.net/en/posts/inside-inside)  
- [Teaser video (Inside Inside)](https://vimeo.com/589844238)  
- [GIFF Festival installation (2021)](https://www.giff.ch/archives/2021/programme/virtualterritories/index.html)

---

## 🤝 Contribution & Labeling Guidelines

- Labels reflect both visual elements and interpretive categories (e.g. atmosphere, affordance).
- For collaboration or access to the dataset, contact the project lead via IRAD, HEAD – Genève.