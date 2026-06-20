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
# Motif — obsolete attach raises RuntimeError
# ---------------------------------------------------------------------------

import json
import tempfile
from pathlib import Path


class TestAttachMotifsObsolete:
    """attach_motifs_to_annotation is obsolete; it must raise RuntimeError."""

    def test_attach_raises_runtime_error(self):
        from data.motif import attach_motifs_to_annotation
        import pytest
        with pytest.raises(RuntimeError, match="obsolete"):
            attach_motifs_to_annotation("/any/project", "test.mp4", "movie")

