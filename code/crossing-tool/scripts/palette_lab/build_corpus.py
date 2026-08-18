"""Build the 30-image palette research corpus.

Selections are expressed as (pool, rank) pairs so that the curatorial choice
stays tied to a reproducible query rather than to a hand-copied id.  The pools
are the same searches used during curation; re-running this file regenerates
the identical corpus.

    uv run python -m scripts.palette_lab.build_corpus
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from scripts.palette_lab import contact_sheet as CS
from scripts.palette_lab import corpus as C

SPACE = "palette-system-2026-08-18"


# ---------------------------------------------------------------------------
# Candidate pools — the queries that produced the shortlist
# ---------------------------------------------------------------------------

def build_pools(records: list[dict]) -> dict:
    with_palette = [x for x in records if C.has_frame(x) and x.get("fg_lab")]
    with_frame = [x for x in records if C.has_frame(x)]

    def pick(rows, key, n, per_film=1):
        seen: dict = {}
        out = []
        for row in sorted(rows, key=key):
            if seen.get(row["title"], 0) >= per_film:
                continue
            seen[row["title"]] = seen.get(row["title"], 0) + 1
            out.append(row)
            if len(out) >= n:
                break
        return out

    searchers = [x for x in with_palette if x["title"] == "The Searchers"]
    gameplay = [x for x in with_frame if x["media_type"] == "gameplay"]

    return {
        "S-wide": pick(
            [x for x in searchers if x["shot"] == "wide"],
            lambda x: (x["fg_coverage"] or 1), 25, 99),
        "S-cu": pick(
            [x for x in searchers if x["shot"] in ("close-up", "medium close-up")],
            lambda x: -(x.get("max_C") or 0), 25, 99),
        "S-indoor": pick(
            [x for x in searchers if x["spatial"] == "indoor"],
            lambda x: -(x.get("L_range") or 0), 25, 99),
        "A-figure-in-landscape": pick(
            [x for x in with_palette
             if x["shot"] == "wide" and x["humans"] and not x["animals"]
             and x["setting"] in ("desert", "mountain", "canyon", "field", "cliff", "hillside", "plain")
             and (x["fg_coverage"] or 1) < 0.12],
            lambda x: (x["fg_coverage"] or 1), 20),
        "B-face-in-darkness": pick(
            [x for x in with_palette
             if x["shot"] in ("close-up", "medium close-up")
             and (x["bg_L"] or 99) < 14 and (x["fg_L"] or 0) > 28 and x["humans"]],
            lambda x: (x["bg_L"] - x["fg_L"]), 20),
        "C-monochrome": pick(
            [x for x in with_palette
             if (x["max_C"] or 99) < 4.5 and (x["L_range"] or 0) > 34 and x["year"] < "1960"],
            lambda x: -(x["L_range"] or 0), 20),
        "D-saturated-accent": pick(
            [x for x in with_palette
             if (x["max_C"] or 0) > 60 and (x["fg_C"] or 99) < 14 and (x["bg_C"] or 99) < 14],
            lambda x: -(x["max_C"]), 20),
        "E-doorway": pick(
            C.search(with_palette, r"doorway|door frame|framed by the door|through the door|open door"),
            lambda x: -((x["bg_L"] or 0) - (x["fg_L"] or 0)), 20),
        "I-silhouette": pick(
            C.search(with_palette, r"silhouette|silhouetted|against the sky|backlit|back-lit"),
            lambda x: (x["fg_L"] or 99), 20),
        "N-trackofcat": pick(
            [x for x in with_palette if x["title"] == "Track of the Cat"],
            lambda x: -(x["max_C"] or 0), 15, 99),
        "O-object-dominant": pick(
            [x for x in with_palette
             if not x["humans"] and not x["animals"] and x["objects"]
             and x["shot"] in ("close-up", "medium close-up", "medium")],
            lambda x: -(x.get("max_C") or 0), 15),
        "K-gp-landscape": pick(
            [x for x in gameplay
             if x["shot"] == "wide" and x["spatial"] == "outdoor" and not x["humans"]],
            lambda x: x["shot_id"], 20, 99),
        "L-gp-night": pick(
            [x for x in gameplay if x["time_of_day"] in ("night", "dusk", "dawn-dusk")],
            lambda x: x["shot_id"], 20, 99),
    }


# ---------------------------------------------------------------------------
# The curated selection
# ---------------------------------------------------------------------------
# ("frame", pool, rank) — a shot resolved from a query pool
# ("shot", shot_id)          — a shot addressed directly (CLIP semantic finds)
# ("poster", filename-stem)  — a film poster from media/thumbnails/movie
# ("thumb", media_id, media_type) — a canonical media thumbnail

SELECTION = [
    dict(id="PAL-001", src=("frame", "S-wide", 17),
         tags=["canonical", "aperture", "figure-vs-landscape"],
         group="aperture-in-dark-mass",
         why="Ford's cave-mouth framing: a huge black rock mass occupies most of the frame and the entire narrative event happens inside a small bright aperture.",
         question="Does the focus become the tiny riders inside the aperture, or the black rock that organises the composition?",
         fail="Area dominance may make the rock the foreground and reduce the riders to noise; SAM3 may fail to find a nameable subject in the bright hole."),
    dict(id="PAL-002", src=("frame", "S-wide", 5),
         tags=["canonical", "solitary-rider", "tiny-figure"],
         group="figure-in-landscape",
         why="The emblematic solitary rider on a bare dune — one small dark silhouette on an enormous soft field of sand.",
         question="Does the rider remain the focus against an overwhelming landscape, or does the dune claim both focus and ambience?",
         fail="The interpreter may privilege the human simply because a human is present, while measurement finds almost no rider pixels."),
    dict(id="PAL-003", src=("frame", "S-wide", 24),
         tags=["canonical", "setting-only", "monument-valley"],
         group="setting-as-subject",
         why="Monument Valley under a dark sky with no human, animal or object annotated — the landscape has to carry the image alone.",
         question="Can an empty landscape be named as the focus, and what then becomes the ambience?",
         fail="The model may invent a subject rather than accept the setting; focus and ambience masks may collapse onto the same pixels."),
    dict(id="PAL-004", src=("frame", "S-indoor", 9),
         tags=["canonical", "interior", "doorway", "group"],
         group="doorway",
         why="A dim cabin interior with a group of figures and a blown-out doorway — the Ford interior/exterior threshold, with several faces competing.",
         question="Does the bright doorway or a human face become the focus of a dark interior?",
         fail="The model may confuse the bright aperture with the visual focus; multiple faces may produce one merged human mask."),
    dict(id="PAL-005", src=("frame", "S-wide", 10),
         tags=["canonical", "snow", "saturated-accent", "horse"],
         group="accent-in-neutral-field",
         why="Riders crossing a snowy river in a near-neutral winter forest, with one saturated red blanket as the only chromatic event.",
         question="Does the red blanket become curatorially important despite its tiny area?",
         fail="Ward may absorb the red into a larger neutral cluster, so the accent never reaches the curator as a candidate."),
    dict(id="PAL-006", src=("frame", "S-cu", 8),
         tags=["canonical", "portrait", "dark-coat"],
         group="face-vs-dark-mass",
         why="Wayne in a dark hat and coat against a near-black background — the classical-Technicolor version of the black-coat problem that started this research.",
         question="Does the system choose the illuminated face rather than the dark hat and coat that dominate the figure?",
         fail="Exactly the ce5e0bba failure: area dominance may overwhelm the face."),

    dict(id="PAL-007", src=("frame", "A-figure-in-landscape", 1),
         tags=["figure-vs-landscape", "tiny-figure", "contemporary"],
         group="figure-in-landscape",
         why="A single prospector digging in a vast yellow-green field — a modern, deliberately flat restatement of the figure-in-landscape problem.",
         question="Is the man the focus, or is the field the subject and the man merely its punctuation?",
         fail="The saturated field may dominate every candidate; the figure may not survive Ward as a separate colour family."),
    dict(id="PAL-008", src=("frame", "A-figure-in-landscape", 6),
         tags=["silent", "monochrome", "unusual-crop", "tiny-figure"],
         group="historical-extremes",
         why="A 1925 silent frame: near-monochrome, soft, and surrounded by the visible film-frame border — the image is not flush with the rectangle.",
         question="In an almost purely luminance image, does Ward reveal meaningful tonal strata or just grey noise?",
         fail="Letterbox detection may not recognise a non-black frame border, so the surround is measured as if it were picture."),
    dict(id="PAL-009", src=("frame", "A-figure-in-landscape", 11),
         tags=["silent", "tinted", "vignette", "provocation"],
         group="historical-extremes",
         why="A 1922 silent frame with an overall chemical tint and a heavy vignette: every pixel is pre-coloured by the print, not by the scene.",
         question="What does a system built on perceptual colour do when the whole image is a single applied dye?",
         fail="Focus and ambience may return near-identical hues; the vignette may dominate the background candidates."),
    dict(id="PAL-010", src=("frame", "A-figure-in-landscape", 0),
         tags=["silhouette", "fire", "figure-vs-landscape", "canonical"],
         group="fire-and-silhouette",
         why="Malick silhouettes against a burning field — the figures carry no colour at all and the only chromatic event is the fire behind them.",
         question="Is the focus the black silhouettes, or the fire that makes them legible?",
         fail="A colourless focus may yield only near-black candidates, forcing the curator to choose between indistinguishable blacks."),

    dict(id="PAL-011", src=("frame", "B-face-in-darkness", 0),
         tags=["close-up", "low-key", "face-vs-dark-mass", "canonical"],
         group="face-vs-dark-mass",
         why="A lit face emerging from near-total darkness with a small warm lamp at the edge — the purest form of the area-versus-significance conflict.",
         question="Does the curator prefer the illuminated skin over the vastly larger black field?",
         fail="Near-black may win on coverage; the warm lamp may be selected as a proxy for the face."),
    dict(id="PAL-012", src=("shot", "tmdb_938@f082099-f082120"),
         tags=["low-key", "tiny-subject", "canonical", "spaghetti"],
         group="accent-in-neutral-field",
         why="Leone: a cowboy hat suspended in an almost entirely black night frame. Roughly all of the image is one colour, and the only content is a small neutral object.",
         question="When almost nothing is measurable, does the system find the object or describe the darkness?",
         fail="SEEDS may produce a field of near-identical black superpixels; the hat may not survive Ward as a separate candidate."),
    dict(id="PAL-013", src=("frame", "B-face-in-darkness", 19),
         tags=["black-and-white", "multiple-faces", "interior", "postwar"],
         group="luminance-only",
         why="A black-and-white noir Western interior with two faces — a palette that consists of nothing but lightness.",
         question="With chroma effectively zero, does the curator reason about tonal structure or fall back on area?",
         fail="Every candidate will be a neutral grey; the fg/bg distinction may become meaningless."),

    dict(id="PAL-014", src=("frame", "C-monochrome", 1),
         tags=["silent", "typography", "graphic", "monochrome"],
         group="typography",
         why="A 1925 silent intertitle: white lettering on black, with no photographic content at all.",
         question="Does typography become a legitimate focus, or does the system look for a subject that does not exist?",
         fail="SAM3 may return nothing for a text phrase; the interpreter may hallucinate a scene from the words it reads."),
    dict(id="PAL-015", src=("frame", "N-trackofcat", 2),
         tags=["saturated-accent", "snow", "ambiguity", "canonical"],
         group="accent-in-neutral-field",
         why="Wellman's colour film designed to look monochrome: one red mackinaw in a white forest, with a second figure in pure black.",
         question="Between the red coat, the black figure and the white snow, which does the system call the focus?",
         fail="The red may win for the wrong reason (chroma weighting) rather than as a reasoned interpretation of the composition."),
    dict(id="PAL-016", src=("frame", "D-saturated-accent", 0),
         tags=["typography", "canonical", "spaghetti", "text-over-image"],
         group="typography",
         why="Corbucci's opening: saturated red credit typography laid over a man dragging a coffin through mud.",
         question="Does the system treat the credit type as contamination, as ambience, or as the actual visual focus?",
         fail="Typography may contaminate candidate generation and be described as a physical object in the scene."),

    dict(id="PAL-017", src=("frame", "O-object-dominant", 14),
         tags=["object-dominant", "gun", "tinted", "monochrome"],
         group="object-vs-human",
         why="A hand thrusting a revolver at the camera in a heavily sepia-tinted frame — the object, not the person, occupies the compositional centre.",
         question="Where an object appears to matter more compositionally than the human holding it, which does the interpreter choose?",
         fail="The interpreter may default to the human because a hand is visible; the tint may flatten all candidates to one hue."),
    dict(id="PAL-018", src=("shot", "tmdb_113629@f113572-f114107"),
         tags=["provocation", "image-within-image", "reflection", "face"],
         group="provocations",
         why="Godard: the only face in the frame exists as a reflection inside a hand mirror, surrounded by near-black. The face is not in the scene — it is an image held within it.",
         question="Is a reflected face a face, an object, or a picture? And is the mirror the focus or its content?",
         fail="SAM3 may segment the mirror rather than the reflection; the interpreter may describe a woman the measurement cannot locate."),

    dict(id="PAL-019", src=("frame", "I-silhouette", 0),
         tags=["graphic", "silhouette", "flat-colour", "canonical"],
         group="fire-and-silhouette",
         why="Leone's title sequence: a pure black cowboy silhouette on a flat, fully saturated red ground. No texture, no depth, no lighting.",
         question="What is focus and what is ambience when the figure has no colour and the ground has no detail?",
         fail="SEEDS may produce almost no meaningful superpixels in a flat field; the focus candidates may all be identical black."),
    dict(id="PAL-020", src=("frame", "I-silhouette", 9),
         tags=["silhouette", "ambiguity", "canonical", "revisionist"],
         group="fire-and-silhouette",
         why="The lone figure and the lone tree against an orange sunset — two silhouettes of comparable weight, and a sky that may be the real subject.",
         question="Where two reasonable viewers would disagree between figure, tree and sky, what does the interpreter choose?",
         fail="SAM3 may segment both silhouettes together; the alternative interpretation may be more defensible than the primary one."),
    dict(id="PAL-021", src=("frame", "E-doorway", 7),
         tags=["doorway", "architecture", "silhouette", "contemporary"],
         group="doorway",
         why="A black figure standing inside the golden aperture of a ruined building — architecture, silhouette and threshold at once.",
         question="Is the focus the person, the doorway, or the building that frames them?",
         fail="The figure may be too dark to yield candidates distinguishable from the surrounding structure."),

    dict(id="PAL-022", src=("thumb", "game_rdr2_ce5e0bba", "gameplay"),
         tags=["gameplay", "canonical-failure", "subtitles", "multiple-faces"],
         group="face-vs-dark-mass",
         why="The image that started this research: three men in heavy dark coats in a snowy forest, plus burned-in subtitles and a watermark.",
         question="Does the curator select the illuminated faces rather than the coats, now that we can see what each candidate is?",
         fail="Subtitle text and watermark may form their own candidate and be mistaken for scene content."),
    dict(id="PAL-023", src=("frame", "K-gp-landscape", 13),
         tags=["gameplay", "high-key", "setting-only", "atmosphere"],
         group="setting-as-subject",
         why="A cabin barely visible in a whiteout blizzard — an almost entirely high-key frame with no human and very little structure.",
         question="At the opposite extreme from a black frame, does the system find anything to call a focus?",
         fail="All candidates may be near-white and perceptually indistinguishable; the residual region may swallow the image."),
    dict(id="PAL-024", src=("frame", "K-gp-landscape", 11),
         tags=["gameplay", "fire", "complementary", "night"],
         group="fire-and-silhouette",
         why="A burning wooden structure in a snowy field at night — orange fire against blue snow, a genuine complementary split.",
         question="With two strong opposed colour fields, does the focus/ambience pair capture the opposition or collapse it?",
         fail="The fire may be chosen as both focus and ambience; the blue snow may be treated as residual."),
    dict(id="PAL-025", src=("frame", "L-gp-night", 6),
         tags=["gameplay", "typography", "graphic", "title-card"],
         group="typography",
         why="The game's own title card: white and red type on pure black — a twenty-first-century equivalent of the silent intertitle in PAL-014.",
         question="Does a game title card get read the same way as a 1925 intertitle?",
         fail="The interpreter may describe the game rather than the image, using the words it reads as evidence."),
    dict(id="PAL-026", src=("frame", "L-gp-night", 18),
         tags=["gameplay", "interior", "warm-light", "point-source"],
         group="light-as-subject",
         why="A figure carrying a lantern through a black wooden interior, with opening-credit typography overlaid — the image is organised around a point light source rather than around an object.",
         question="Where an image is organised around light rather than a thing, what does the interpreter name as the focus?",
         fail="The lantern flame may be too small to cluster; the overlaid credit text may be read as scene content."),

    dict(id="PAL-027", src=("poster", "The Wild Bunch (1969) {tmdb-576}"),
         tags=["poster", "graphic", "silhouette", "typography"],
         group="poster-graphic-vs-photographic",
         why="A designed poster: a row of flat black silhouettes on a graduated field with large display type. No photographic depth at all.",
         question="On a poster with no photographic subject, does the system choose the figures, the type, or the colour field?",
         fail="A designed gradient may produce meaningless smooth candidates; the figures may merge into one mass."),
    dict(id="PAL-028", src=("poster", "Dead Man (1995) {tmdb-922}"),
         tags=["poster", "monochrome", "typography", "revisionist"],
         group="typography",
         why="A black-and-white poster where hand-drawn white lettering visually outweighs the photographic image behind it.",
         question="When typography is genuinely the largest and brightest element, is naming it as the focus correct or a failure?",
         fail="A monochrome poster gives the curator nothing but lightness to reason about."),
    dict(id="PAL-029", src=("poster", "First Cow (2019) {tmdb-558582}"),
         tags=["poster", "typography", "tiny-subject", "landscape"],
         group="poster-graphic-vs-photographic",
         why="Enormous yellow title type over a muted landscape containing one very small brown cow — the title of the film names the smallest thing in it.",
         question="Between huge typography, a wide landscape and a tiny animal, which is the focus?",
         fail="The cow may be too small to segment; the type may dominate every measured candidate."),
    dict(id="PAL-030", src=("poster", "Django (1966) {tmdb-10772}"),
         tags=["poster", "object-dominant", "face", "gun"],
         group="object-vs-human",
         why="A photographic poster where a revolver in forced perspective, a face, and a large red title all compete for the same attention.",
         question="On a poster built from three competing elements, does the interpreter commit to one or hedge?",
         fail="The interpreter may enumerate all three rather than choosing; SAM3 may segment the gun and the face equally well."),
]


def resolve(selection: list[dict], pools: dict, project: Path, records: list[dict]) -> list[dict]:
    by_shot = {r["shot_id"]: r for r in records}
    out = []
    for entry in selection:
        kind = entry["src"][0]
        record: dict = {}

        if kind in ("frame", "shot"):
            if kind == "frame":
                _, pool_name, rank = entry["src"]
                pool = pools[pool_name]
                if rank >= len(pool):
                    raise IndexError(f"{entry['id']}: pool {pool_name} has {len(pool)} items, wanted {rank}")
                shot = pool[rank]
                origin = f"{pool_name}#{rank}"
            else:
                shot = by_shot[entry["src"][1]]
                origin = "clip-semantic-search"
            record = {
                "media_id": shot["media_id"],
                "shot_id": shot["shot_id"],
                "film": shot["title"],
                "year": shot["year"],
                "director": shot["director"],
                "media_type": shot["media_type"],
                "image_kind": "gameplay_frame" if shot["media_type"] == "gameplay" else "film_frame",
                "image": shot["frame"],
                "annotation_present": True,
                "description": shot["description"],
                "setting": shot["setting"],
                "framing": shot["shot"],
                "time_of_day": shot["time_of_day"],
                "humans": shot["humans"],
                "animals": shot["animals"],
                "objects": shot["objects"],
                "production_palette": {
                    "foreground_rgb": shot.get("fg_rgb"),
                    "background_rgb": shot.get("bg_rgb"),
                    "fg_L": shot.get("fg_L"), "bg_L": shot.get("bg_L"),
                    "max_chroma": shot.get("max_C"), "fg_bg_deltaE": shot.get("fg_bg_dE"),
                },
                "source_pool": origin,
            }

        elif kind == "poster":
            stem = entry["src"][1]
            meta = json.loads((project / "data/metadata/movie.json").read_text())["media"]
            film = next(m for m in meta if Path(m["filename"]).stem == stem)
            record = {
                "media_id": film["media_id"],
                "shot_id": None,
                "film": film.get("title", ""),
                "year": str(film.get("year", "")),
                "director": film.get("director", ""),
                "media_type": "movie",
                "image_kind": "poster",
                "image": str(project / "media/thumbnails/movie" / f"{stem}.jpg"),
                "annotation_present": False,
                "description": "",
                "source_pool": "poster",
            }

        elif kind == "thumb":
            _, media_id, media_type = entry["src"]
            meta = json.loads((project / f"data/metadata/{media_type}.json").read_text())["media"]
            film = next(m for m in meta if m["media_id"] == media_id)
            stem = Path(film["filename"]).stem
            record = {
                "media_id": media_id,
                "shot_id": None,
                "film": film.get("title", ""),
                "year": str(film.get("year", "")),
                "director": film.get("director", ""),
                "media_type": media_type,
                "image_kind": "gameplay_frame",
                "image": str(project / f"media/thumbnails/{media_type}" / f"{stem}.jpg"),
                "annotation_present": True,
                "description": "",
                "source_pool": "canonical-thumbnail",
            }

        out.append({
            "id": entry["id"],
            **record,
            "research_tags": entry["tags"],
            "comparison_group": entry["group"],
            "selection_reason": entry["why"],
            "research_question": entry["question"],
            "anticipated_failure": entry["fail"],
        })
    return out


def main() -> Path:
    from tool import prefs

    project = Path(prefs.get("path"))
    records = C.load(str(project))
    pools = build_pools(records)
    entries = resolve(SELECTION, pools, project, records)

    out_dir = project / "outputs" / "tests" / SPACE / "corpus"
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        source = Path(entry["image"])
        if not source.exists():
            raise FileNotFoundError(f"{entry['id']}: missing {source}")
        destination = images_dir / f"{entry['id']}{source.suffix.lower()}"
        shutil.copy2(source, destination)
        entry["corpus_image"] = str(destination.relative_to(out_dir))

    manifest = {
        "space": SPACE,
        "created": datetime.now(timezone.utc).isoformat(),
        "count": len(entries),
        "note": "Proposed research corpus. Palette System 2.0 has NOT been run on these images.",
        "images": entries,
    }
    (out_dir / "corpus-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # contact sheets, 10 per sheet
    for index, start in enumerate(range(0, len(entries), 10), start=1):
        chunk = entries[start:start + 10]
        cells = [{
            "image": str(out_dir / e["corpus_image"]),
            "id": e["id"],
            "line1": f"{e['film'][:34]}",
            "line2": f"{e['year']}  {e['image_kind'].replace('_', ' ')}",
            "tags": e["research_tags"][:3],
        } for e in chunk]
        CS.sheet(
            cells, out_dir / f"contact-sheet-{index}.jpg",
            title=f"Palette 2.0 research corpus — sheet {index} of 3  ({chunk[0]['id']}–{chunk[-1]['id']})",
            columns=5, cell_width=560, cell_height=315, caption_height=84,
        )

    lines = [
        "# Palette System 2.0 — proposed research corpus",
        "",
        f"{len(entries)} images. Palette System 2.0 has **not** been run on them yet.",
        "",
    ]
    for entry in entries:
        lines += [
            f"## {entry['id']} — {entry['film']} ({entry['year']})",
            "",
            f"![{entry['id']}]({entry['corpus_image']})",
            "",
            f"- **Source** — {entry['image_kind']}, `{entry.get('shot_id') or entry['media_id']}`",
            f"- **Tags** — {', '.join(entry['research_tags'])}",
            f"- **Comparison group** — {entry['comparison_group']}",
            "",
            f"**Why selected.** {entry['selection_reason']}",
            "",
            f"**Question.** {entry['research_question']}",
            "",
            f"**Possible failure.** {entry['anticipated_failure']}",
            "",
        ]
    (out_dir / "corpus.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"corpus written: {out_dir}")
    for entry in entries:
        print(f"  {entry['id']}  {entry['film'][:34]:34s} {entry['year']:5s} {entry['image_kind']}")
    return out_dir


if __name__ == "__main__":
    main()
