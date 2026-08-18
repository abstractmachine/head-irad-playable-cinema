"""Palette System 2.0 research laboratory (2026-08).

A disposable experimental path that wraps the existing production palette
components in a hybrid Qwen -> SAM3 -> SEEDS -> LAB/Ward -> Qwen loop and
writes browsable visual artifacts under
``<project>/outputs/tests/palette-system-<date>/``.

This package deliberately does NOT touch the production palette pipeline in
``data/palette.py``.  It imports from it.

Run with::

    uv run python -m scripts.palette_lab.run_experiment --media game_rdr2_ce5e0bba
"""
