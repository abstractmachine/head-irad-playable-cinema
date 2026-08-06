"""Tests for services/engraving_generate.py prompt expansion.

Covers:
- _expand_prompt: canonical variables (label, field, movie, shot_id,
  description, motif) default to empty string and are substituted from
  context; unknown placeholders are left unchanged; line_weight_hint is
  never injected into the compiled prompt.
- _expand_prompt: parity with services.engraving_generate_openai._expand_prompt
  on the shared canonical variable set (see services/engraving_prompt.py).
- build_size_context: size-aware variables computed from placement metadata.
"""

import unittest

from services.engraving_generate import _expand_prompt, build_size_context


class TestExpandPromptDefaults(unittest.TestCase):
    def test_canonical_variables_default_to_empty_string(self):
        result = _expand_prompt(
            "$label|$field|$movie|$shot_id|$description|$motif", None
        )
        self.assertEqual(result, "|||||")

    def test_context_overrides_defaults(self):
        result = _expand_prompt(
            "$label — $motif",
            {"label": "horse", "motif": "freedom"},
        )
        self.assertEqual(result, "horse — freedom")

    def test_motif_supported(self):
        # Regression test: motif is part of the canonical variable set
        # documented in services/engraving_prompt.py and must be expanded
        # by the FLUX backend just like the OpenAI backend already does.
        result = _expand_prompt("$motif", {"motif": "revolver"})
        self.assertEqual(result, "revolver")

    def test_unknown_placeholder_left_unchanged(self):
        result = _expand_prompt("$totally_unknown", {})
        self.assertEqual(result, "$totally_unknown")

    def test_line_weight_default(self):
        result = _expand_prompt("$line_weight", None)
        self.assertEqual(result, "1.0")

    def test_line_weight_hint_never_injected(self):
        result = _expand_prompt(
            "$line_weight_hint", {"line_weight_hint": "heavy — bold contours"}
        )
        # Explicitly excluded — must remain un-expanded, not leak into prompt.
        self.assertEqual(result, "$line_weight_hint")

    def test_size_aware_variables_pass_through_context(self):
        result = _expand_prompt("$size_class $object_width_mm", {"size_class": "large", "object_width_mm": "42.0"})
        self.assertEqual(result, "large 42.0")


class TestBuildSizeContext(unittest.TestCase):
    def test_computes_mm_and_size_class(self):
        ctx = build_size_context(
            preprocessing_size=[400, 300],
            preprocess_dpi=300,
            page_pt_w=600.0,
            page_pt_h=800.0,
            width_frac=0.5,
            height_frac=0.5,
        )
        self.assertEqual(ctx["object_width_px"], "400")
        self.assertEqual(ctx["object_height_px"], "300")
        self.assertEqual(ctx["page_dpi"], "300")
        self.assertIn(ctx["size_class"], {"large", "medium", "small", "very_small"})

    def test_safe_fallback_on_missing_values(self):
        ctx = build_size_context(
            preprocessing_size=None,
            preprocess_dpi=0,
            page_pt_w=0.0,
            page_pt_h=0.0,
            width_frac=0.0,
            height_frac=0.0,
        )
        self.assertEqual(ctx["object_width_mm"], "")
        self.assertEqual(ctx["object_width_px"], "")
        # longest_mm is 0.0, which still matches the "very_small" threshold
        # (>= 0.0) — "unknown" is only reachable if no threshold matches.
        self.assertEqual(ctx["size_class"], "very_small")


if __name__ == "__main__":
    unittest.main()
