import json
import sqlite3

import pytest

from test_silhouette_archive import _project
from services.silhouette_archive import plan_archive
from services.silhouette_archive_execute import initialize, preflight, execute, status, select_pilot
from services.silhouette_legacy_allocation import prepare_registry, publish_registry


def _ready(tmp_path, count=3, media_type='movie', retired_only=False):
    project = _project(tmp_path, count=count, media_type=media_type)
    if retired_only:
        census_path = project / 'outputs/tests/silhouette-identity-migration/census.sqlite3'
        with sqlite3.connect(census_path) as database:
            for metadata, png in database.execute("SELECT metadata_path,png_path FROM records WHERE assignment!='superseded'"):
                for relative in (metadata, png):
                    (project / relative).unlink()
                    database.execute('DELETE FROM files WHERE path=?', (relative,))
            database.execute("DELETE FROM records WHERE assignment!='superseded'")
    lock = project / 'data/silhouettes/.catalog-extraction.lock'
    lock.touch()
    census = project / 'outputs/tests/silhouette-identity-migration/census.sqlite3'
    from services.silhouette_identity_census import fingerprint
    info, _ = fingerprint(lock)
    with sqlite3.connect(census) as database:
        database.execute('INSERT INTO files(path,domain,size,mtime_ns,sha256,kind) VALUES(?,?,?,?,?,?)', (lock.relative_to(project).as_posix(), 'data/silhouettes', info['size'], info['mtime_ns'], info['sha256'], info['kind']))
    plan_archive(project)
    prepared = tmp_path / 'registry.sqlite3'
    prepare_registry(project, census, prepared)
    publish_registry(project, census, prepared)
    plan_archive(project, version='v3-test', revalidate=True, preparation=True)
    initialize(project, 'v3-test')
    from services.silhouette_archive_execute import protected_baseline
    protected_baseline(project, 'v3-test')
    with sqlite3.connect(project / 'outputs/tests/silhouette-identity-migration/archival-execution/v3-test/journal.sqlite3') as database:
        database.execute("INSERT INTO events VALUES('fixture','pilot_passed','synthetic executor unit fixture')")
    return project


def test_move_verify_rollback_resume_preserves_active(tmp_path):
    project = _ready(tmp_path)
    active = project / 'data/silhouettes/catalog/movie/Film/coat/object_0004.json'
    before = active.read_bytes()
    preflight(project, 'v3-test')
    assert execute(project, 'v3-test')['verified_files'] == 6
    assert execute(project, 'v3-test', verify_only=True)['verified_files'] == 6
    assert execute(project, 'v3-test')['verified_files'] == 6
    execute(project, 'v3-test', rollback=True)
    assert status(project, 'v3-test')['states'] == {'rolled_back': 6}
    assert execute(project, 'v3-test')['verified_files'] == 6
    assert active.read_bytes() == before


@pytest.mark.parametrize('point', ['before_rename', 'after_rename', 'after_verify', 'before_commit'])
def test_crash_boundary_resume(tmp_path, point):
    project = _ready(tmp_path, count=1)
    preflight(project, 'v3-test')
    def crash(stage, row):
        if stage == point:
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        execute(project, 'v3-test', fault=crash)
    assert execute(project, 'v3-test')['verified_files'] == 2


@pytest.mark.parametrize('issue', ['missing', 'bytes', 'metadata', 'collision', 'symlink'])
def test_preflight_blocks_bad_source(tmp_path, issue):
    project = _ready(tmp_path, count=1)
    source = project / 'data/silhouettes/catalog/movie/Film/coat/object_0001.png'
    destination = project / 'archive/silhouettes/catalog/movie/Film/coat/object_0001.png'
    if issue == 'missing':
        source.unlink()
    elif issue == 'bytes':
        source.write_bytes(b'changed')
    elif issue == 'metadata':
        source.touch()
    elif issue == 'collision':
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b'collision')
    else:
        source.unlink()
        source.symlink_to(project / 'other')
    with pytest.raises((ValueError, OSError)):
        preflight(project, 'v3-test')


@pytest.mark.parametrize('issue', ['collision', 'changed'])
def test_rollback_refuses_destructive_recovery(tmp_path, issue):
    project = _ready(tmp_path, count=1)
    preflight(project, 'v3-test')
    execute(project, 'v3-test')
    source = project / 'data/silhouettes/catalog/movie/Film/coat/object_0001.json'
    archived = project / 'archive/silhouettes/catalog/movie/Film/coat/object_0001.json'
    if issue == 'collision':
        source.write_text('collision')
    else:
        archived.write_text('changed')
    with pytest.raises(ValueError):
        execute(project, 'v3-test', rollback=True)


def test_immutable_plan_digest_required(tmp_path):
    project = _ready(tmp_path, count=1)
    summary = project / 'outputs/tests/silhouette-archive/v3-test/summary.json'
    summary.write_text(summary.read_text() + ' ')
    with pytest.raises(ValueError, match='digest'):
        preflight(project, 'v3-test')


def test_pending_destination_is_not_adopted(tmp_path):
    project = _ready(tmp_path, count=1)
    preflight(project, 'v3-test')
    source = project / 'data/silhouettes/catalog/movie/Film/coat/object_0001.png'
    destination = project / 'archive/silhouettes/catalog/movie/Film/coat/object_0001.png'
    destination.parent.mkdir(parents=True)
    source.rename(destination)
    with pytest.raises(FileExistsError):
        execute(project, 'v3-test')


def test_changed_lifecycle_blocks_move(tmp_path):
    project = _ready(tmp_path, count=1)
    preflight(project, 'v3-test')
    source = project / 'data/silhouettes/catalog/movie/Film/coat/object_0001.json'
    record = json.loads(source.read_text())
    record['assignment']['state'] = 'active'
    source.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        execute(project, 'v3-test')


def test_pilot_selection_rollback_and_explicit_resolution(tmp_path):
    from services.silhouette_archive_reference import resolve_silhouette_reference
    project = _ready(tmp_path)
    preflight(project, 'v3-test')
    selected = select_pilot(project, 'v3-test', count=2)
    assert len(selected) == 2
    execute(project, 'v3-test', pilot=True)
    lookup = project / 'outputs/tests/silhouette-archive/v3-test/plan.sqlite3'
    assert resolve_silhouette_reference(project, selected[0], include_archive=True, lookup=lookup)['status'] == 'archived'
    execute(project, 'v3-test', pilot=True, rollback=True)
    assert (project / selected[0]).exists()


def test_complete_population_and_protected_hashes(tmp_path):
    from services.silhouette_archive_execute import verify_population, verify_protected
    project = _ready(tmp_path)
    preflight(project, 'v3-test')
    execute(project, 'v3-test')
    result = verify_population(project, 'v3-test')
    assert result['working'] == {'active': 1, 'inactive': 1}
    assert result['archive_superseded'] == 3
    assert verify_protected(project, 'v3-test')['fresh_hashes_matched'] == 6


def test_unexpected_sidecar_blocks_preflight(tmp_path):
    project = _ready(tmp_path)
    (project / 'data/silhouettes/catalog/movie/Film/coat/unexpected.txt').write_text('not selected')
    with pytest.raises(ValueError, match='uncensused'):
        preflight(project, 'v3-test')


def test_protected_change_is_never_accepted(tmp_path):
    from services.silhouette_archive_execute import verify_protected
    project = _ready(tmp_path)
    source = project / 'data/silhouettes/catalog/movie/Film/coat/object_0004.png'
    source.write_bytes(b'changed')
    with pytest.raises(ValueError, match='Protected'):
        verify_protected(project, 'v3-test')


@pytest.mark.parametrize('point', ['before_rename', 'after_rename', 'after_verify', 'before_commit'])
def test_rollback_crash_can_resume(tmp_path, point):
    project = _ready(tmp_path, count=1)
    preflight(project, 'v3-test')
    execute(project, 'v3-test')
    def crash(stage, row):
        if stage == point:
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        execute(project, 'v3-test', rollback=True, fault=crash)
    assert execute(project, 'v3-test', rollback=True)['states'] == {'rolled_back': 2}
    assert execute(project, 'v3-test')['verified_files'] == 2


def test_missing_both_locations_fails_closed(tmp_path):
    project = _ready(tmp_path, count=1)
    preflight(project, 'v3-test')
    (project / 'data/silhouettes/catalog/movie/Film/coat/object_0001.png').unlink()
    with pytest.raises(ValueError, match='missing'):
        execute(project, 'v3-test')


def test_partial_move_can_roll_back_without_moving_remaining(tmp_path):
    project = _ready(tmp_path, count=3)
    preflight(project, 'v3-test')
    def crash(stage, row):
        if stage == 'after_rename':
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        execute(project, 'v3-test', batch_size=2, fault=crash)
    result = execute(project, 'v3-test', rollback=True)
    assert result['states'] == {'rolled_back': 6}


def test_retained_candidate_plan_is_immutable(tmp_path):
    project = _ready(tmp_path)
    path = project / 'outputs/tests/silhouette-archive/summary.json'
    path.write_text(path.read_text() + ' ')
    with pytest.raises(ValueError, match='digest'):
        preflight(project, 'v3-test')


def test_full_operation_requires_successful_pilot(tmp_path):
    project = _ready(tmp_path)
    preflight(project, 'v3-test')
    with sqlite3.connect(project / 'outputs/tests/silhouette-identity-migration/archival-execution/v3-test/journal.sqlite3') as database:
        database.execute("DELETE FROM events WHERE event='pilot_passed'")
    with pytest.raises(ValueError, match='pilot'):
        execute(project, 'v3-test')


def test_journaled_pilot_rehearsal(tmp_path):
    from services.silhouette_archive_execute import run_pilot
    project = _ready(tmp_path)
    preflight(project, 'v3-test')
    select_pilot(project, 'v3-test', count=2)
    result = run_pilot(project, 'v3-test')
    assert result['status'] == 'PASS'
    assert result['gates']['original_hashes_and_metadata_restored'] == 4


def test_pruning_preserves_catalog_root_and_nonempty_namespace(tmp_path):
    from services.silhouette_archive_execute import prune_empty, verify_population
    project = _ready(tmp_path)
    preflight(project, 'v3-test')
    execute(project, 'v3-test')
    verify_population(project, 'v3-test')
    prune_empty(project, 'v3-test')
    assert (project / 'data/silhouettes/catalog').is_dir()
    assert (project / 'data/silhouettes/catalog/movie/Film/coat').is_dir()
    execute(project, 'v3-test', rollback=True)
    assert (project / 'data/silhouettes/catalog/movie/Film/coat/object_0001.png').exists()


@pytest.mark.parametrize('media', ['movie', 'gameplay'])
def test_both_media_active_index_and_explicit_history_after_archival(tmp_path, media):
    from services.illustration_index import rebuild_index, index_path
    from services.silhouette_catalog import iter_catalog
    from services.silhouette_archive_reference import resolve_silhouette_reference, ArchiveReferenceError
    project = _ready(tmp_path, media_type=media)
    preflight(project, 'v3-test')
    execute(project, 'v3-test')
    assert len(list(iter_catalog(str(project), media))) == 1
    assert len(list(iter_catalog(str(project), media, include_history=True))) == 2
    assert rebuild_index(project, 'silhouettes', media)['count'] == 1
    with sqlite3.connect(index_path(project, 'silhouettes', media)) as database:
        assert database.execute('SELECT assignment_state FROM records').fetchall() == [('active',)]
    original = f'data/silhouettes/catalog/{media}/Film/coat/object_0001.json'
    with pytest.raises(ArchiveReferenceError):
        resolve_silhouette_reference(project, original)
    lookup = project / 'outputs/tests/silhouette-archive/v3-test/plan.sqlite3'
    assert resolve_silhouette_reference(project, original, include_archive=True, lookup=lookup)['status'] == 'archived'


def test_cross_filesystem_rename_never_copies(tmp_path, monkeypatch):
    from services import silhouette_archive_execute as executor
    import errno
    project = _ready(tmp_path, count=1)
    preflight(project, 'v3-test')
    def refused(source, destination):
        raise OSError(errno.EXDEV, 'Cross-device link')
    monkeypatch.setattr(executor, '_rename', refused)
    with pytest.raises(OSError):
        execute(project, 'v3-test')
    assert (project / 'data/silhouettes/catalog/movie/Film/coat/object_0001.json').exists()


def test_hardlinked_source_is_rejected(tmp_path):
    import os
    project = _ready(tmp_path, count=1)
    source = project / 'data/silhouettes/catalog/movie/Film/coat/object_0001.png'
    os.link(source, project / 'duplicate.png')
    with pytest.raises(ValueError):
        preflight(project, 'v3-test')


def test_index_transition_requires_physical_acceptance(tmp_path):
    from services.silhouette_archive_execute import rebuild_silhouette_indexes
    project = _ready(tmp_path)
    with pytest.raises(ValueError, match='population'):
        rebuild_silhouette_indexes(project, 'v3-test')


def test_complete_index_backup_transition_preserves_other_indexes(tmp_path):
    from services.silhouette_archive_execute import rebuild_silhouette_indexes, verify_population, digest_file
    from services.illustration_index import rebuild_index, index_path
    project = _ready(tmp_path)
    preflight(project, 'v3-test')
    for media in ('movie', 'gameplay'):
        rebuild_index(project, 'silhouettes', media)
        path = index_path(project, 'silhouettes', media)
        info = path.stat()
        with sqlite3.connect(project / 'outputs/tests/silhouette-identity-migration/archival-execution/v3-test/journal.sqlite3') as database:
            database.execute('INSERT INTO protected VALUES(?,?,?,?)', (path.relative_to(project).as_posix(), info.st_size, info.st_mtime_ns, digest_file(path)))
    other = index_path(project, 'engravings', 'movie')
    other.write_bytes(b'unrelated index preserved')
    execute(project, 'v3-test')
    verify_population(project, 'v3-test')
    result = rebuild_silhouette_indexes(project, 'v3-test')
    assert result['status'] == 'PASS'
    assert result['indexes']['movie']['active_rows'] == 1
    assert result['indexes']['gameplay']['active_rows'] == 0
    assert other.read_bytes() == b'unrelated index preserved'
    for index in result['indexes'].values():
        assert digest_file(project / index['backup_path']) == index['old_sha256']


def test_empty_source_directories_are_journaled_and_restored(tmp_path):
    from services.silhouette_archive_execute import prune_empty, verify_population
    project = _ready(tmp_path, retired_only=True)
    preflight(project, 'v3-test')
    execute(project, 'v3-test')
    verify_population(project, 'v3-test')
    prune_empty(project, 'v3-test')
    assert not (project / 'data/silhouettes/catalog/movie/Film').exists()
    assert (project / 'data/silhouettes/catalog/movie').is_dir()
    assert (project / 'data/silhouettes/catalog').is_dir()
    execute(project, 'v3-test', rollback=True)
    assert (project / 'data/silhouettes/catalog/movie/Film/coat/object_0001.png').is_file()
    with sqlite3.connect(project / 'outputs/tests/silhouette-identity-migration/archival-execution/v3-test/journal.sqlite3') as database:
        assert database.execute('SELECT DISTINCT state FROM removed_directories').fetchall() == [('restored',)]


def test_pruning_interruption_reconciles_removed_directory(tmp_path):
    from services.silhouette_archive_execute import prune_empty, verify_population
    project = _ready(tmp_path, retired_only=True)
    preflight(project, 'v3-test')
    execute(project, 'v3-test')
    verify_population(project, 'v3-test')
    def crash(stage, directory):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        prune_empty(project, 'v3-test', fault=crash)
    prune_empty(project, 'v3-test')
    with sqlite3.connect(project / 'outputs/tests/silhouette-identity-migration/archival-execution/v3-test/journal.sqlite3') as database:
        assert database.execute('SELECT DISTINCT state FROM removed_directories').fetchall() == [('removed',)]
    execute(project, 'v3-test', rollback=True)
    assert (project / 'data/silhouettes/catalog/movie/Film/coat/object_0001.json').is_file()