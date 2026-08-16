import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

import cli as cli_mod
import data.palette as palette_mod
from data.palette import (
    create_thumbnail_palette,
    create_thumbnail_palettes_for_all,
    get_palette_path,
    get_thumbnail_palette_path,
    load_palette,
    load_thumbnail_palette,
    save_palette,
)


FOREGROUND = {
    "rgb": [30, 60, 210],
    "lab": [35.0, 20.0, -60.0],
    "luminance": 0.35,
    "chroma": 0.63,
    "palette": [],
    "coverage": 0.25,
}
BACKGROUND = {
    "rgb": [190, 40, 20],
    "lab": [45.0, 55.0, 45.0],
    "luminance": 0.45,
    "chroma": 0.71,
    "palette": [],
    "coverage": 0.75,
}
DIAGNOSTICS = {"fg_bg_delta_e": 90.0, "rescue_applied": False}


@pytest.fixture(autouse=True)
def compatible_sam(monkeypatch):
    segmenter = SimpleNamespace(segment_palette=lambda _image: [])
    monkeypatch.setattr(
        palette_mod,
        "_load_palette_segmenter",
        lambda _project_path, _verbose=False: segmenter,
    )


@pytest.fixture
def extractor(monkeypatch):
    mock = MagicMock(return_value=(FOREGROUND, BACKGROUND, DIAGNOSTICS))
    monkeypatch.setattr(palette_mod, "_extract_fg_bg_full", mock)
    return mock


def _parse(command: str):
    return cli_mod.build_parser().parse_args(command.split())


def _metadata(
    filename="The Searchers (1956).mp4",
    media_type="movie",
    media_id="tmdb_3114",
):
    return {
        "filename": filename,
        "media_type": media_type,
        "media_id": media_id,
        "title": "The Searchers",
        "year": 1956,
        "tmdb": "3114",
    }


def _write_thumbnail(project: Path, metadata: dict, color=(160, 80, 30)) -> Path:
    destination = (
        project
        / "media"
        / "thumbnails"
        / metadata["media_type"]
        / f"{Path(metadata['filename']).stem}.jpg"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(destination)
    return destination


def _create_args(**overrides):
    values = {
        "media": "movie",
        "force": False,
        "verbose": False,
        "notify_items": False,
        "notify": False,
        "all": False,
        "movie": None,
        "tmdb": None,
        "thumbnail": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _get_args(**overrides):
    values = {
        "media": "movie",
        "all": False,
        "movie": None,
        "tmdb": None,
        "shot": None,
        "thumbnail": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _batch_summary(media_type):
    return {
        "media_type": media_type,
        "total_files": 0,
        "total_processed": 0,
        "total_skipped": 0,
        "total_failed": 0,
        "total_cached": 0,
        "results": [],
    }


def test_thumbnail_flag_parses():
    assert _parse("index palette create --thumbnail").thumbnail is True


def test_thumbnail_title_parses():
    args = _parse("index palette create --thumbnail --title Searchers")
    assert args.thumbnail is True
    assert args.movie == "Searchers"


def test_thumbnail_tmdb_parses():
    args = _parse("index palette create --thumbnail --tmdb 12345")
    assert args.thumbnail is True
    assert args.tmdb == 12345


def test_thumbnail_all_parses():
    args = _parse("index palette create --thumbnail --all")
    assert args.thumbnail is True
    assert args.all is True


def test_thumbnail_all_movie_parses():
    args = _parse("index palette create --thumbnail --all --media movie")
    assert args.all is True
    assert args.media == "movie"


def test_thumbnail_all_gameplay_parses():
    args = _parse("index palette create --thumbnail --all --media gameplay")
    assert args.all is True
    assert args.media == "gameplay"


def test_thumbnail_get_parses_with_existing_selectors():
    args = _parse("index palette get --thumbnail --title Searchers --media movie")
    assert args.thumbnail is True
    assert args.movie == "Searchers"
    assert args.media == "movie"


def test_thumbnail_palette_uses_exact_metadata_viewer_source(tmp_path, extractor):
    from visualizers.metadata_visualizer import _resolve_thumbnail

    metadata = _metadata()
    expected = _write_thumbnail(tmp_path, metadata)
    assert _resolve_thumbnail(
        str(tmp_path), metadata["media_type"], metadata["filename"]
    ) == expected

    create_thumbnail_palette(
        str(tmp_path), metadata["filename"], metadata["media_type"],
        metadata=metadata,
    )

    assert extractor.call_args.args[0] == expected


def test_thumbnail_palette_does_not_read_best_frames(
    tmp_path, extractor, monkeypatch,
):
    metadata = _metadata()
    _write_thumbnail(tmp_path, metadata)
    monkeypatch.setattr(
        palette_mod,
        "load_annotation_items",
        lambda *_args, **_kwargs: pytest.fail("best-frame annotations were read"),
    )

    create_thumbnail_palette(
        str(tmp_path), metadata["filename"], metadata["media_type"],
        metadata=metadata,
    )

    extractor.assert_called_once()


def test_movie_thumbnail_palette_is_stored_under_movie_identity(tmp_path, extractor):
    metadata = _metadata()
    _write_thumbnail(tmp_path, metadata)

    create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata,
    )

    cached = load_thumbnail_palette(str(tmp_path), metadata["media_id"], "movie")
    assert cached["media"]["media_type"] == "movie"
    assert cached["media"]["media_id"] == metadata["media_id"]


def test_gameplay_thumbnail_palette_is_stored_under_gameplay_identity(
    tmp_path, extractor,
):
    metadata = _metadata(
        filename="RDR2 Session.mp4",
        media_type="gameplay",
        media_id="game_rdr2_abc12345",
    )
    _write_thumbnail(tmp_path, metadata)

    create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "gameplay", metadata=metadata,
    )

    cached = load_thumbnail_palette(
        str(tmp_path), metadata["media_id"], "gameplay"
    )
    assert cached["media"]["media_type"] == "gameplay"
    assert cached["media"]["media_id"] == metadata["media_id"]


def test_movie_and_gameplay_thumbnail_palettes_cannot_overwrite_each_other(
    tmp_path, extractor,
):
    movie = _metadata(filename="shared.mp4", media_id="shared_id")
    gameplay = _metadata(
        filename="shared.mp4", media_type="gameplay", media_id="shared_id"
    )
    _write_thumbnail(tmp_path, movie, (200, 40, 20))
    _write_thumbnail(tmp_path, gameplay, (20, 40, 200))

    create_thumbnail_palette(str(tmp_path), "shared.mp4", "movie", metadata=movie)
    create_thumbnail_palette(
        str(tmp_path), "shared.mp4", "gameplay", metadata=gameplay
    )

    movie_path = get_thumbnail_palette_path(str(tmp_path), "shared_id", "movie")
    gameplay_path = get_thumbnail_palette_path(
        str(tmp_path), "shared_id", "gameplay"
    )
    assert movie_path != gameplay_path
    assert movie_path.exists()
    assert gameplay_path.exists()


def test_thumbnail_palette_reuses_existing_figure_ground_result(tmp_path, extractor):
    metadata = _metadata()
    _write_thumbnail(tmp_path, metadata)

    create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata,
    )

    cached = load_thumbnail_palette(str(tmp_path), metadata["media_id"], "movie")
    assert cached["method"] == "figure"
    assert cached["thumbnail"]["foreground"] == FOREGROUND
    assert cached["thumbnail"]["background"] == BACKGROUND
    assert cached["thumbnail"]["diagnostics"] == DIAGNOSTICS


def test_existing_best_frame_palette_is_unchanged(tmp_path, extractor):
    metadata = _metadata()
    _write_thumbnail(tmp_path, metadata)
    best_frame_document = {
        "movie": {"filename": metadata["filename"]},
        "source": "best_frame",
        "shots": [{"shot_index": 0, "foreground": {"rgb": [1, 2, 3]}}],
    }
    save_palette(str(tmp_path), metadata["filename"], "movie", best_frame_document)

    create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata,
    )

    assert load_palette(str(tmp_path), metadata["filename"], "movie") == best_frame_document


def test_thumbnail_palette_is_stored_separately_from_best_frame_palette(
    tmp_path, extractor,
):
    metadata = _metadata()
    _write_thumbnail(tmp_path, metadata)
    create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata,
    )

    assert get_thumbnail_palette_path(
        str(tmp_path), metadata["media_id"], "movie"
    ) != get_palette_path(str(tmp_path), metadata["filename"], "movie")


def test_cached_thumbnail_palette_is_reused_without_force(tmp_path, extractor):
    metadata = _metadata()
    _write_thumbnail(tmp_path, metadata)
    create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata,
    )

    summary = create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata,
    )

    assert summary["cached"] is True
    assert summary["processed"] == 0
    assert extractor.call_count == 1


def test_force_recomputes_thumbnail_palette(tmp_path, extractor):
    metadata = _metadata()
    _write_thumbnail(tmp_path, metadata)
    create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata,
    )

    summary = create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata,
        force=True,
    )

    assert summary["cached"] is False
    assert summary["processed"] == 1
    assert extractor.call_count == 2


def test_cached_thumbnail_palette_is_retrievable_through_cli_get(
    tmp_path, extractor, monkeypatch, capsys,
):
    metadata = _metadata()
    _write_thumbnail(tmp_path, metadata)
    create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata,
    )
    monkeypatch.setattr(cli_mod.prefs, "get", lambda _key: str(tmp_path))
    monkeypatch.setattr(
        "data.shotlist.resolve_filename",
        lambda *_args, **_kwargs: metadata["filename"],
    )
    monkeypatch.setattr("data.metadata.get_metadata", lambda *_args, **_kwargs: [metadata])

    cli_mod._index_palette_get(_get_args(movie="The Searchers"))

    output = json.loads(capsys.readouterr().out)
    assert output["source"] == "thumbnail"
    assert output["media"]["media_id"] == metadata["media_id"]
    assert output["thumbnail"]["foreground"] == FOREGROUND


def test_changed_thumbnail_content_invalidates_cache(tmp_path, extractor):
    metadata = _metadata()
    thumbnail = _write_thumbnail(tmp_path, metadata, (200, 20, 20))
    create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata,
    )
    Image.new("RGB", (16, 16), (20, 20, 200)).save(thumbnail)

    summary = create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata,
    )

    assert summary["cached"] is False
    assert extractor.call_count == 2


def test_invalid_thumbnail_cache_is_rebuilt_without_force(tmp_path, extractor):
    metadata = _metadata()
    _write_thumbnail(tmp_path, metadata)
    cache_path = get_thumbnail_palette_path(
        str(tmp_path), metadata["media_id"], "movie"
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{invalid json", encoding="utf-8")

    summary = create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata,
    )

    assert summary["cached"] is False
    assert load_thumbnail_palette(
        str(tmp_path), metadata["media_id"], "movie"
    )["source"] == "thumbnail"


def test_title_selector_creates_exactly_one_thumbnail_palette(
    tmp_path, monkeypatch, capsys,
):
    create_one = MagicMock(return_value={
        "filename": "The Searchers.mp4", "processed": 1,
        "skipped": 0, "failed": 0, "cached": False,
    })
    resolve = MagicMock(return_value="The Searchers.mp4")
    monkeypatch.setattr(cli_mod.prefs, "get", lambda _key: str(tmp_path))
    monkeypatch.setattr(palette_mod, "create_thumbnail_palette", create_one)
    monkeypatch.setattr("data.shotlist.resolve_filename", resolve)

    cli_mod._index_palette_create(_create_args(movie="The Searchers"))
    capsys.readouterr()

    resolve.assert_called_once_with(str(tmp_path), None, "The Searchers", "movie")
    create_one.assert_called_once()


def test_tmdb_selector_selects_expected_movie(tmp_path, monkeypatch, capsys):
    create_one = MagicMock(return_value={
        "filename": "The Searchers.mp4", "processed": 1,
        "skipped": 0, "failed": 0, "cached": False,
    })
    resolve = MagicMock(return_value="The Searchers.mp4")
    monkeypatch.setattr(cli_mod.prefs, "get", lambda _key: str(tmp_path))
    monkeypatch.setattr(palette_mod, "create_thumbnail_palette", create_one)
    monkeypatch.setattr("data.shotlist.resolve_filename", resolve)

    cli_mod._index_palette_create(_create_args(tmdb="3114"))
    capsys.readouterr()

    resolve.assert_called_once_with(str(tmp_path), "3114", None, "movie")
    assert create_one.call_args.args[1] == "The Searchers.mp4"


def test_media_movie_all_excludes_gameplay(tmp_path, monkeypatch, capsys):
    create_all = MagicMock(return_value=_batch_summary("movie"))
    monkeypatch.setattr(cli_mod.prefs, "get", lambda _key: str(tmp_path))
    monkeypatch.setattr(palette_mod, "create_thumbnail_palettes_for_all", create_all)

    cli_mod._index_palette_create(_create_args(all=True, media="movie"))
    capsys.readouterr()

    assert create_all.call_args.args[:2] == (str(tmp_path), "movie")


def test_media_gameplay_all_excludes_movies(tmp_path, monkeypatch, capsys):
    create_all = MagicMock(return_value=_batch_summary("gameplay"))
    monkeypatch.setattr(cli_mod.prefs, "get", lambda _key: str(tmp_path))
    monkeypatch.setattr(palette_mod, "create_thumbnail_palettes_for_all", create_all)

    cli_mod._index_palette_create(_create_args(all=True, media="gameplay"))
    capsys.readouterr()

    assert create_all.call_args.args[:2] == (str(tmp_path), "gameplay")


def test_all_processes_every_record_from_one_selected_metadata_collection(
    tmp_path, monkeypatch,
):
    records = [
        _metadata(filename="one.mp4", media_id="tmdb_1"),
        _metadata(filename="two.mp4", media_id="tmdb_2"),
    ]
    load_metadata = MagicMock(return_value=records)
    create_one = MagicMock(side_effect=[
        {"filename": "one.mp4", "processed": 1, "cached": False},
        {"filename": "two.mp4", "processed": 1, "cached": False},
    ])
    monkeypatch.setattr(palette_mod, "get_metadata", load_metadata)
    monkeypatch.setattr(palette_mod, "create_thumbnail_palette", create_one)

    summary = create_thumbnail_palettes_for_all(str(tmp_path), "movie")

    load_metadata.assert_called_once_with(str(tmp_path), media_type="movie")
    assert create_one.call_count == 2
    assert [call.kwargs["metadata"] for call in create_one.call_args_list] == records
    assert summary["total_files"] == 2
    assert summary["total_processed"] == 2