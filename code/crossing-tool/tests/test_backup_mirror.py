from pathlib import Path
import json
import os
import sqlite3

import pytest

from services import backup_mirror as mirror


@pytest.mark.parametrize('name', ['.Spotlight-V100', '.fseventsd'])
def test_exfat_macos_sibling_inode_exception(environment, name):
    project, destination, mount = environment
    path = str(Path(mount['target']) / name)
    before = {path: [destination.stat().st_dev, 10, 0o40755, 262144, 100, 100]}
    after = {path: [destination.stat().st_dev, 20, 0o40755, 262144, 100, 100]}
    result = mirror.verify_protected_siblings(mirror.destination_context(project, destination), before, after, ['data/old'])
    assert result['exfat_inode_exceptions'][0]['path'] == path
    assert result['journaled_paths_confined_to_destination'] == 1
    assert not result['sibling_contents_proven_unchanged']


@pytest.mark.parametrize('filesystem,name', [('ext4', '.Spotlight-V100'), ('exfat', 'work'), ('exfat', 'other')])
def test_sibling_inode_exception_is_narrow(environment, filesystem, name):
    project, destination, mount = environment
    mount['fstype'] = filesystem
    path = str(Path(mount['target']) / name)
    before = {path: [destination.stat().st_dev, 10, 0o40755, 262144, 100, 100]}
    after = {path: [destination.stat().st_dev, 20, 0o40755, 262144, 100, 100]}
    with pytest.raises(mirror.MirrorError, match='metadata changed'):
        mirror.verify_protected_siblings(mirror.destination_context(project, destination), before, after, [])


@pytest.mark.parametrize('field,value', [(0, -1), (2, 0o120755), (3, 0), (4, 101), (5, 101)])
def test_exfat_sibling_real_changes_rejected(environment, field, value):
    project, destination, mount = environment
    path = str(Path(mount['target']) / '.fseventsd')
    before = {path: [destination.stat().st_dev, 10, 0o40755, 262144, 100, 100]}
    after = {path: list(before[path])}
    after[path][field] = value
    with pytest.raises(mirror.MirrorError):
        mirror.verify_protected_siblings(mirror.destination_context(project, destination), before, after, [])


@pytest.mark.parametrize('relative', ['../work/file', '/work/file', '../.fseventsd/file', 'data/../../.Spotlight-V100', './file', ''])
def test_sibling_journal_escape_rejected(environment, relative):
    project, destination, mount = environment
    with pytest.raises(mirror.MirrorError, match='escapes'):
        mirror.verify_protected_siblings(mirror.destination_context(project, destination), {}, {}, [relative])


def test_sibling_content_evidence_and_limits(environment):
    project, destination, mount = environment
    work = Path(mount['target']) / 'work'
    work.mkdir()
    payload = work / 'important'
    payload.write_text('before')
    info = work.stat()
    snapshot = {str(work): [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]}
    checksum = mirror.file_hash(payload)
    context = mirror.destination_context(project, destination)
    result = mirror.verify_protected_siblings(context, snapshot, snapshot, ['data/old'], content_hashes={str(payload): checksum})
    assert result['baseline_content_hashes_verified'] == 1
    assert not result['sibling_contents_proven_unchanged']
    payload.write_text('after!')
    with pytest.raises(mirror.MirrorError, match='content changed'):
        mirror.verify_protected_siblings(context, snapshot, snapshot, [], content_hashes={str(payload): checksum})
    with pytest.raises(mirror.MirrorError, match='membership'):
        mirror.verify_protected_siblings(context, snapshot, {}, [])


def test_completed_clear_verification_without_deletion(environment, monkeypatch):
    project, destination, mount = environment
    work = Path(mount['target']) / 'work'
    work.mkdir()
    payload = work / 'preserved'
    payload.write_text('user data')
    info = work.stat()
    snapshot = {str(work): [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]}
    bootstrap = Path(mount['target']) / 'bootstrap.sqlite3'
    bootstrap.write_bytes(b'fixture census')
    checksum = mirror.file_hash(bootstrap)
    context = mirror.destination_context(project, destination)
    baseline = {'destination': str(destination), 'destination_inode': destination.stat().st_ino,
                'destination_device': destination.stat().st_dev, 'siblings': snapshot}
    journal = sqlite3.connect(':memory:')
    journal.execute('CREATE TABLE clear_entries(path TEXT,state TEXT)')
    journal.execute("INSERT INTO clear_entries VALUES('data/old','deleted')")
    def forbid_deletion(*args, **kwargs):
        raise AssertionError('Completed clear verification must not delete anything')
    monkeypatch.setattr(os, 'unlink', forbid_deletion)
    monkeypatch.setattr(os, 'rmdir', forbid_deletion)
    result = mirror.verify_cleared_backup(context, baseline, journal, snapshot, bootstrap, checksum)
    assert result['empty_destination_verified'] and not result['deletions_repeated']
    assert payload.read_text() == 'user data'
    assert not result['siblings']['sibling_contents_proven_unchanged']
    bootstrap.write_bytes(b'changed')
    with pytest.raises(mirror.MirrorError, match='census differs'):
        mirror.verify_cleared_backup(context, baseline, journal, snapshot, bootstrap, checksum)
    bootstrap.write_bytes(b'fixture census')
    journal.execute("UPDATE clear_entries SET state='prepared'")
    with pytest.raises(mirror.MirrorError, match='unfinished'):
        mirror.verify_cleared_backup(context, baseline, journal, snapshot, bootstrap, checksum)
    journal.execute("UPDATE clear_entries SET state='deleted'")
    (destination / 'unexpected').write_text('new file')
    with pytest.raises(mirror.MirrorError, match='not empty'):
        mirror.verify_cleared_backup(context, baseline, journal, snapshot, bootstrap, checksum)
    baseline['destination_inode'] += 1
    with pytest.raises(mirror.MirrorError, match='root was replaced'):
        mirror.verify_cleared_backup(context, baseline, journal, snapshot, bootstrap, checksum)
    journal.close()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    project = tmp_path / 'source' / 'dead-crossing'
    project.mkdir(parents=True)
    for name in mirror.REQUIRED_TOPS:
        (project / name).mkdir()
    mountpoint = tmp_path / 'external'
    destination = mountpoint / 'backups' / project.name
    destination.mkdir(parents=True)
    mount = {'target': str(mountpoint), 'source': '/dev/test1', 'fstype': 'exfat',
             'options': 'rw', 'label': 'PLAYABLE', 'uuid': 'fixture-uuid', 'maj:min': '8:1'}
    monkeypatch.setattr(mirror, 'mounted_filesystems', lambda: [mount])
    monkeypatch.setattr(mirror, 'conflicting_operations', lambda: [])
    return project, destination, mount


@pytest.fixture
def remounted_mirror(environment, monkeypatch):
    project, destination, mount = environment
    result = ready(environment)
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    original = mirror.destination_context
    def changed_inode(*args, **kwargs):
        context = original(*args, **kwargs)
        context['destination_inode'] += 1
        return context
    expected = {'disk_serial': 'serial', 'disk_model': 'model', 'disk_bytes': 4000,
                'partition_uuid': 'partition', 'partition_bytes': 3900, 'filesystem_uuid': mount['uuid'],
                'mountpoint': mount['target'], 'destination': str(destination)}
    monkeypatch.setattr(mirror, 'restoration_device_identity', lambda context: dict(expected))
    return project, destination, result, expected, changed_inode


def test_read_only_remount_identity_and_strict_mutation(remounted_mirror, monkeypatch):
    project, destination, result, expected, changed_inode = remounted_mirror
    with mirror.readonly_database(result['plan']) as database:
        recorded = mirror.metadata(database, 'context')
    original = mirror.destination_context
    def historical_inode(*args, **kwargs):
        context = original(*args, **kwargs)
        context['destination_inode'] = recorded['destination_inode'] + 1
        return context
    monkeypatch.setattr(mirror, 'destination_context', historical_inode)
    with pytest.raises(mirror.MirrorError, match='identity changed'):
        mirror.restore_verification(project, destination, result['plan'], full=True)
    with pytest.raises(mirror.MirrorError, match='identity changed'):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    monkeypatch.setattr(mirror, 'destination_context', original)
    with mirror.restoration_context(project, destination, result['plan'], expected) as (context, evidence):
        assert evidence['read_only'] and not evidence['exfat_remount_inode_exception']


@pytest.mark.parametrize('field', ['disk_serial', 'disk_model', 'disk_bytes', 'partition_uuid', 'partition_bytes', 'filesystem_uuid', 'mountpoint', 'destination'])
def test_restoration_pins_reject_changes(remounted_mirror, field):
    project, destination, result, expected, changed_inode = remounted_mirror
    wrong = dict(expected)
    wrong[field] = 'changed'
    with pytest.raises(mirror.MirrorError, match='Pinned restoration'):
        mirror.restore_verification(project, destination, result['plan'], full=True, remount_identity=wrong)


def test_restoration_rejects_seal_tampering(remounted_mirror):
    project, destination, result, expected, changed_inode = remounted_mirror
    marker = mirror.read_marker(mirror.destination_context(project, destination))
    journal = destination / mirror.CONTROL / ('journal-' + marker['last_verified_plan'] + '.sqlite3')
    with sqlite3.connect(journal) as database:
        database.execute("UPDATE operation SET phase='interrupted'")
    with pytest.raises(mirror.MirrorError, match='SHA256'):
        mirror.restore_verification(project, destination, result['plan'], full=True, remount_identity=expected)


@pytest.mark.parametrize('filesystem', ['exfat', 'ext4'])
def test_historical_inode_exception_only_for_exfat_restoration(remounted_mirror, monkeypatch, filesystem):
    project, destination, result, expected, changed_inode = remounted_mirror
    mount = mirror.mounted_filesystems()[0]
    mount['fstype'] = filesystem
    original_metadata = mirror.metadata
    def historical_metadata(database, key, value=None):
        answer = original_metadata(database, key, value)
        if key == 'context' and value is None:
            answer = dict(answer)
            answer['destination_inode'] -= 1
            answer['mount'] = {**answer['mount'], 'fstype': filesystem}
        return answer
    monkeypatch.setattr(mirror, 'metadata', historical_metadata)
    if filesystem == 'ext4':
        with pytest.raises(mirror.MirrorError, match='beyond permitted'):
            mirror.restore_verification(project, destination, result['plan'], full=True, remount_identity=expected)
    else:
        restored = mirror.restore_verification(project, destination, result['plan'], full=True, remount_identity=expected)
        assert restored['full_payload_verified']
        assert restored['restoration_identity']['exfat_remount_inode_exception']
        assert restored['restoration_identity']['current_destination_inode'] == destination.stat().st_ino
        with pytest.raises(mirror.MirrorError, match='identity changed'):
            mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])


def test_restoration_rejects_target_replacement_in_session(remounted_mirror):
    project, destination, result, expected, changed_inode = remounted_mirror
    with pytest.raises(mirror.MirrorError, match='identity changed'):
        with mirror.restoration_context(project, destination, result['plan'], expected):
            destination.rename(destination.with_name('old-root'))
            destination.mkdir()


def test_restoration_rejects_marker_changes_in_session(remounted_mirror):
    project, destination, result, expected, changed_inode = remounted_mirror
    with pytest.raises(mirror.MirrorError, match='marker changed'):
        with mirror.restoration_context(project, destination, result['plan'], expected):
            path = destination / mirror.MARKER
            marker = json.loads(path.read_text())
            marker['last_verified_at'] += 1
            path.write_text(json.dumps(marker))


def test_mount_and_existing_destination(environment):
    project, destination, mount = environment
    context = mirror.destination_context(project, destination)
    assert context['mount']['uuid'] == 'fixture-uuid'
    assert context['destination'] == str(destination)
    assert mirror.read_marker(context) is None


def test_missing_external_mount(environment, monkeypatch):
    project, destination, mount = environment
    monkeypatch.setattr(mirror, 'mounted_filesystems', lambda: [])
    with pytest.raises(mirror.MirrorError, match='absent'):
        mirror.destination_context(project, destination)


def test_wrong_device_label(environment):
    project, destination, mount = environment
    mount['label'] = 'OTHER'
    with pytest.raises(mirror.MirrorError, match='absent'):
        mirror.destination_context(project, destination)


def test_wrong_marker(environment):
    project, destination, mount = environment
    (destination / mirror.MARKER).write_text(json.dumps({'purpose': 'other'}))
    with pytest.raises(mirror.MirrorError, match='identity'):
        mirror.read_marker(mirror.destination_context(project, destination))


def test_preview_inventory_and_exclusions(environment):
    project, destination, mount = environment
    (project / 'data/new').write_text('new')
    (destination / 'old').write_text('old research')
    (project / 'models').mkdir()
    (project / 'models/link').symlink_to('/unavailable/model')
    (destination / 'models').mkdir()
    (destination / 'models/link').mkdir()
    (destination / 'models/link/unique').write_text('keep')
    before = sorted(str(path.relative_to(destination)) for path in destination.rglob('*'))
    result = mirror.plan_mirror(project, destination)
    with mirror.readonly_database(result['plan']) as database:
        assert database.execute("SELECT action,classification FROM actions WHERE path='old'").fetchone() == ('delete', 'REQUIRES REVIEW')
        assert database.execute("SELECT action FROM actions WHERE path='data/new'").fetchone() == ('copy',)
        assert database.execute("SELECT action FROM actions WHERE path='models/link/unique'").fetchone() == ('protect',)
    assert sorted(str(path.relative_to(destination)) for path in destination.rglob('*')) == before
    assert not (destination / mirror.MARKER).exists()
    assert not (destination / mirror.CONTROL).exists()


def test_missing_source_directory(environment):
    project, destination, mount = environment
    (project / 'media').rmdir()
    with pytest.raises(mirror.MirrorError, match='Required source'):
        mirror.plan_mirror(project, destination)


def test_empty_source(environment):
    project, destination, mount = environment
    with pytest.raises(mirror.MirrorError, match='empty source'):
        mirror.plan_mirror(project, destination)


def test_changed_contents_same_size_and_time(environment):
    project, destination, mount = environment
    import os
    (project / 'data/value').write_text('abc')
    (destination / 'data').mkdir()
    (destination / 'data/value').write_text('xyz')
    info = (project / 'data/value').stat()
    os.utime(destination / 'data/value', ns=(info.st_atime_ns, info.st_mtime_ns))
    result = mirror.plan_mirror(project, destination, checksums=True)
    assert result['actions']['update']['count'] == 1


def ready(environment):
    project, destination, mount = environment
    (project / 'data/current').write_text('current data')
    (destination / 'obsolete').mkdir()
    (destination / 'obsolete/old').write_text('old data')
    result = mirror.plan_mirror(project, destination)
    mirror.adopt_mirror(project, destination, result['plan'], result['plan_digest'])
    return result


def test_add_update_move_delete_and_empty_directories(environment):
    project, destination, mount = environment
    (destination / 'data').mkdir()
    (destination / 'data/current').write_text('stale')
    result = ready(environment)
    verified = mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert verified['files_verified'] == 1
    assert (destination / 'data/current').read_text() == 'current data'
    assert not (destination / 'obsolete').exists()
    assert mirror.read_marker(mirror.destination_context(project, destination))['reconciled']


def test_apply_missing_marker(environment):
    project, destination, mount = environment
    (project / 'data/current').write_text('data')
    result = mirror.plan_mirror(project, destination)
    with pytest.raises(mirror.MirrorError, match='adoption'):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert not (destination / mirror.CONTROL).exists()


def test_first_reconciliation_threshold_requires_approval(environment):
    project, destination, mount = environment
    result = ready(environment)
    with pytest.raises(mirror.MirrorError, match='requires exact plan approval'):
        mirror.apply_mirror(project, destination, result['plan'])
    assert (destination / 'obsolete/old').exists()


def test_wrong_device_uuid(environment):
    project, destination, mount = environment
    result = ready(environment)
    mount['uuid'] = 'different-device'
    with pytest.raises(mirror.MirrorError):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert (destination / 'obsolete/old').exists()


def test_wrong_plan_approval(environment):
    project, destination, mount = environment
    result = ready(environment)
    with pytest.raises(mirror.MirrorError, match='Approval'):
        mirror.apply_mirror(project, destination, result['plan'], approval='not-the-plan')


@pytest.mark.parametrize('failure', ['copy', 'verify'])
def test_failure_never_deletes_backup(environment, monkeypatch, failure):
    project, destination, mount = environment
    result = ready(environment)
    def fail(*args, **kwargs):
        raise mirror.MirrorError('injected ' + failure + ' failure')
    monkeypatch.setattr(mirror, 'atomic_copy' if failure == 'copy' else 'verify_copy', fail)
    with pytest.raises(mirror.MirrorError, match='injected'):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert (destination / 'obsolete/old').read_text() == 'old data'
    assert not (destination / 'data/current').exists()


def test_interruption_and_resume(environment, monkeypatch):
    project, destination, mount = environment
    result = ready(environment)
    original = mirror.atomic_copy
    def interrupt(*args):
        original(*args)
        raise KeyboardInterrupt()
    monkeypatch.setattr(mirror, 'atomic_copy', interrupt)
    with pytest.raises(KeyboardInterrupt):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert (destination / 'obsolete/old').exists()
    monkeypatch.setattr(mirror, 'atomic_copy', original)
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert not (destination / 'obsolete').exists()


def test_concurrent_backup_lock(environment):
    project, destination, mount = environment
    with mirror.operation_lock(project):
        with pytest.raises(mirror.MirrorError, match='Another backup'):
            with mirror.operation_lock(project):
                pass


def test_source_population_change_aborts(environment):
    project, destination, mount = environment
    result = ready(environment)
    (project / 'data/unplanned').write_text('new')
    with pytest.raises(mirror.MirrorError, match='population'):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert (destination / 'obsolete/old').exists()


def test_missing_top_after_adoption(environment):
    project, destination, mount = environment
    result = ready(environment)
    (project / 'media').rmdir()
    with pytest.raises(mirror.MirrorError, match='Required source'):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])


def test_sqlite_consistent_copy_and_integrity(environment):
    project, destination, mount = environment
    with sqlite3.connect(project / 'data/index.sqlite3') as database:
        database.execute('CREATE TABLE sample(value TEXT)')
        database.execute("INSERT INTO sample VALUES('canonical')")
    result = ready(environment)
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    mirror.sqlite_integrity(destination / 'data/index.sqlite3')
    with mirror.readonly_database(destination / 'data/index.sqlite3') as database:
        assert database.execute('SELECT value FROM sample').fetchone() == ('canonical',)


def test_live_sqlite_wal_blocks_deletion(environment):
    project, destination, mount = environment
    database = sqlite3.connect(project / 'data/index.sqlite3')
    try:
        database.execute('PRAGMA journal_mode=WAL')
        database.execute('CREATE TABLE sample(value TEXT)')
        database.commit()
        result = ready(environment)
        with pytest.raises(mirror.MirrorError, match='WAL'):
            mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
        assert (destination / 'obsolete/old').exists()
    finally:
        database.close()


def test_sqlite_integrity_failure_before_deletion(environment, monkeypatch):
    project, destination, mount = environment
    with sqlite3.connect(project / 'data/index.sqlite3') as database:
        database.execute('CREATE TABLE sample(value TEXT)')
    result = ready(environment)
    def fail(path):
        raise mirror.MirrorError('injected integrity failure')
    monkeypatch.setattr(mirror, 'sqlite_integrity', fail)
    with pytest.raises(mirror.MirrorError, match='integrity failure'):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert (destination / 'obsolete/old').exists()


def test_exfat_case_collision(environment):
    project, destination, mount = environment
    (project / 'data/Foo').write_text('one')
    (project / 'data/foo').write_text('two')
    result = ready(environment)
    assert result['issues'] == 1
    with pytest.raises(mirror.MirrorError, match='filename'):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])


def test_restore_verification_without_production_restore(environment):
    project, destination, mount = environment
    result = ready(environment)
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    restored = mirror.restore_verification(project, destination, result['plan'], full=True)
    assert restored['full_payload_verified']
    assert not restored['complete_linux_restore']
    assert not restored['census']['verified']


def test_configured_wrong_project(environment):
    project, destination, mount = environment
    with pytest.raises(mirror.MirrorError, match='Configuration'):
        mirror.destination_context(project, destination.parent / 'other-project')


def test_symlink_destination_escape(environment):
    project, destination, mount = environment
    destination.rmdir()
    destination.symlink_to(project, target_is_directory=True)
    with pytest.raises(mirror.MirrorError):
        mirror.destination_context(project, destination)


def test_readonly_mount(environment):
    project, destination, mount = environment
    mount['options'] = 'ro'
    with pytest.raises(mirror.MirrorError, match='not writable'):
        mirror.destination_context(project, destination)


def test_cli_preview_calls_shared_service(environment, monkeypatch, capsys):
    import cli
    from types import SimpleNamespace
    project, destination, mount = environment
    (project / 'preferences/preferences.json').write_text(json.dumps({'backup_path': str(destination)}))
    monkeypatch.setattr(cli.prefs, 'get', lambda key: '/wrong/active/project')
    calls = []
    def preview(source, backup, **kwargs):
        calls.append((source, backup))
        return {'preview': 'shared-service-result'}
    monkeypatch.setattr(mirror, 'publish_preview', preview)
    cli.cmd_backup(SimpleNamespace(backup_subcommand='mirror', project=str(project), apply=False, checksum=False))
    assert calls == [(str(project), str(destination))]
    assert 'shared-service-result' in capsys.readouterr().out


def test_cli_error_does_not_fall_back(environment, monkeypatch, capsys):
    import cli
    from types import SimpleNamespace
    project, destination, mount = environment
    (project / 'preferences/preferences.json').write_text(json.dumps({'backup_path': str(destination)}))
    monkeypatch.setattr(mirror, 'mounted_filesystems', lambda: [])
    with pytest.raises(SystemExit) as error:
        cli.cmd_backup(SimpleNamespace(backup_subcommand='mirror', project=str(project), apply=False, checksum=False))
    assert error.value.code == 1
    assert 'absent' in capsys.readouterr().err
    assert not (destination / mirror.MARKER).exists()


@pytest.mark.parametrize('arguments', [[], ['--dry-run'], ['update'], ['update', '--dry-run'],
                                      ['update', '--mirror'], ['mirror', '--dry-run']])
def test_normal_backup_defaults_to_guarded_mirror(environment, monkeypatch, arguments):
    import cli
    project, destination, mount = environment
    (project / 'preferences/preferences.json').write_text(json.dumps({'backup_path': str(destination)}))
    monkeypatch.setattr(cli.prefs, 'get', lambda key, default=None: str(project) if key == 'path' else default)
    calls = []
    def preview(source, backup, **kwargs):
        calls.append((source, backup))
        return {'mode': 'guarded mirror'}
    def forbidden(*args, **kwargs):
        raise AssertionError('Normal backup must never use legacy additive copy or implicit apply')
    monkeypatch.setattr(mirror, 'publish_preview', preview)
    monkeypatch.setattr(mirror, 'apply_mirror', forbidden)
    monkeypatch.setattr(cli, '_backup_update', forbidden)
    args = cli.build_parser().parse_args(['backup', *arguments])
    cli.cmd_backup(args)
    assert calls == [(str(project), str(destination))]


@pytest.mark.parametrize('arguments', [['update', '--apply', '--dry-run'],
                                      ['--dry-run', 'update', '--apply']])
def test_normal_backup_dry_run_always_prevents_apply(environment, monkeypatch, arguments):
    import cli
    project, destination, mount = environment
    (project / 'preferences/preferences.json').write_text(json.dumps({'backup_path': str(destination)}))
    monkeypatch.setattr(cli.prefs, 'get', lambda key, default=None: str(project) if key == 'path' else default)
    calls = []
    monkeypatch.setattr(mirror, 'publish_preview', lambda *args, **kwargs: calls.append('preview'))
    def forbidden(*args, **kwargs):
        raise AssertionError('Dry run must override apply')
    monkeypatch.setattr(mirror, 'apply_mirror', forbidden)
    cli.cmd_backup(cli.build_parser().parse_args(['backup', *arguments]))
    assert calls == ['preview']


def test_normal_backup_apply_delegates_to_guarded_mirror(environment, monkeypatch):
    import cli
    project, destination, mount = environment
    (project / 'preferences/preferences.json').write_text(json.dumps({'backup_path': str(destination)}))
    calls = []
    def apply(source, backup, plan, **kwargs):
        calls.append((source, backup, plan, kwargs))
        return {'verified': True}
    monkeypatch.setattr(mirror, 'apply_mirror', apply)
    args = cli.build_parser().parse_args(['backup', 'update', '--project', str(project),
                                          '--apply', '--plan', 'reviewed.sqlite3', '--approve-plan', 'approved-digest'])
    cli.cmd_backup(args)
    assert calls == [(str(project), str(destination), 'reviewed.sqlite3', {'approval': 'approved-digest'})]


def test_source_changes_during_copy_block_deletion(environment, monkeypatch):
    project, destination, mount = environment
    result = ready(environment)
    original = mirror.atomic_copy
    def copy_then_change(*args):
        checksum = original(*args)
        (project / 'data/current').write_text('changed while copying')
        return checksum
    monkeypatch.setattr(mirror, 'atomic_copy', copy_then_change)
    with pytest.raises(mirror.MirrorError, match='changed'):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert (destination / 'obsolete/old').exists()


def test_destination_candidate_replacement_blocks_deletion(environment):
    project, destination, mount = environment
    result = ready(environment)
    (destination / 'obsolete/old').unlink()
    (destination / 'obsolete/old').write_text('unique replacement')
    with pytest.raises(mirror.MirrorError, match='changed'):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert (destination / 'obsolete/old').read_text() == 'unique replacement'


def test_insufficient_copy_first_capacity(environment, monkeypatch):
    from types import SimpleNamespace
    project, destination, mount = environment
    result = ready(environment)
    monkeypatch.setattr(mirror.shutil, 'disk_usage', lambda path: SimpleNamespace(total=1000, free=0))
    with pytest.raises(mirror.MirrorError, match='Insufficient'):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert (destination / 'obsolete/old').exists()


def test_extraction_lock_prevents_backup(environment):
    import fcntl
    project, destination, mount = environment
    (project / 'data/silhouettes').mkdir()
    with (project / 'data/silhouettes/.catalog-extraction.lock').open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(mirror.MirrorError, match='Conflicting silhouette'):
            with mirror.operation_lock(project):
                pass


@pytest.mark.parametrize('limits', [
    {'max_files': -1, 'max_bytes': 0, 'max_percentage': 0},
    {'max_files': 0, 'max_bytes': 0, 'max_percentage': float('nan')},
    {'max_files': 0, 'max_bytes': 0, 'max_percentage': 101},
])
def test_invalid_deletion_limits(environment, limits):
    project, destination, mount = environment
    (project / 'data/current').write_text('canonical')
    result = mirror.plan_mirror(project, destination)
    with pytest.raises(mirror.MirrorError, match='limits'):
        mirror.adopt_mirror(project, destination, result['plan'], result['plan_digest'], limits=limits)
    assert not (destination / mirror.MARKER).exists()


def test_cli_mirror_defaults_to_preview():
    import cli
    args = cli.build_parser().parse_args(['backup', 'mirror'])
    assert args.backup_subcommand == 'mirror'
    assert args.apply is False


def test_preserve_existing_control_subtree(environment):
    project, destination, mount = environment
    (destination / mirror.CONTROL).mkdir()
    (destination / mirror.CONTROL / 'unique').write_text('never prune')
    result = ready(environment)
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert (destination / mirror.CONTROL / 'unique').read_text() == 'never prune'


def test_complete_preview_outputs_and_source_protection(environment):
    project, destination, mount = environment
    (project / 'data/canonical').write_text('unchanged')
    (project / 'preferences/preferences.json').write_text(json.dumps({'backup_path': str(destination)}))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in project.rglob('*') if path.is_file()}
    result = mirror.publish_preview(project, destination)
    report = project / mirror.REPORT_RELATIVE
    for name in ('architecture.md', 'existing-backup-audit.md', 'mirror-policy.json',
                 'reconciliation-plan.sqlite3', 'reconciliation-summary.json', 'deletion-preview.md',
                 'restore-verification.md', 'tests-report.md', 'implementation-report.md'):
        assert (report / name).is_file()
    assert all((path.read_bytes(), path.stat().st_mtime_ns) == values for path, values in before.items())
    assert list(destination.iterdir()) == []
    assert result['census_backup_verified'] is False


def test_source_change_guard_detects_noncooperating_writer(environment):
    project, destination, mount = environment
    (project / 'data/canonical').write_text('before')
    result = mirror.plan_mirror(project, destination)
    with mirror.readonly_database(result['plan']) as database:
        guard = mirror.SourceGuard(database, project)
        try:
            (project / 'data/canonical').write_text('after')
            with pytest.raises(mirror.MirrorError, match='Source changed'):
                guard.check()
        finally:
            guard.close()


def test_source_change_guard_ignores_report_workspace(environment):
    project, destination, mount = environment
    (project / 'data/canonical').write_text('before')
    result = mirror.plan_mirror(project, destination)
    with mirror.readonly_database(result['plan']) as database:
        guard = mirror.SourceGuard(database, project)
        try:
            (project / mirror.REPORT_RELATIVE / 'small-report').write_text('control only')
            guard.check()
        finally:
            guard.close()


def test_restore_verification_detects_extra_payload(environment):
    project, destination, mount = environment
    result = ready(environment)
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    (destination / 'unexplained').write_text('extra')
    with pytest.raises(mirror.MirrorError, match='population'):
        mirror.restore_verification(project, destination, result['plan'], full=True)
    assert (destination / 'unexplained').exists()


def test_change_guard_resource_failure_prevents_deletion(environment, monkeypatch):
    project, destination, mount = environment
    result = ready(environment)
    def unavailable(*args):
        raise mirror.MirrorError('Source change guard unavailable')
    monkeypatch.setattr(mirror, 'SourceGuard', unavailable)
    with pytest.raises(mirror.MirrorError, match='unavailable'):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert (destination / 'obsolete/old').exists()


def test_corrupt_sqlite_header_blocks_deletion(environment):
    project, destination, mount = environment
    (project / 'data/index.sqlite3').write_bytes(b'broken historical database')
    result = ready(environment)
    with pytest.raises(mirror.MirrorError, match='header'):
        mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    assert (destination / 'obsolete/old').exists()


def test_wal_rejected_without_opening_sqlite_sidecars(environment, monkeypatch):
    project, destination, mount = environment
    path = project / 'data/live.sqlite3'
    header = bytearray(100)
    header[:16] = b'SQLite format 3\x00'
    header[18] = 2
    path.write_bytes(header)
    def forbidden(path):
        raise AssertionError('WAL source must not be opened by SQLite')
    monkeypatch.setattr(mirror, 'readonly_database', forbidden)
    with pytest.raises(mirror.MirrorError, match='WAL'):
        with mirror.sqlite_window(path):
            pass
    assert not Path(str(path) + '-shm').exists()


def test_offline_restore_verification_after_primary_loss(environment):
    import shutil
    project, destination, mount = environment
    result = ready(environment)
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    shutil.rmtree(project)
    restored = mirror.verify_stored_mirror(destination)
    assert restored['verified']
    assert restored['primary_project_required'] is False
    assert restored['files_verified'] == 1


def test_offline_restore_rejects_checksum_journal_drift(environment):
    project, destination, mount = environment
    result = ready(environment)
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    ledger = destination / mirror.CONTROL / ('journal-' + result['plan_digest'] + '.sqlite3')
    with sqlite3.connect(ledger) as database:
        database.execute("UPDATE verified SET sha256='incorrect'")
    with pytest.raises(mirror.MirrorError, match='SHA256'):
        mirror.verify_stored_mirror(destination)


def test_historical_snapshot_accepts_changed_live_source(environment, monkeypatch):
    project, destination, mount = environment
    result = ready(environment)
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    (project / 'data/current').write_text('changed live research')
    (project / 'preferences/preferences.json').write_text('{"window_project": {"width": 900}}')
    def forbidden_source_comparison(*args, **kwargs):
        raise AssertionError('Historical verification must not compare the current source inventory')
    monkeypatch.setattr(mirror, 'validate_inventory', forbidden_source_comparison)
    verified = mirror.verify_stored_mirror(destination)
    assert verified['verified'] and verified['files_verified'] == 1
    assert not verified['primary_project_required']
    assert (project / 'data/current').read_text() == 'changed live research'


@pytest.mark.parametrize('damage', ['content', 'extra', 'missing_directory'])
def test_historical_snapshot_rejects_mirror_changes(environment, damage):
    project, destination, mount = environment
    result = ready(environment)
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    if damage == 'content':
        (destination / 'data/current').write_text('corrupt mirror')
    elif damage == 'extra':
        (destination / 'unexpected').write_text('not in sealed snapshot')
    else:
        (destination / 'media').rmdir()
    with pytest.raises(mirror.MirrorError):
        mirror.verify_stored_mirror(destination)


def test_historical_snapshot_detects_change_after_checksum(environment):
    project, destination, mount = environment
    result = ready(environment)
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    def change_after_checksum(detail):
        if detail['phase'] == 'population':
            (destination / 'data/current').write_text('changed after verification')
    with pytest.raises(mirror.MirrorError, match='changed'):
        mirror.verify_stored_mirror(destination, progress=change_after_checksum)


def test_historical_snapshot_requires_destination_monitor(environment, monkeypatch):
    project, destination, mount = environment
    result = ready(environment)
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    def unavailable(*args, **kwargs):
        raise mirror.MirrorError('destination monitoring unavailable')
    monkeypatch.setattr(mirror, 'SourceGuard', unavailable)
    with pytest.raises(mirror.MirrorError, match='monitoring unavailable'):
        mirror.verify_stored_mirror(destination)


def test_historical_snapshot_progress_and_sqlite(environment):
    project, destination, mount = environment
    path = project / 'data/critical.sqlite3'
    with sqlite3.connect(path) as database:
        database.execute('CREATE TABLE evidence(value TEXT)')
        database.execute("INSERT INTO evidence VALUES('historical')")
    result = mirror.plan_mirror(project, destination)
    mirror.adopt_mirror(project, destination, result['plan'], result['plan_digest'])
    mirror.apply_mirror(project, destination, result['plan'], approval=result['plan_digest'])
    details = []
    verification = mirror.verify_stored_mirror(destination, progress=details.append)
    assert verification['verified'] and verification['sqlite_databases_verified'] == 1
    assert verification['directory_structure_and_population_verified']
    assert details[0]['files_verified'] == 0
    assert details[-1]['phase'] == 'population'