"""Tests for tool/prefs.py, focused on the atomic-write path.

`_user_save()`/`_project_save()` used to call `Path.write_text()` directly,
so a crash or power loss mid-write could leave `prefs.json` truncated or
corrupted. Both now go through `_atomic_write_text()`, which writes to a
same-directory temp file and swaps it in with `os.replace()`. These tests
confirm the on-disk format is unchanged and that the temp file is always
cleaned up, including when the write itself fails.
"""

import json

import pytest

from tool import prefs


@pytest.fixture
def isolated_prefs(tmp_path, monkeypatch):
    """Point the user/project prefs files at a scratch directory."""
    user_file = tmp_path / "home" / ".crossing" / "prefs.json"
    monkeypatch.setattr(prefs, "_USER_FILE", user_file)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return project_dir


class TestAtomicWriteText:
    def test_creates_file_with_exact_content(self, tmp_path):
        dest = tmp_path / "sub" / "prefs.json"
        prefs._atomic_write_text(dest, json.dumps({"a": 1}, indent=2))
        assert dest.read_text() == json.dumps({"a": 1}, indent=2)

    def test_no_temp_file_left_behind_after_success(self, tmp_path):
        dest = tmp_path / "prefs.json"
        prefs._atomic_write_text(dest, "{}")
        leftovers = [p for p in tmp_path.iterdir() if p != dest]
        assert leftovers == []

    def test_overwrites_existing_content(self, tmp_path):
        dest = tmp_path / "prefs.json"
        prefs._atomic_write_text(dest, json.dumps({"a": 1}))
        prefs._atomic_write_text(dest, json.dumps({"a": 2}))
        assert json.loads(dest.read_text()) == {"a": 2}

    def test_cleans_up_temp_file_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        dest = tmp_path / "prefs.json"
        dest.write_text(json.dumps({"a": 1}))

        def _boom(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(prefs.os, "replace", _boom)
        with pytest.raises(OSError):
            prefs._atomic_write_text(dest, json.dumps({"a": 2}))

        assert json.loads(dest.read_text()) == {"a": 1}
        leftovers = [p for p in tmp_path.iterdir() if p != dest]
        assert leftovers == []


class TestUserAndProjectSave:
    def test_user_save_load_round_trip(self, isolated_prefs):
        prefs._user_save({"path": "/some/project"})
        assert prefs._user_load() == {"path": "/some/project"}

    def test_project_save_load_round_trip(self, isolated_prefs):
        project_dir = isolated_prefs
        prefs._user_save({"path": str(project_dir)})
        prefs._project_save({"name": "Test Project"})
        assert prefs._project_load() == {"name": "Test Project"}
        pf = project_dir / "preferences" / "preferences.json"
        assert pf.read_text() == json.dumps({"name": "Test Project"}, indent=2)

    def test_project_save_noop_without_active_project(self, isolated_prefs):
        # No "path" set in user prefs yet, so there is no project file to write.
        prefs._project_save({"name": "Test Project"})
        assert prefs._project_load() == {}

    def test_public_get_set_api_unchanged(self, isolated_prefs):
        project_dir = isolated_prefs
        prefs.set("path", str(project_dir))
        prefs.set("name", "Test Project")
        assert prefs.get("path") == str(project_dir)
        assert prefs.get("name") == "Test Project"
        assert prefs.load() == {"path": str(project_dir), "name": "Test Project"}
