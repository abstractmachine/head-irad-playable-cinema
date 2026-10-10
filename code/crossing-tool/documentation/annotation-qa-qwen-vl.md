# Experimental Ontology-Aware Annotation QA

This is a read-only Proposal-1 experiment, not a production validator.
Annotation semantic support, ontology/scope, evidence quality, silhouette
assignment, and engraving eligibility are separate dimensions. Engraving
review is not semantic-correctness ground truth.

## Actual Ontology

The active project's `prompts/movie/shots/system-2026-04-12-v17.txt` defines
the shot annotation semantics. Both the bird and camp source records cite
that prompt, historically under another machine's project path. Prompt
selection is implemented by `data.annotate.load_system_prompt`; inline and
explicit prompts override the latest project prompt. There is no universal,
immutable Python ontology registry.

| Field | Actual proposition | Expected evidence/localization | Mask interpretation |
|---|---|---|---|
| objects | Visible inanimate items; a hat belongs here if not worn | Local entity plus frame context | Intended entity, but SAM output is not semantic proof |
| animals | All animals, including those used by humans | Local animal plus context | Animal instance/part, possibly incomplete |
| humans | Visually supported human categories/archetypes; plural labels, no individual counts | Figure and contextual appearance | Actor/figure, not verified identity or social role |
| wearing | Clothing and gear worn by figures, preferring short/archetypal terms | Frame establishes wearer relation; crop is diagnostic | Item/gear evidence, not the complete wearing relation |
| action | One or two simple visible verbs | Actor/environment context; motion may need multiple frames | Concept-prompt region is evidentiary, not a literal action segmentation |
| setting | One dominant visible place/environment | Full frame; not necessarily spatially bounded | Scene region or accidental concept response, not proof of the setting |
| description | One factual sentence: subject, environment, visible action | Shot context | No prescribed mask |
| spatial | indoor/outdoor/mixed/unknown in schema; prompt asks to choose first three | Frame-level environment | No prescribed mask |
| time_of_day | day/night/dawn-dusk/unknown in schema; lighting-based | Contextual lighting | No prescribed mask |
| shot | extreme wide/wide/medium long/medium/full/close-up/unknown | Framing relative to subjects | No prescribed mask |
| camera | travelling/pan/zoom/birds-eye/static/unknown; default static | Often temporal evidence; a still cannot establish movement | No prescribed mask |
| type | diegetic/graphics/title/credits/intertitle/meta/the_end | Shot content/presentation | No prescribed mask |
| text | Visible text, excluding subtitles | Text region with context | Potential text evidence, not transcription validation |
| motif | Separate derived semantic title stored on shot, not in v17 annotation schema | Not a direct visual proposition | Not routed by this probe |
| dress | Present in display configuration, absent from v17 schema | Meaning not established | Conservatively unsupported |

The humans prompt lists cowboy/cowgirl, man/woman/child/group/crowd,
sheriff/soldier/outlaw/bandit/rider, lady/gentleman, saloon girl/barman,
farmer/farm worker, with pluralization. Wearing lists base clothing and
Western gear (including holster, gun belt, spurs and military uniform).
Setting examples include desert, town, saloon, cave, camp, forest, mountain
and field. These are prompt constraints, not runtime-enforced enumerations:
actual annotations include additional places such as room.

`preferences/data/fields.yaml` configures display fields, atomic list
normalization, and vocabulary. Current atomic fields are humans, wearing,
animals, objects and action; vocabulary additionally includes setting.
`data.annotate._validate_annotation` coerces setting to string and
objects/humans/animals/text to lists, lowercases keys, normalizes configured
atomic lists, and preserves extra fields. It does not enforce all v17
types/allowed values or perform multimodal truth validation.

## Actual Lifecycle

1. `crossing annotate` calls `data.annotate` using selected project prompts
   and sampled shot frames. Model text is parsed, structurally normalized,
   and persisted directly to `data/annotations/shots/<media>/<stem>.json`.
   Entries contain movie identity, model/prompt provenance, stable shot ID,
   and `shot.annotation`. Failed generation is omitted for later retry.
2. Shotlist displays/edits these annotations. `_on_fields_cell_changed`
   persists edits immediately. The valid-annotation display test checks
   dictionary/setting presence, not human acceptance or semantic truth.
   No annotation proposal accepted/rejected/inactive lifecycle was found in
   these owning paths. Shot-boundary editing/validation is a different task.
3. `crossing index silhouette extract LABEL --field FIELD` searches shots
   using `services.search.search_shots`. Multi-field extraction can expand
   configured vocabulary, including setting, action and humans. An explicit
   field is not restricted to object/animal semantics.
4. `services.silhouette_catalog.extract_objects_for_shot` chooses a
   label-specific CLIP frame, asks SAM3 to segment the label, filters mask
   size/borders, reranks broad/tight crops with CLIP, deduplicates masks and
   saves transparent PNGs and object metadata. `field` is provenance; the
   SAM concept prompt is the label, not a field-aware proposition.
5. Catalog identity includes media/film/label/object ID. Metadata retains
   field, shot, frame, bbox, mask area and source path. The general BEST PNG
   named by `source_frame` may not match the label-specific `frame`.
6. Illustration indexes catalog records. Active/inactive/superseded and
   human-best describe silhouette lifecycle/curation, not annotation truth.
   Non-object fields can reach Illustration and downstream engraving
   selection; indexing does not itself certify engraving eligibility.

Existing `semantic_confidence` is mean CLIP softmax confidence over
viewpoint/completeness/occlusion/isolation classifications, not confidence
that the source annotation is correct. `engraving_score` combines geometry
and evidence-condition factors. Canonical-search provenance answers source
shot search membership, not independent visual entailment. Existing
normalization and atomic-write tests encode structure/persistence, not
ontology-aware multimodal correctness.

The bird trace is animals/bird in Qwen3-VL-8B-Instruct annotation, source
shot `tmdb_61044@f078383-f078476`, label-specific frame 78453 versus general
BEST 78447. Camp is setting/camp in the same annotation model/prompt family,
shot `tmdb_43887@f020169-f020209`, label-specific frame 20189 versus general
BEST 20183. Neither persisted entry records human annotation acceptance.

## Recommended Insertion Point

For future proposals, advisory QA fits after JSON parsing/normalization
and before canonical persistence, followed by human inspection when
appropriate. That integration is not implemented here. Existing annotations
can instead receive read-only post hoc QA; prior human decisions must not
be overridden. A separate mask-association check belongs downstream of
segmentation. Engraving eligibility remains a distinct policy.

## Probe and Reproduction

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run python \
  -m services.annotation_qa_qwen_vl \
  --project /home/cowpoke/playable/dead-crossing
```

The runner refuses any manifest other than the ordered forty Proposal-1
IDs. It never calls annotation generation, catalog mutation, index rebuild,
or engraving generation. The `annotation-qa` optional dependency group
declares Qwen's required utility. No model download is performed.

Model: `Qwen/Qwen3-VL-Reranker-2B`, local BF16 checkpoint at
`models/Qwen3-VL-Reranker-2B`, downloaded revision
`4bd860ac4f15ad1897a214615cccc700f8f71818`. SHA256s of weights, configuration,
processor, tokenizer files and supplied script are recorded in provenance.
Runtime: Torch 2.12.0/CUDA 13.0, Transformers 5.9.0, qwen-vl-utils 0.0.14,
Pillow 12.2.0, Accelerate 1.13.0. FlashAttention 2 is absent; SDPA is used.

The supplied `scripts/qwen3_vl_reranker.py` formats query/document/instruction,
uses qwen-vl-utils for image loading/resizing, and computes the last-token
projection against yes-minus-no LM-head weights. Its sigmoid is preserved;
raw logits are captured via a forward hook. These are retrieval relevance
scores, not calibrated truth probabilities. Images use the reference
4096-to-1310720 pixel budget, 32-pixel factor and 8192-token cap.
Transformers 5 requires rebuilding `mm_token_type_ids` with the processor's
own method after the supplied script pads tokens; this compatibility step
is recorded. The downloaded reference script is not edited.

Every case gets a white-composited RGB silhouette crop and an exact
label-selected contextual frame. General BEST is reused only when its
recorded frame number matches; otherwise the existing mosaic extractor
reads the correct frame into the experiment directory. Marked overlays
were deferred. Object/animal crops and frames remain separate. Wearing,
action and setting use frame-primary predicates with diagnostic crop scores.
Exact predicates/instructions and image hashes are stored for every query.

Four controls plus the proposed label are scored per view. Controls come
from same-field current annotations of the benchmark films, with conservative
experimental ontology filters and lexical/structural contrasts. The first
unfiltered pass exposed animals/wagons contamination and is retained as
`results-initial-controls.json`; final controls exclude that mismatch.
Alternatives may be synonyms or co-present: they are not verified negatives.
No ranking result replaces an annotation.

## Results and Interpretation

All forty scored: 28 objects, 5 wearing, 4 animals, 2 action, 1 setting.
The full matrix includes per-view scores/ranks/competitors/margins, source
movie/frame/shot/annotation values/state, and the separate engraving review.
The 16 accepted/24 rejected review is never used as semantic truth labels.

Camp's crop is CINCINNATI lettering on a bench. The frame shows women outside
a log cabin. Crop relevance is 0.3203, frame 0.4180; camp ranks second behind
farm (0.4629), margin -0.0449. This establishes poor local mask evidence and
a downstream scope mismatch, not that camp is invalid for lacking objecthood.
An upstream annotation error remains possible but unproven; wider temporal
context was not scored.

Frame medians: accepted 0.4521, engraving-rejected 0.4414. Examples of
rejected but relatively strong support include barrel/revolver/holster.
Accepted flower and lamp have relatively low full-frame scores. Action/head
and action/face expose nominal-label drift; actual source action propositions
were additionally scored separately, without replacing the original query.
The score compression, synonyms and crop/context disagreement argue against
production thresholds from this experiment. Candidate 0.2/0.5/0.8 cutoffs
are shown only as exploratory associations, never accuracy/calibration.

Model parameters: 2,127,532,032, all BF16, 4,255,064,064 parameter bytes
(3.96 GiB). Warm load 0.72-0.85 s; warm single decisions approximately
120 ms, two-item batches 176 ms (88 ms/decision). Measured inference
throughput about 12.5 decisions/s; peak allocated inference VRAM 4.50 GiB.
Repeat raw deltas were zero; maximum single/batch raw delta was 0.0332.
These are warm/local measurements, not cold-start or archive-scale estimates.
The initial complete 403-decision pass took 32.36 s scoring/52.69 s overall;
control repair reused 226 decisions and computed 174 in 14.21 s scoring.
Wall times exclude safety hashing; model-load peak/driver reservation were
not measured. The final matrix contains 400 view/control decisions plus
three separate source-action queries, with 577 cached unique decisions
across both control passes.

Artifacts are under `outputs/tests/annotation-qa-qwen-vl/`: `results.json`,
`report.md`, `model-provenance.json`, `prepared-cases.json`, `smoke.json`,
`scores-cache.json`, `exploratory-analysis.json`, protected before/after
snapshots, and input PNGs. Cache keys include model file/config/revision
identity, runtime dependencies, BF16/attention settings, exact versioned
predicate/instruction, field, label/query and input-image SHA256.

Safety checks covered 2,373 files: canonical annotations, Illustration
indexes, engraving catalog including bird provenance, all benchmark/review
materials, forty silhouette JSON/PNG pairs, and field configuration. Bytes
and mtimes were unchanged. No annotations were invalidated/relabelled, no
assignments changed, no engravings generated, and no temporal tracking ran.
Stop here for human/research review; no corpus-wide QA is authorized.