import tempfile
import unittest
from pathlib import Path

from data.subtitles import (
    Cue,
    active_subtitle,
    load_subtitle_cues,
    subtitle_exists,
    subtitle_path_for,
    _parse_srt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(tmp: Path, media_type: str, filename: str, srt_content: str | None = None) -> Path:
    """Create a minimal project tree under *tmp*."""
    sub_dir = tmp / "media" / "subtitles" / media_type
    sub_dir.mkdir(parents=True)
    if srt_content is not None:
        stem = Path(filename).stem
        (sub_dir / f"{stem}.srt").write_text(srt_content, encoding="utf-8")
    return tmp


_SIMPLE_SRT = """\
1
00:00:01,000 --> 00:00:03,000
Hello, world!

2
00:00:05,500 --> 00:00:08,000
Second line.

3
00:00:10,000 --> 00:00:12,000
Third cue.
"""

_HTML_SRT = """\
1
00:00:01,000 --> 00:00:02,500
<i>Italic text</i>

2
00:00:04,000 --> 00:00:06,000
<b>Bold</b> and <i>italic</i>
"""

_NO_SEQUENCE_SRT = """\
00:00:01,000 --> 00:00:03,000
No sequence number
"""

_MALFORMED_SRT = """\
this is not a subtitle
just random text

and more random text
"""


# ---------------------------------------------------------------------------
# Path resolution tests
# ---------------------------------------------------------------------------

class TestSubtitlePathResolution(unittest.TestCase):
    def test_canonical_path_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp), "movies", "My Film.mp4", _SIMPLE_SRT)
            result = subtitle_path_for(str(project), "movies", "My Film.mp4")
            self.assertIsNotNone(result)
            self.assertEqual(result.name, "My Film.srt")

    def test_legacy_dash_path_found(self):
        """The legacy dash-separated filename should be found when the canonical doesn't exist."""
        with tempfile.TemporaryDirectory() as tmp:
            sub_dir = Path(tmp) / "media" / "subtitles" / "movies"
            sub_dir.mkdir(parents=True)
            (sub_dir / "My-Film.srt").write_text(_SIMPLE_SRT, encoding="utf-8")
            result = subtitle_path_for(str(tmp), "movies", "My Film.mp4")
            self.assertIsNotNone(result)
            self.assertEqual(result.name, "My-Film.srt")

    def test_canonical_preferred_over_legacy(self):
        """Canonical (space) name takes priority when both exist."""
        with tempfile.TemporaryDirectory() as tmp:
            sub_dir = Path(tmp) / "media" / "subtitles" / "movies"
            sub_dir.mkdir(parents=True)
            (sub_dir / "My Film.srt").write_text(_SIMPLE_SRT, encoding="utf-8")
            (sub_dir / "My-Film.srt").write_text(_SIMPLE_SRT, encoding="utf-8")
            result = subtitle_path_for(str(tmp), "movies", "My Film.mp4")
            self.assertIsNotNone(result)
            self.assertEqual(result.name, "My Film.srt")

    def test_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(Path(tmp), "movies", "My Film.mp4")  # no SRT written
            result = subtitle_path_for(str(tmp), "movies", "My Film.mp4")
            self.assertIsNone(result)

    def test_subtitle_exists_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp), "movies", "Film.mp4", _SIMPLE_SRT)
            self.assertTrue(subtitle_exists(str(project), "movies", "Film.mp4"))

    def test_subtitle_exists_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp), "movies", "Film.mp4")
            self.assertFalse(subtitle_exists(str(project), "movies", "Film.mp4"))

    def test_different_media_type(self):
        """Subtitle in 'movies' dir must not match 'gameplay' lookup."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(Path(tmp), "movies", "Film.mp4", _SIMPLE_SRT)
            self.assertFalse(subtitle_exists(str(tmp), "gameplay", "Film.mp4"))


# ---------------------------------------------------------------------------
# SRT parsing tests
# ---------------------------------------------------------------------------

class TestSRTParsing(unittest.TestCase):
    def test_parses_simple_srt(self):
        cues = _parse_srt(_SIMPLE_SRT)
        self.assertEqual(len(cues), 3)
        self.assertAlmostEqual(cues[0].start_secs, 1.0)
        self.assertAlmostEqual(cues[0].end_secs, 3.0)
        self.assertEqual(cues[0].text, "Hello, world!")
        self.assertAlmostEqual(cues[1].start_secs, 5.5)
        self.assertEqual(cues[1].text, "Second line.")
        self.assertAlmostEqual(cues[2].start_secs, 10.0)
        self.assertAlmostEqual(cues[2].end_secs, 12.0)
        self.assertEqual(cues[2].text, "Third cue.")

    def test_strips_html_tags(self):
        cues = _parse_srt(_HTML_SRT)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "Italic text")
        self.assertEqual(cues[1].text, "Bold and italic")

    def test_handles_no_sequence_number(self):
        cues = _parse_srt(_NO_SEQUENCE_SRT)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "No sequence number")

    def test_malformed_returns_empty(self):
        cues = _parse_srt(_MALFORMED_SRT)
        self.assertEqual(cues, [])

    def test_empty_string_returns_empty(self):
        self.assertEqual(_parse_srt(""), [])

    def test_period_separator(self):
        """Some SRT files use periods instead of commas in timestamps."""
        srt = "1\n00:00:01.000 --> 00:00:03.000\nPeriod separator\n"
        cues = _parse_srt(srt)
        self.assertEqual(len(cues), 1)
        self.assertAlmostEqual(cues[0].start_secs, 1.0)

    def test_multiline_cue(self):
        srt = "1\n00:00:01,000 --> 00:00:03,000\nLine one\nLine two\n"
        cues = _parse_srt(srt)
        self.assertEqual(len(cues), 1)
        self.assertIn("Line one", cues[0].text)
        self.assertIn("Line two", cues[0].text)


# ---------------------------------------------------------------------------
# load_subtitle_cues tests
# ---------------------------------------------------------------------------

class TestLoadSubtitleCues(unittest.TestCase):
    def test_loads_cues_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp), "movies", "Film.mp4", _SIMPLE_SRT)
            cues = load_subtitle_cues(str(project), "movies", "Film.mp4")
            self.assertEqual(len(cues), 3)

    def test_returns_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(Path(tmp), "movies", "Film.mp4")
            cues = load_subtitle_cues(str(tmp), "movies", "Film.mp4")
            self.assertEqual(cues, [])

    def test_loads_legacy_dash_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub_dir = Path(tmp) / "media" / "subtitles" / "movies"
            sub_dir.mkdir(parents=True)
            (sub_dir / "My-Film.srt").write_text(_SIMPLE_SRT, encoding="utf-8")
            cues = load_subtitle_cues(str(tmp), "movies", "My Film.mp4")
            self.assertEqual(len(cues), 3)


# ---------------------------------------------------------------------------
# active_subtitle tests
# ---------------------------------------------------------------------------

class TestActiveSubtitle(unittest.TestCase):
    def setUp(self):
        self.cues = _parse_srt(_SIMPLE_SRT)

    def test_returns_text_within_cue(self):
        self.assertEqual(active_subtitle(self.cues, 2.0), "Hello, world!")

    def test_returns_text_at_cue_start(self):
        self.assertEqual(active_subtitle(self.cues, 1.0), "Hello, world!")

    def test_returns_text_at_cue_end(self):
        self.assertEqual(active_subtitle(self.cues, 3.0), "Hello, world!")

    def test_returns_empty_between_cues(self):
        # Between cue 1 (ends 3.0) and cue 2 (starts 5.5)
        self.assertEqual(active_subtitle(self.cues, 4.0), "")

    def test_returns_empty_before_first_cue(self):
        self.assertEqual(active_subtitle(self.cues, 0.0), "")

    def test_returns_empty_after_last_cue(self):
        self.assertEqual(active_subtitle(self.cues, 99.0), "")

    def test_returns_correct_cue_for_multiple(self):
        self.assertEqual(active_subtitle(self.cues, 6.0), "Second line.")
        self.assertEqual(active_subtitle(self.cues, 11.0), "Third cue.")

    def test_empty_cues_returns_empty(self):
        self.assertEqual(active_subtitle([], 5.0), "")


if __name__ == "__main__":
    unittest.main()
