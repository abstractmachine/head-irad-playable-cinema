"""Tests for services/corpus_stats.py."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from services.corpus_stats import get_corpus_stats, get_top_silhouette_labels


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Scene", "start_time", "end_time"])
        writer.writeheader()
        writer.writerows(rows)


def _write_silhouette_object(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (2, 2), (0, 0, 0, 0)).save(path.with_suffix(".png"), format="PNG")
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "label": path.parent.name,
                "filename_stem": path.parent.parent.name,
                "media_type": path.parents[2].name,
            }
        ),
        encoding="utf-8",
    )


class TestCorpusStats(unittest.TestCase):
    def test_corpus_stats_counts_and_top_labels(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project = Path(tmp_dir)

            _write_json(
                project / "data" / "metadata" / "movie.json",
                {
                    "version": "1",
                    "media": [
                        {"filename": "Film One.mp4", "title": "Film One"},
                        {"filename": "Film Two.mp4", "title": "Film Two"},
                    ],
                },
            )
            _write_json(
                project / "data" / "metadata" / "gameplay.json",
                {
                    "version": "1",
                    "media": [
                        {"filename": "Game Clip.mp4", "title": "Game Clip"},
                    ],
                },
            )

            _write_json(
                project / "data" / "vocabulary" / "vocabulary_movie.json",
                {
                    "meta": {"total_tokens": 3, "vocabulary_fields": ["animals", "objects"]},
                    "fields": {
                        "animals": {
                            "horse": {"count": 2, "aliases": ["horse"]},
                            "chair": {"count": 1, "aliases": ["chair"]},
                        },
                        "objects": {
                            "building": {"count": 1, "aliases": ["building"]},
                        },
                    },
                },
            )

            _write_json(
                project / "data" / "annotations" / "shots" / "movie" / "film-one.annotations.json",
                [{"shot": 1}, {"shot": 2}],
            )
            _write_json(
                project / "data" / "annotations" / "shots" / "movie" / "film-two.annotations.json",
                [{"shot": 1}],
            )

            _write_csv(
                project / "data" / "shotlists" / "movie" / "Film One.csv",
                [
                    {"Scene": "1", "start_time": "00:00:00.000", "end_time": "00:00:10.000"},
                    {"Scene": "1", "start_time": "00:00:10.000", "end_time": "00:00:20.000"},
                    {"Scene": "2", "start_time": "00:00:20.000", "end_time": "00:00:30.000"},
                ],
            )
            _write_csv(
                project / "data" / "shotlists" / "movie" / "Film Two.csv",
                [
                    {"Scene": "9", "start_time": "00:00:00.000", "end_time": "00:00:12.000"},
                ],
            )

            subtitle_dir = project / "media" / "subtitles" / "movie"
            subtitle_dir.mkdir(parents=True, exist_ok=True)
            (subtitle_dir / "Film One.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                encoding="utf-8",
            )

            _write_silhouette_object(
                project / "data" / "silhouettes" / "catalog" / "movie" / "film-one" / "horse" / "object_0001.json"
            )
            _write_silhouette_object(
                project / "data" / "silhouettes" / "catalog" / "movie" / "film-two" / "horse" / "object_0001.json"
            )
            _write_silhouette_object(
                project / "data" / "silhouettes" / "catalog" / "gameplay" / "game-clip" / "chair" / "object_0001.json"
            )

            stats = get_corpus_stats(str(project))

            self.assertEqual(stats["movie_videos"], 2)
            self.assertEqual(stats["gameplay_videos"], 1)
            self.assertEqual(stats["vocabulary_terms"], 3)
            self.assertEqual(stats["annotated_shots"], 3)
            self.assertEqual(stats["detected_scenes"], 3)
            self.assertEqual(stats["subtitle_files"], 1)
            self.assertEqual(stats["shotlists"], 2)

            # No illustration index has been built yet — silhouette figures
            # must be reported as explicitly unavailable, never silently
            # computed by scanning the raw catalog sidecars.
            self.assertIsNone(stats["silhouette_objects"])
            self.assertIsNone(stats["silhouette_labels"])
            self.assertEqual(stats["silhouette_state"], "unavailable")
            self.assertEqual(stats["silhouette_reason"], "illustration_index_missing")
            self.assertEqual(get_top_silhouette_labels(str(project), limit=2), [])

            # Building the illustration index for both media types is the
            # only thing that should make silhouette figures available.
            from services.illustration_index import rebuild_index

            rebuild_index(str(project), "silhouettes", "movie")
            rebuild_index(str(project), "silhouettes", "gameplay")

            stats = get_corpus_stats(str(project))
            self.assertEqual(stats["silhouette_objects"], 3)
            self.assertEqual(stats["silhouette_labels"], 2)
            self.assertEqual(stats["silhouette_state"], "ready")
            self.assertIsNone(stats["silhouette_reason"])

            self.assertEqual(get_top_silhouette_labels(str(project), limit=2), [("horse", 2), ("chair", 1)])

    def test_motif_count_from_annotation_json(self):
        """_count_motifs reads shot.motif from annotation JSON, not data/motifs/."""
        from services.corpus_stats import _count_motifs

        with tempfile.TemporaryDirectory() as tmp_dir:
            project = Path(tmp_dir)

            # Two movie shots with canonical shot.motif, one without
            _write_json(
                project / "data" / "annotations" / "shots" / "movie" / "film-one.annotations.json",
                [
                    {"shot": {"shot_id": "s1", "annotation": {}, "motif": "riding"}},
                    {"shot": {"shot_id": "s2", "annotation": {}, "motif": "duel"}},
                    {"shot": {"shot_id": "s3", "annotation": {}}},          # no motif
                    {"shot": {"shot_id": "s4", "annotation": {}, "motif": ""}},  # empty string
                    {"shot": {"shot_id": "s5", "annotation": {}, "motif": None}},  # null
                ],
            )
            # One gameplay shot with motif
            _write_json(
                project / "data" / "annotations" / "shots" / "gameplay" / "game.annotations.json",
                [
                    {"shot": {"shot_id": "g1", "annotation": {}, "motif": "snow"}},
                ],
            )
            # A sidecar file that must be ignored
            sidecar = project / "data" / "motifs" / "movie" / "film-one.json"
            _write_json(sidecar, {"shots": [{"shot_id": "s1", "value": "legacy"}] * 99})

            result = _count_motifs(str(project))

            self.assertEqual(result.get("movie", 0), 2)
            self.assertEqual(result.get("gameplay", 0), 1)