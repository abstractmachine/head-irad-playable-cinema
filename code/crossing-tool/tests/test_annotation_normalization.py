import tempfile
import unittest
from pathlib import Path

from data.annotate import normalize_label_list, _validate_annotation, _LABEL_LIST_FIELDS, load_label_list_fields
from data.index import load_atomic_fields, save_atomic_fields

# Shorthand label set used in tests that need explicit normalization behaviour
# but don't read from disk.
_ANIMALS = frozenset({"animals"})
_OBJECTS = frozenset({"objects"})
_HUMANS  = frozenset({"humans"})
_COMMON  = frozenset({"objects", "humans", "animals"})


class NormalizeLabelListTests(unittest.TestCase):

    # ------------------------------------------------------------------
    # normalize_label_list — atomic-label fields (always explicit label_fields)
    # ------------------------------------------------------------------

    def test_comma_joined_string_is_split(self):
        self.assertEqual(
            normalize_label_list(["dog, horse"], "animals", label_fields=_ANIMALS),
            ["dog", "horse"],
        )

    def test_double_quote_wrapped_label_is_unquoted(self):
        self.assertEqual(
            normalize_label_list(['"cat"'], "animals", label_fields=_ANIMALS),
            ["cat"],
        )

    def test_single_quote_wrapped_label_is_unquoted(self):
        self.assertEqual(
            normalize_label_list(["'bus'"], "objects", label_fields=_OBJECTS),
            ["bus"],
        )

    def test_quoted_comma_joined_string_is_split_and_unquoted(self):
        self.assertEqual(
            normalize_label_list(['"dog, horse"'], "animals", label_fields=_ANIMALS),
            ["dog", "horse"],
        )

    def test_whitespace_is_stripped(self):
        self.assertEqual(
            normalize_label_list(["  bird  "], "animals", label_fields=_ANIMALS),
            ["bird"],
        )

    def test_empty_tokens_after_split_are_discarded(self):
        self.assertEqual(
            normalize_label_list(["cat,"], "animals", label_fields=_ANIMALS),
            ["cat"],
        )

    def test_deduplication_preserves_first_seen_order(self):
        self.assertEqual(
            normalize_label_list(["cat", "dog", "cat", "horse"], "animals", label_fields=_ANIMALS),
            ["cat", "dog", "horse"],
        )

    def test_comma_joined_with_duplicates(self):
        self.assertEqual(
            normalize_label_list(["cat, dog", "cat"], "animals", label_fields=_ANIMALS),
            ["cat", "dog"],
        )

    def test_multiple_items_mixed(self):
        result = normalize_label_list(
            ['"dog, horse"', "cat", " bird "],
            "animals",
            label_fields=_ANIMALS,
        )
        self.assertEqual(result, ["dog", "horse", "cat", "bird"])

    def test_label_fields_controls_which_fields_are_treated(self):
        # Each field splits only when present in label_fields.
        for field in ("objects", "humans", "animals"):
            with self.subTest(field=field):
                lf = frozenset({field})
                self.assertEqual(normalize_label_list(["a, b"], field, label_fields=lf), ["a", "b"])

    # ------------------------------------------------------------------
    # normalize_label_list — no label_fields / empty set → pass-through
    # ------------------------------------------------------------------

    def test_no_label_fields_returns_list_unchanged(self):
        # With the empty default, nothing is split.
        items = ["dog, horse"]
        self.assertEqual(normalize_label_list(items, "animals"), items)

    def test_text_field_not_split_when_excluded_from_label_fields(self):
        items = ["Hello, world!", '"quoted line"']
        self.assertEqual(normalize_label_list(items, "text", label_fields=_COMMON), items)

    def test_unknown_field_is_not_split(self):
        items = ["a, b, c"]
        self.assertEqual(normalize_label_list(items, "setting"), items)

    def test_custom_label_fields_excludes_default_names(self):
        # When a custom set is passed that does NOT include 'animals', animals must not split.
        result = normalize_label_list(["dog, horse"], "animals", label_fields=frozenset({"objects"}))
        self.assertEqual(result, ["dog, horse"])

    def test_custom_label_fields_can_include_any_field(self):
        # 'creatures' is not in the built-in constant, but with custom label_fields it splits.
        result = normalize_label_list(["dog, horse"], "creatures", label_fields=frozenset({"creatures"}))
        self.assertEqual(result, ["dog", "horse"])

    # ------------------------------------------------------------------
    # _validate_annotation integration — explicit label_fields
    # ------------------------------------------------------------------

    def test_validate_annotation_splits_comma_joined_objects(self):
        ann = _validate_annotation({"objects": ["car, truck", "bus"]}, label_fields=_OBJECTS)
        self.assertEqual(ann["objects"], ["car", "truck", "bus"])

    def test_validate_annotation_strips_quotes_in_humans(self):
        ann = _validate_annotation({"humans": ['"Alice"', "'Bob'"]}, label_fields=_HUMANS)
        self.assertEqual(ann["humans"], ["Alice", "Bob"])

    def test_validate_annotation_deduplicates_label_fields(self):
        ann = _validate_annotation({"animals": ["cat", "cat, dog"]}, label_fields=_ANIMALS)
        self.assertEqual(ann["animals"], ["cat", "dog"])

    def test_validate_annotation_scalar_wrapped_in_list_for_label_field(self):
        ann = _validate_annotation({"animals": "dog, horse"}, label_fields=_ANIMALS)
        self.assertEqual(ann["animals"], ["dog", "horse"])

    def test_validate_annotation_with_custom_label_fields_splits_text(self):
        # Edge case: 'text' treated as atomic when explicitly configured.
        ann = _validate_annotation({"text": ["Hello, world"]}, label_fields=frozenset({"text"}))
        self.assertEqual(ann["text"], ["Hello", "world"])

    # ------------------------------------------------------------------
    # _validate_annotation — field-type invariants (no label_fields needed)
    # ------------------------------------------------------------------

    def test_validate_annotation_text_field_preserved_by_default(self):
        # With empty _LABEL_LIST_FIELDS, text is never split.
        items = ["Hello, world!", "Yes, I know."]
        ann = _validate_annotation({"text": items})
        self.assertEqual(ann["text"], items)

    def test_validate_annotation_setting_preserved(self):
        ann = _validate_annotation({"setting": "outdoor, sunny park"})
        self.assertEqual(ann["setting"], "outdoor, sunny park")

    def test_validate_annotation_caps_at_20(self):
        items = [f"item{i}" for i in range(25)]
        ann = _validate_annotation({"objects": items})
        self.assertEqual(len(ann["objects"]), 20)

    def test_validate_annotation_none_field_becomes_empty_list(self):
        ann = _validate_annotation({"objects": None})
        self.assertEqual(ann["objects"], [])

    def test_validate_annotation_missing_fields_get_defaults(self):
        ann = _validate_annotation({})
        self.assertEqual(ann["setting"], "")
        self.assertEqual(ann["objects"], [])
        self.assertEqual(ann["humans"], [])
        self.assertEqual(ann["animals"], [])
        self.assertEqual(ann["text"], [])

    def test_validate_annotation_no_split_without_label_fields(self):
        # Without label_fields, objects/animals/humans are NOT split — no opinionated default.
        ann = _validate_annotation({"objects": ["car, truck"], "animals": ["dog, horse"]})
        self.assertEqual(ann["objects"], ["car, truck"])
        self.assertEqual(ann["animals"], ["dog, horse"])


class AtomicFieldsYamlTests(unittest.TestCase):
    """Tests for load_label_list_fields / load_atomic_fields / save_atomic_fields."""

    def test_builtin_label_list_fields_is_empty(self):
        # No hardcoded fields — empty frozenset.
        self.assertEqual(_LABEL_LIST_FIELDS, frozenset())

    def test_load_label_list_fields_no_project_returns_empty(self):
        self.assertEqual(load_label_list_fields(""), frozenset())

    def test_load_label_list_fields_missing_yaml_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = load_label_list_fields(tmp)
            self.assertEqual(result, frozenset())

    def test_save_and_load_atomic_fields_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Need a minimal fields.yaml to exist before saving atomic fields.
            from data.index import save_fields
            save_fields(tmp, ["objects", "creatures", "vehicles"])
            save_atomic_fields(tmp, ["objects", "creatures", "vehicles"])
            loaded = load_atomic_fields(tmp)
            self.assertEqual(loaded, ["objects", "creatures", "vehicles"])

    def test_save_atomic_fields_preserves_fields_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            from data.index import save_fields, load_fields
            save_fields(tmp, ["type", "setting", "animals"])
            save_atomic_fields(tmp, ["animals"])
            # fields key must survive save_atomic_fields
            self.assertEqual(load_fields(tmp), ["type", "setting", "animals"])

    def test_save_fields_preserves_atomic_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            from data.index import save_fields
            save_fields(tmp, ["type", "setting"])
            save_atomic_fields(tmp, ["animals"])
            # now update fields — atomic should survive
            save_fields(tmp, ["type", "setting", "objects"])
            self.assertEqual(load_atomic_fields(tmp), ["animals"])

    def test_load_label_list_fields_reads_atomic_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            from data.index import save_fields
            save_fields(tmp, ["animals", "objects"])
            save_atomic_fields(tmp, ["creatures", "vehicles"])
            result = load_label_list_fields(tmp)
            self.assertEqual(result, frozenset({"creatures", "vehicles"}))

    def test_load_atomic_fields_returns_empty_list_when_key_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            from data.index import save_fields
            save_fields(tmp, ["type", "setting"])
            # No atomic key written — should return []
            self.assertEqual(load_atomic_fields(tmp), [])

    def test_load_atomic_fields_raises_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                load_atomic_fields(tmp)

    def test_save_atomic_fields_creates_fields_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            # save_atomic_fields must create fields.yaml (and the directory)
            save_atomic_fields(tmp, ["objects"])
            expected = Path(tmp) / "preferences" / "data" / "fields.yaml"
            self.assertTrue(expected.exists())

    def test_normalize_with_project_atomic_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            from data.index import save_fields
            save_fields(tmp, ["animals"])
            save_atomic_fields(tmp, ["animals"])
            lf = load_label_list_fields(tmp)
            result = normalize_label_list(["dog, horse"], "animals", label_fields=lf)
            self.assertEqual(result, ["dog", "horse"])


if __name__ == "__main__":
    unittest.main()
