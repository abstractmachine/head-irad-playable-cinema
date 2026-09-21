"""Written findings for the tmdb_95864 review analysis.

Kept separate from the statistics so the prose is reviewable on its own. The
numbers are recomputed here from the same functions that write the JSON, never
retyped.
"""

from __future__ import annotations

from pathlib import Path

HYPOTHESES = {
    "note": (
        "Induced from the reviewed corpus, not from a prior taxonomy. Each is "
        "labelled with how well this one film supports it. None of these should "
        "be turned into a generator rule before the blind second film."
    ),
    "supported_repeatedly": [
        {
            "id": "H1",
            "claim": "Figure and background are two different decisions, not one palette decision.",
            "evidence": [
                "The figure was hand-picked on 51.3% of frames, the background on 27.8%.",
                "When exactly one role was replaced it was the figure 230 times and the background 30 times, a 7.7:1 ratio.",
                "Hand corrections are larger for the figure (median dE 14.4) than for the background (median dE 10.9).",
            ],
            "reading": (
                "The pipeline's account of the field the frame is set against is "
                "far more often acceptable than its account of what the frame is "
                "about. The background is close to solved; the figure is not."
            ),
        },
        {
            "id": "H2",
            "claim": "A whole generated palette is accepted when the pair is 'a person against their surroundings'.",
            "evidence": [
                "Accepted DIRECT/FIELD proposals are dominated by a face, skin, costume or silhouette as the distinguished material, paired with sand, sky, landscape or a dim interior as the counterfield.",
                "377 frames were accepted whole, 44.2% of the film.",
            ],
            "reading": (
                "The chain's strongest case is the genre's most conventional "
                "figure/ground arrangement. This is also the arrangement the "
                "operationalizer can localize."
            ),
        },
        {
            "id": "H3",
            "claim": "Most hand corrections are re-measurements of a material the system had already identified, not new interpretations.",
            "evidence": [
                "Figure: 157 of 436 hand picks are within dE 10 of a generated colour; background: 114 of 236.",
                "Only 137 figure picks and 55 background picks are beyond dE 20 from everything generated.",
            ],
            "reading": (
                "The modal correction is chromatic, not categorical. The system "
                "often names the right thing and reports the wrong colour for it."
            ),
        },
    ],
    "tested_and_not_supported": [
        {
            "id": "H4",
            "claim": "The reviewer systematically inverts the system's figure/background assignment.",
            "result": "Not supported.",
            "evidence": [
                "On fully hand-made palettes the nearest generated pair is in the swapped orientation 42.2% of the time, and for the control quadrant specifically 100 swapped vs 106 not.",
                "The test is weak in any case: these are the frames where both generated colours were rejected, so the nearest match is usually far away in either orientation.",
            ],
            "reading": (
                "Close to chance. The right statement is not that the reviewer "
                "inverts, but that the generated role labels carry little "
                "information about which colour he will treat as the figure."
            ),
        },
        {
            "id": "H5",
            "claim": "Superimposed credits are read as the narrative figure and have to be overridden.",
            "result": "Real but negligible in this film.",
            "evidence": [
                "Only 6 of 852 frames have narratology naming text/credits as what stands out.",
                "In all 6 the reviewer took the control palette for the figure, never a narratological one.",
            ],
            "reading": (
                "Observed in the title sequence and correctly handled by the "
                "reviewer, but far too rare here to justify a generator change. "
                "Worth watching on a film with a longer credit sequence."
            ),
        },
        {
            "id": "H6",
            "claim": "Segmentation quality is the main thing standing between the system and agreement.",
            "result": "Not supported as the main cause.",
            "evidence": [
                "A cleanly segmented role was kept 38.0% of the time; multiple_masks 28.4%; no_mask 27.4%.",
                "Even when SAM reported success, the reviewer replaced that role 62% of the time.",
            ],
            "reading": (
                "Clean segmentation helps by roughly ten points and no more. "
                "Fixing SAM would not fix this. The dominant failure is upstream "
                "of the mask, in which material is nominated."
            ),
        },
    ],
    "unresolved_contradictions": [
        {
            "id": "C1",
            "observation": (
                "The reviewer sometimes chooses two colours that barely separate "
                "(near-black on near-black in very dark shots), and sometimes "
                "chooses a locally tiny but bright material as the figure."
            ),
            "why_it_matters": (
                "These cannot both follow from a single rule about contrast or "
                "salience. Any generator rule that maximises separation would "
                "produce the wrong answer on the dark shots; any rule that picks "
                "the most salient small element would produce the wrong answer on "
                "the shots where he preserved the gloom."
            ),
        },
        {
            "id": "C2",
            "observation": (
                "Narratology often names a small decisive detail (a single eye, a "
                "faint vertical line) as what stands out. The reviewer sometimes "
                "honours that and sometimes ignores it in favour of a materially "
                "substantial region."
            ),
            "why_it_matters": (
                "'What stands out' and 'what should carry the palette' are not the "
                "same question, and the current chain treats them as if they were."
            ),
        },
    ],
    "overfitting_risks": [
        "This is one Italian Western with a narrow palette: sand, sky, skin, dark interiors. Rules tuned to that will not transfer.",
        "The control quadrant was available on 99.9% of frames while DIRECT was available on 21.1%. Any comparison of strategies that ignores availability will flatter the control.",
        "207 fully manual frames mean the reviewer was working without a usable proposal; their statistics describe his unaided practice, not his judgement of the system.",
    ],
}


OBSERVATIONS = """# What Douglas actually did — tmdb_95864

*$10,000 for a Massacre* (1967), 852 shots, every one reviewed.
Analysis only. The review record was not modified.

## 0. Before anything: what the evidence can and cannot support

The review ran 15–20 September and straddled two changes to the store.

| cohort | frames | meaning |
|---|---|---|
| `pre_split` | 3 | reviewed before split validation existed |
| `pre_supersedes` | 257 | split existed; a hand pick recorded no link to what it displaced |
| `full` | 592 | every provenance field available |

So the `supersedes` link exists for 287 of 674 hand picks. **Its absence never
means a pick displaced nothing**, and no finding below depends on it. Every
distance is computed against the frozen proposals, which are present for all
852 frames.

One frame (`@f041844-f041964`) has no proposals at all and was built by hand.
One frame carries a rejected answer that was later superseded. One frame from
15 September carries an accepted answer *and* manual roles, a combination the
store can no longer produce; it predates the fix.

## 1. The census

| decision shape | frames |
|---|---|
| accepted a whole generated palette | 377 |
| replaced **only the figure** | 230 |
| built both colours by hand | 207 |
| replaced **only the background** | 30 |
| figure and background from **different proposals** | 8 |

The split-validation feature was almost never used the way it was designed.
Only 8 frames combine two generated hypotheses. The other 260 splits are
"keep one generated half, pipette the other" — and overwhelmingly the half
kept is the background.

## 2. Figure and background are not the same decision

| role | hand-picked | control | field | direct | alternative |
|---|---|---|---|---|---|
| figure | **51.3%** | 26.6% | 16.5% | 5.5% | 0% |
| background | 27.8% | 42.5% | 22.9% | 6.8% | 0% |

The figure is hand-picked at nearly twice the rate of the background, and when
only one role is replaced it is the figure by 7.7 to 1. Hand corrections are
also larger for the figure (median dE 14.4) than the background (10.9).

**The pipeline's account of the field is far more often acceptable than its
account of the subject.**

## 3. The strategy shares are mostly an availability artefact

| choice | selectable | figure take-up | background take-up |
|---|---|---|---|
| 1 DIRECT | 21.1% | 26.1% | 32.2% |
| 2 FIELD | 57.5% | 28.8% | 39.8% |
| 3 ALTERNATIVE | **0.0%** | — | — |
| 4 CONTROL | 99.9% | 26.7% | 42.5% |

Read naively, the deterministic control looks like the winner. Conditional on
being offered, the three are taken at almost identical rates for the figure
(26–29%), and the control leads only mildly for the background.

Two findings matter more than the ranking:

- **DIRECT was offerable on only 21% of frames.** In 623 frames carrier-v1
  did not return two carrier sides at all, so choice 1 never reached the
  reviewer. FIELD lost a further 182 frames to converging on DIRECT's own
  materials and 135 to incomplete measurement.
- **Choice 3 was selectable on 0 of 851 frames.** narratology-v1 never
  recorded a second organizing distinction or a second "stands out" entry
  anywhere in this film, so the ALTERNATIVE slot was always the refusal
  option. As implemented, that quadrant does not exist in production.

## 4. How far the hand corrections travel

| dE to nearest generated colour | figure | background |
|---|---|---|
| 0–2 | 17 | 22 |
| 2–5 | 52 | 38 |
| 5–10 | 88 | 54 |
| 10–20 | 142 | 67 |
| 20–40 | 116 | 43 |
| 40+ | 21 | 12 |

About a third of hand picks are within dE 10 of something the system already
produced — the system named the right material and reported the wrong colour
for it. About a third are beyond dE 20 — a different material entirely.

## 5. Where the failure actually is

Segmentation status against whether the reviewer kept that role:

| segmentation | kept |
|---|---|
| success | 38.0% |
| suspicious_broad | 38.1% |
| multiple_masks | 28.4% |
| no_mask | 27.4% |

**Clean segmentation buys about ten points and no more.** Even perfectly
segmented materials were replaced 62% of the time. Repairing SAM would not
repair this.

The larger structural problem is one stage earlier: **47% of all materials
named by carrier-v1/articulation-v1 name more than one thing at once** — for
example *"the deep shadow cast by the hat across the lower half of the face and
the background sky"*. 68% of those segment as `no_mask` or `multiple_masks`,
and the union that gets measured mixes materials, so the mean is a colour that
belongs to nothing in the frame. `_is_conjunctive` already detects this shape
in `e8_articulation.shape_problems`, but it only records a shape problem; the
material is measured anyway.

A second recurring shape is the unlocalizable nominal: *"the silhouette of the
rider and horse"*, *"the hazy, pale sky and distant hills"*. The operationalizer
marks these `unusable` and the whole choice is greyed out, even though the
narratological reading behind it was correct and the reviewer went on to pick
by hand exactly the thing it had named.

## 6. Failure classes

Provisional and mostly low-confidence; these prioritise inspection, they do not
settle anything.

| class | figure | background |
|---|---|---|
| A interpretation | 115 | 71 |
| B material | 164 | 58 |
| C segmentation/measurement | 129 | 68 |
| D reduction | 1 | 1 |
| no failure (dE ≤ 3) | 28 | 39 |

The centre of mass is **B — the reading was usable, the material chosen to
carry it was not.**

## 7. Sequence

| | figure | background |
|---|---|---|
| consecutive step, same scene | 14.4 | 15.2 |
| consecutive step, across a scene change | 19.7 | 20.9 |
| within-scene pairwise | 15.6 | 15.8 |

There is a continuity signal and it is weak. Steps within a scene are smaller
than steps across a scene boundary, but the median within-scene step is still
dE 14–15, which is a visibly different colour. Provenance runs have a median
length of 1: the source of the palette changes from shot to shot constantly.

**There is no evidence here that the reviewer was smoothing a palette across a
sequence.** He appears to be deciding frame by frame. Whether he *should* be is
a separate question this data cannot answer.

## 8. What the review contradicts in the current system

- That the four quadrants are four hypotheses. In practice they are between one
  and three; choice 3 is never real, and choice 1 is real on a fifth of frames.
- That a narratological reading, once correct, will survive to a measured
  colour. It frequently does not: the material that carries it is often named
  in a form nothing can localize.
- That "what stands out" is the right question for the figure colour. It
  sometimes yields a single eye or a faint line — formally true, curatorially
  unusable — and the reviewer then chooses a materially substantial region
  instead. Sometimes, though, he does the opposite, so this is not a rule.
- That figure and background are one decision. They plainly are not.
"""


def write(root: Path) -> list:
    from services.palette_review_sheets import write_json
    from data.annotate import atomic_write_text

    write_json(root / "07-strategy-hypotheses.json", HYPOTHESES)
    path = root / "observations.md"
    atomic_write_text(path, OBSERVATIONS, encoding="utf-8")
    return [str(root / "07-strategy-hypotheses.json"), str(path)]
