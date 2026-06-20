"""Tests for serialize_annotation_item() in data/index.py.

Covers:
- Standard annotation fields
- Virtual motif field: canonical plain string (shot.motif = "word")
- Virtual motif field: legacy dict forms (value, motif, word, label)
- Missing motif with skip_empty=True (no crash, field omitted)
- Missing motif with skip_empty=False (empty value included)
- List field formatting
- include_labels=False
- Full acceptance-criteria example from the spec
"""

import pytest

from data.index import serialize_annotation_item, _get_serialized_field_value


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MAPPING_WITH_MOTIF = {
    "fields": ["description", "motif", "setting", "animals", "action"],
    "include_labels": True,
    "separator": " | ",
    "skip_empty": True,
}

# Canonical form: shot.motif is a plain string (after attach or generation)
FULL_ITEM_STRING_MOTIF = {
    "shot": {
        "annotation": {
            "description": "A rider crosses a snowy forest.",
            "setting": "forest",
            "animals": ["horse"],
            "action": ["riding"],
        },
        "motif": "shelter",
    }
}

# Legacy form: shot.motif is a dict (pre-attach data)
FULL_ITEM_DICT_MOTIF = {
    "shot": {
        "annotation": {
            "description": "A rider crosses a snowy forest.",
            "setting": "forest",
            "animals": ["horse"],
            "action": ["riding"],
        },
        "motif": {
            "value": "shelter",
        },
    }
}

# Keep alias for old tests that used FULL_ITEM
FULL_ITEM = FULL_ITEM_DICT_MOTIF


# ---------------------------------------------------------------------------
# _get_serialized_field_value
# ---------------------------------------------------------------------------

class TestGetSerializedFieldValue:
    def test_standard_field_str(self):
        item = {"shot": {"annotation": {"description": "A dark alley."}, "motif": None}}
        assert _get_serialized_field_value(item, "description") == "A dark alley."

    def test_standard_field_list(self):
        item = {"shot": {"annotation": {"animals": ["horse", "dog"]}, "motif": None}}
        assert _get_serialized_field_value(item, "animals") == ["horse", "dog"]

    def test_standard_field_missing(self):
        item = {"shot": {"annotation": {}, "motif": None}}
        assert _get_serialized_field_value(item, "setting") is None

    def test_motif_value_key(self):
        item = {"shot": {"annotation": {}, "motif": {"value": "shelter"}}}
        assert _get_serialized_field_value(item, "motif") == "shelter"

    def test_motif_motif_key_fallback(self):
        item = {"shot": {"annotation": {}, "motif": {"motif": "journey"}}}
        assert _get_serialized_field_value(item, "motif") == "journey"

    def test_motif_word_key_fallback(self):
        item = {"shot": {"annotation": {}, "motif": {"word": "exile"}}}
        assert _get_serialized_field_value(item, "motif") == "exile"

    def test_motif_label_key_fallback(self):
        item = {"shot": {"annotation": {}, "motif": {"label": "revenge"}}}
        assert _get_serialized_field_value(item, "motif") == "revenge"

    def test_motif_bare_string(self):
        item = {"shot": {"annotation": {}, "motif": "redemption"}}
        assert _get_serialized_field_value(item, "motif") == "redemption"

    def test_motif_none(self):
        item = {"shot": {"annotation": {}, "motif": None}}
        assert _get_serialized_field_value(item, "motif") is None

    def test_motif_missing(self):
        item = {"shot": {"annotation": {}}}
        assert _get_serialized_field_value(item, "motif") is None

    def test_motif_empty_dict(self):
        item = {"shot": {"annotation": {}, "motif": {}}}
        # All keys absent → None (falsy or missing)
        assert _get_serialized_field_value(item, "motif") is None


# ---------------------------------------------------------------------------
# serialize_annotation_item — acceptance-criteria example
# ---------------------------------------------------------------------------

class TestSerializeAnnotationItem:
    def test_full_example_spec(self):
        """Acceptance criteria from the spec."""
        result = serialize_annotation_item(FULL_ITEM, MAPPING_WITH_MOTIF)
        expected = (
            "description: A rider crosses a snowy forest. | "
            "motif: shelter | "
            "setting: forest | "
            "animals: horse | "
            "action: riding"
        )
        assert result == expected

    def test_missing_motif_skip_empty(self):
        item = {
            "shot": {
                "annotation": {
                    "description": "A still room.",
                    "setting": "interior",
                },
            }
        }
        result = serialize_annotation_item(item, MAPPING_WITH_MOTIF)
        assert "motif" not in result
        assert "description: A still room." in result

    def test_missing_motif_no_crash(self):
        """Missing motif must not raise an exception."""
        item = {"shot": {"annotation": {"description": "Empty shot."}}}
        result = serialize_annotation_item(item, MAPPING_WITH_MOTIF)
        assert isinstance(result, str)

    def test_missing_motif_include_when_skip_empty_false(self):
        mapping = {**MAPPING_WITH_MOTIF, "skip_empty": False}
        item = {"shot": {"annotation": {"description": "A field."}}}
        result = serialize_annotation_item(item, mapping)
        # motif label present with empty value
        assert "motif: " in result

    def test_list_field_joined(self):
        item = {
            "shot": {
                "annotation": {"animals": ["horse", "dog"]},
                "motif": {"value": "chase"},
            }
        }
        mapping = {
            "fields": ["animals", "motif"],
            "include_labels": True,
            "separator": " | ",
            "skip_empty": True,
        }
        result = serialize_annotation_item(item, mapping)
        assert result == "animals: horse, dog | motif: chase"

    def test_include_labels_false(self):
        item = {
            "shot": {
                "annotation": {"setting": "forest"},
                "motif": {"value": "exile"},
            }
        }
        mapping = {
            "fields": ["setting", "motif"],
            "include_labels": False,
            "separator": " | ",
            "skip_empty": True,
        }
        result = serialize_annotation_item(item, mapping)
        assert result == "forest | exile"

    def test_empty_annotation_all_skip(self):
        item = {"shot": {"annotation": {}}}
        result = serialize_annotation_item(item, MAPPING_WITH_MOTIF)
        assert result == ""

    def test_separator_respected(self):
        item = {
            "shot": {
                "annotation": {"description": "X", "setting": "Y"},
                "motif": {"value": "Z"},
            }
        }
        mapping = {
            "fields": ["description", "motif", "setting"],
            "include_labels": False,
            "separator": " // ",
            "skip_empty": True,
        }
        result = serialize_annotation_item(item, mapping)
        assert result == "forest | exile"

    def test_empty_annotation_all_skip(self):
        item = {"shot": {"annotation": {}}}
        result = serialize_annotation_item(item, MAPPING_WITH_MOTIF)
        assert result == ""

    def test_separator_respected(self):
        item = {
            "shot": {
                "annotation": {"description": "X", "setting": "Y"},
                "motif": {"value": "Z"},
            }
        }
        mapping = {
            "fields": ["description", "motif", "setting"],
            "include_labels": False,
            "separator": " // ",
            "skip_empty": True,
        }
        result = serialize_annotation_item(item, mapping)
        assert result == "X // Z // Y"

    def test_canonical_string_motif(self):
        """Canonical form: shot.motif is a plain string after attach."""
        result = serialize_annotation_item(FULL_ITEM_STRING_MOTIF, MAPPING_WITH_MOTIF)
        expected = (
            "description: A rider crosses a snowy forest. | "
            "motif: shelter | "
            "setting: forest | "
            "animals: horse | "
            "action: riding"
        )
        assert result == expected

    def test_canonical_string_equals_dict_form(self):
        """Both forms produce identical output."""
        r_str  = serialize_annotation_item(FULL_ITEM_STRING_MOTIF, MAPPING_WITH_MOTIF)
        r_dict = serialize_annotation_item(FULL_ITEM_DICT_MOTIF,   MAPPING_WITH_MOTIF)
        assert r_str == r_dict


# ---------------------------------------------------------------------------
# Motif attach logic
# ---------------------------------------------------------------------------

import json
import tempfile
from pathlib import Path


def _make_annotation_json(tmp: Path, entries: list) -> Path:
    p = tmp / "test.json"
    p.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return p


def _make_motif_sidecar(tmp: Path, shots: list) -> Path:
    """Create a minimal data/motifs/movie/test.json sidecar."""
    d = tmp / "data" / "motifs" / "movie"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "test.json"
    p.write_text(json.dumps({"filename": "test.mp4", "shots": shots}), indent=2)
    return p


def _make_project(tmp: Path, entries: list, sidecar_shots: list) -> tuple[Path, str]:
    """Set up a minimal project with annotation JSON and motif sidecar."""
    # annotation JSON
    ann_dir = tmp / "data" / "annotations" / "shots" / "movie"
    ann_dir.mkdir(parents=True, exist_ok=True)
    ann_path = ann_dir / "test.json"
    ann_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    # motif sidecar
    motif_dir = tmp / "data" / "motifs" / "movie"
    motif_dir.mkdir(parents=True, exist_ok=True)
    motif_path = motif_dir / "test.json"
    motif_path.write_text(
        json.dumps({"filename": "test.mp4", "shots": sidecar_shots}, indent=2),
        encoding="utf-8",
    )

    return tmp, "test.mp4"


def _read_annotation(tmp: Path) -> list:
    return json.loads(
        (tmp / "data" / "annotations" / "shots" / "movie" / "test.json")
        .read_text(encoding="utf-8")
    )


class TestAttachMotifs:
    def _make_entry(self, shot_id: str, motif=None) -> dict:
        entry = {
            "shot": {
                "shot_id": shot_id,
                "annotation": {"description": f"Shot {shot_id}."},
            }
        }
        if motif is not None:
            entry["shot"]["motif"] = motif
        return entry

    def test_adds_missing_motif(self):
        from data.motif import attach_motifs_to_annotation
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            entries = [self._make_entry("shot_001")]
            sidecar = [{"shot_id": "shot_001", "value": "riding"}]
            project_path, filename = _make_project(tmp, entries, sidecar)

            r = attach_motifs_to_annotation(str(project_path), filename, "movie")
            assert r["added"] == 1
            assert r["unchanged"] == 0
            assert r["conflicts"] == 0

            saved = _read_annotation(tmp)
            assert saved[0]["shot"]["motif"] == "riding"

    def test_skips_unchanged_motif(self):
        from data.motif import attach_motifs_to_annotation
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            entries = [self._make_entry("shot_001", motif="riding")]
            sidecar = [{"shot_id": "shot_001", "value": "riding"}]
            project_path, filename = _make_project(tmp, entries, sidecar)

            r = attach_motifs_to_annotation(str(project_path), filename, "movie")
            assert r["added"] == 0
            assert r["unchanged"] == 1
            assert r["conflicts"] == 0

    def test_reports_conflict_without_force(self):
        from data.motif import attach_motifs_to_annotation
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            entries = [self._make_entry("shot_001", motif="exile")]
            sidecar = [{"shot_id": "shot_001", "value": "riding"}]
            project_path, filename = _make_project(tmp, entries, sidecar)

            r = attach_motifs_to_annotation(str(project_path), filename, "movie")
            assert r["conflicts"] == 1
            assert r["added"] == 0

            # File must not be modified
            saved = _read_annotation(tmp)
            assert saved[0]["shot"]["motif"] == "exile"

    def test_overwrites_conflict_with_force(self):
        from data.motif import attach_motifs_to_annotation
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            entries = [self._make_entry("shot_001", motif="exile")]
            sidecar = [{"shot_id": "shot_001", "value": "riding"}]
            project_path, filename = _make_project(tmp, entries, sidecar)

            r = attach_motifs_to_annotation(str(project_path), filename, "movie", force=True)
            assert r["conflicts"] == 1  # still counted as conflict
            assert r["added"] == 0

            saved = _read_annotation(tmp)
            assert saved[0]["shot"]["motif"] == "riding"

    def test_dry_run_does_not_write(self):
        from data.motif import attach_motifs_to_annotation
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            entries = [self._make_entry("shot_001")]
            sidecar = [{"shot_id": "shot_001", "value": "riding"}]
            project_path, filename = _make_project(tmp, entries, sidecar)

            r = attach_motifs_to_annotation(str(project_path), filename, "movie", dry_run=True)
            assert r["added"] == 1

            saved = _read_annotation(tmp)
            assert "motif" not in saved[0]["shot"]

    def test_missing_sidecar_counts_as_missing(self):
        from data.motif import attach_motifs_to_annotation
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            entries = [self._make_entry("shot_001")]
            # Empty sidecar — no motif for this shot
            project_path, filename = _make_project(tmp, entries, sidecar_shots=[])

            r = attach_motifs_to_annotation(str(project_path), filename, "movie")
            assert r["missing"] == 1
            assert r["added"] == 0

