from pathlib import Path
import json
import os
import sqlite3

import pytest

from services import backup_mirror as mirror


@pytest.mark.parametrize('side', ['source', 'destination'])
def test_directory_preflight_reports_identity_not_type_for_inode_change(tmp_path, side):
    root = tmp_path / side
    root.mkdir()
    directory = root / '.pytest_cache'
    directory.mkdir()
    with sqlite3.connect(':memory:') as database:
        mirror.new_inventory(database)
        mirror.inventory_tree(database, root, side)
        info = directory.lstat()
        database.execute('UPDATE inventory SET inode=? WHERE side=? AND path=?', (info.st_ino + 1, side, '.pytest_cache'))
        with pytest.raises(mirror.MirrorError) as error:
            mirror.validate_inventory(database, side, root)
        message = str(error.value)
        assert f'Inventory {side} directory identity changed: .pytest_cache' in message
        assert f'expected type=directory inode={info.st_ino + 1}' in message
        assert f'observed type=directory inode={info.st_ino}' in message


@pytest.mark.parametrize('filesystem', ['ext4', 'exfat'])
@pytest.mark.parametrize('side', ['source', 'destination'])
@pytest.mark.parametrize('change', ['unchanged', 'timestamps', 'permissions', 'file', 'symlink', 'missing', 'recreated', 'inode', 'device'])
def test_directory_preflight_preserves_identity_guards(environment, tmp_path, filesystem, side, change):
    project, destination, mount = environment
    mount['fstype'] = filesystem
    root = project if side == 'source' else destination
    directory = root / '.pytest_cache'
    directory.mkdir()
    info = directory.lstat()
    with sqlite3.connect(':memory:') as database:
        mirror.new_inventory(database)
        mirror.inventory_tree(database, root, side)
        if change == 'timestamps':
            os.utime(directory, ns=(info.st_atime_ns + 4_000_000_000, info.st_mtime_ns + 4_000_000_000))
        elif change == 'permissions':
            directory.chmod(0o700 if info.st_mode & 0o777 != 0o700 else 0o755)
        elif change in ('file', 'symlink', 'missing'):
            directory.rmdir()
            if change == 'file':
                directory.write_text('replacement')
            elif change == 'symlink':
                outside = tmp_path / 'outside'
                outside.mkdir()
                (outside / 'must-not-traverse').write_text('external data')
                directory.symlink_to(outside, target_is_directory=True)
        elif change == 'recreated':
            replacement = tmp_path / 'replacement'
            replacement.mkdir()
            directory.rmdir()
            replacement.rename(directory)
            assert directory.lstat().st_ino != info.st_ino
        elif change in ('inode', 'device'):
            column = change
            previous = info.st_ino + 1 if change == 'inode' else info.st_dev + 1
            database.execute(f'UPDATE inventory SET {column}=? WHERE side=? AND path=?', (previous, side, '.pytest_cache'))
        if change in ('unchanged', 'timestamps', 'permissions'):
            mirror.validate_inventory(database, side, root)
        else:
            with pytest.raises(mirror.MirrorError) as error:
                mirror.validate_inventory(database, side, root)
            message = str(error.value)
            if change == 'missing':
                assert 'Inventory entry disappeared: .pytest_cache' in message
            else:
                reason = 'type' if change in ('file', 'symlink') else 'identity'
                assert f'Inventory {side} directory {reason} changed: .pytest_cache' in message
                assert f'device={info.st_dev}' in message


@pytest.mark.parametrize('side', ['source', 'destination'])
def test_stale_directory_plan_is_not_amended_or_executed(environment, tmp_path, side):
    project, destination, mount = environment
    root = project if side == 'source' else destination
    directory = root / '.pytest_cache'
    directory.mkdir()
    if side == 'destination':
        mount['fstype'] = 'ext4'
    preview = ready(environment)
    marker = (destination / mirror.MARKER).read_bytes()
    replacement = tmp_path / 'replacement'
    replacement.mkdir()
    directory.rmdir()
    replacement.rename(directory)
    with pytest.raises(mirror.MirrorError, match=f'Inventory {side} directory identity changed'):
        mirror.apply_mirror(project, destination, preview['plan'], approval=preview['plan_digest'])
    assert mirror.file_hash(preview['plan']) == preview['plan_digest']
    assert (destination / mirror.MARKER).read_bytes() == marker
    assert (destination / 'obsolete/old').read_text() == 'old data'
    assert not (destination / mirror.CONTROL).exists()
    assert not (destination / 'data/current').exists()


@pytest.mark.parametrize('filesystem,side,change,error', [
    ('exfat', 'destination', 'unchanged', None),
    ('ext4', 'destination', 'unchanged', 'Inventory destination directory identity changed'),
    ('exfat', 'source', 'unchanged', 'Inventory source directory identity changed'),
    ('exfat', 'destination', 'file', 'Inventory destination directory type changed'),
    ('exfat', 'destination', 'symlink', 'Inventory destination directory type changed'),
    ('exfat', 'destination', 'missing', 'Inventory entry disappeared'),
    ('exfat', 'destination', 'unexpected_child', 'file population changed'),
    ('exfat', 'destination', 'missing_child', 'Inventory entry disappeared'),
    ('exfat', 'destination', 'content', 'Inventory file changed'),
    ('exfat', 'destination', 'file_inode', 'Inventory file changed'),
    ('exfat', 'destination', 'directory_device', 'Inventory destination directory identity changed'),
    ('exfat', 'destination', 'mount_uuid', 'identity changed'),
    ('exfat', 'destination', 'mount_device', 'identity changed'),
    ('exfat', 'destination', 'mount_type', 'identity changed'),
    ('exfat', 'destination', 'mount_missing', 'absent'),
    ('exfat', 'destination', 'root_inode', 'identity changed'),
    ('exfat', 'destination', 'rmdir', 'Inventory destination directory identity changed'),
    ('exfat', 'destination', 'approval', 'requires exact plan approval'),
    ('exfat', 'destination', 'wrong_approval', 'Approval does not match'),
])
def test_directory_inode_preflight_through_actual_apply(cli_mirror, monkeypatch, filesystem, side, change, error):
    project, destination, mount = cli_mirror
    mount['fstype'] = filesystem
    directory = destination / 'media/empty'
    directory.mkdir(parents=True)
    child = directory / 'expected'
    child.write_text('original')
    if change != 'rmdir':
        (project / 'media/empty').mkdir(parents=True)
        (project / 'media/empty/expected').write_text('original')
    if change == 'approval':
        (destination / 'obsolete').write_text('must remain')
    preview = mirror.prepare_mirror(project, destination)
    marker = (destination / mirror.MARKER).read_bytes()
    database = sqlite3.connect(':memory:')
    try:
        with mirror.readonly_database(preview['plan']) as original:
            original.backup(database)
        database.execute("UPDATE inventory SET inode=inode+1 WHERE side=? AND kind='directory'", (side,))
        if change == 'directory_device':
            database.execute("UPDATE inventory SET device=device+1 WHERE side='destination' AND path='media/empty'")
        elif change == 'file_inode':
            database.execute("UPDATE inventory SET inode=inode+1 WHERE side='destination' AND path='media/empty/expected'")
        elif change == 'root_inode':
            context = mirror.metadata(database, 'context')
            context['destination_inode'] += 1
            mirror.metadata(database, 'context', context)
        database.commit()
        amended = project / mirror.REPORT_RELATIVE / 'fixture-inode.sqlite3'
        with sqlite3.connect(amended) as output:
            database.backup(output)
        digest = mirror.file_hash(amended)
        if change in ('file', 'symlink', 'missing'):
            child.unlink()
            directory.rmdir()
            if change == 'file':
                directory.write_text('replacement')
            elif change == 'symlink':
                directory.symlink_to(project / 'media/empty', target_is_directory=True)
        elif change == 'unexpected_child':
            (directory / 'unexpected').write_text('unexpected')
        elif change == 'missing_child':
            child.unlink()
        elif change == 'content':
            info = child.stat()
            child.write_text('tampered')
            os.utime(child, ns=(info.st_atime_ns, info.st_mtime_ns))
        elif change == 'mount_uuid':
            mount['uuid'] = 'different-device'
        elif change == 'mount_device':
            mount['maj:min'] = '8:2'
        elif change == 'mount_type':
            mount['fstype'] = 'ext4'
        elif change == 'mount_missing':
            monkeypatch.setattr(mirror, 'mounted_filesystems', lambda: [])
        approval = digest if change == 'rmdir' else 'wrong' if change == 'wrong_approval' else None
        if error is None:
            result = mirror.apply_mirror(project, destination, amended)
            assert result['sha256'] and result['files_verified'] == 3
            assert (destination / 'data/current').read_text() == 'current data'
        else:
            with pytest.raises(mirror.MirrorError, match=error):
                mirror.apply_mirror(project, destination, amended, approval=approval)
            assert (destination / mirror.MARKER).read_bytes() == marker
            assert not (destination / mirror.CONTROL).exists()
            assert not (destination / 'data/current').exists()
            if change == 'rmdir':
                assert child.read_text() == 'original'
            elif change == 'approval':
                assert (destination / 'obsolete').read_text() == 'must remain'
        assert mirror.file_hash(amended) == digest
        assert mirror.file_hash(preview['plan']) == preview['plan_digest']
    finally:
        database.close()


@pytest.mark.parametrize('change', ['mount_type', 'mount_uuid', 'root_inode', 'wrong_root'])
def test_exfat_inventory_exception_requires_validated_context(environment, change):
    project, destination, mount = environment
    directory = destination / '.pytest_cache'
    directory.mkdir()
    context = json.loads(json.dumps(mirror.destination_context(project, destination)))
    with sqlite3.connect(':memory:') as database:
        mirror.new_inventory(database)
        mirror.inventory_tree(database, destination, 'destination')
        database.execute("UPDATE inventory SET inode=inode+1 WHERE side='destination' AND kind='directory'")
        if change == 'mount_type':
            mount['fstype'] = 'ext4'
        elif change == 'mount_uuid':
            mount['uuid'] = 'different-device'
        elif change == 'root_inode':
            context['destination_inode'] += 1
        root = project if change == 'wrong_root' else destination
        with pytest.raises(mirror.MirrorError):
            mirror.validate_inventory(database, 'destination', root, context=context)


def test_preview_session_executes_exact_plan(environment):
    project, destination, mount = environment
    ready(environment)
    preview = mirror.prepare_mirror(project, destination)
    events = []
    with pytest.raises(mirror.MirrorError, match='additional explicit approval'):
        mirror.execute_preview(project, destination, preview)
    result = mirror.execute_preview(project, destination, preview, approve_delete=True, progress=events.append)
    assert result['sha256'] and result['files_verified'] > 0
    assert {'copy', 'verify', 'complete'} <= {event['phase'] for event in events}
    assert 'Destination:' in mirror.preview_text(preview)
    with pytest.raises(mirror.MirrorError, match='Approval does not match'):
        mirror.execute_preview(project, destination, {**preview, 'plan_digest': 'wrong'})


@pytest.fixture
def cli_mirror(environment, monkeypatch):
    import cli
    from types import SimpleNamespace
    project, destination, mount = environment
    (project / 'preferences/preferences.json').write_text(json.dumps({'backup_path': str(destination)}))
    (project / 'data/current').write_text('current data')
    initial = mirror.plan_mirror(project, destination)
    mirror.adopt_mirror(project, destination, initial['plan'], initial['plan_digest'])
    monkeypatch.setattr(cli.prefs, 'get', lambda key, default=None: str(project) if key == 'path' else default)
    monkeypatch.setattr(cli.sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    return project, destination, mount


@pytest.mark.parametrize('answer,execute', [('y', True), ('yes', True), (' YES ', True),
                                          ('n', False), ('no', False), ('', False), ('maybe', False)])
def test_cli_interactive_answers(cli_mirror, monkeypatch, capsys, answer, execute):
    import cli
    project, destination, mount = cli_mirror
    prompts = []
    def confirm(prompt):
        prompts.append(prompt)
        return answer
    monkeypatch.setattr('builtins.input', confirm)
    cli.cmd_backup(cli.build_parser().parse_args(['backup', 'mirror']))
    assert (destination / 'data/current').exists() == execute
    assert len(prompts) == 1 and '[y/N]' in prompts[0]
    output = capsys.readouterr().out
    assert ('synchronized and verified' in output) == execute
    assert 'Already present:' in output and 'Copy-before-delete required:' in output


@pytest.mark.parametrize('mode', ['eof', 'noninteractive', 'interrupt'])
def test_cli_confirmation_never_implicitly_authorizes(cli_mirror, monkeypatch, mode, capsys):
    import cli
    project, destination, mount = cli_mirror
    def interrupted(prompt):
        raise EOFError if mode == 'eof' else KeyboardInterrupt
    monkeypatch.setattr('builtins.input', interrupted)
    if mode == 'noninteractive':
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)
        cli.cmd_backup(cli.build_parser().parse_args(['backup', 'mirror']))
    elif mode == 'interrupt':
        with pytest.raises(SystemExit) as error:
            cli.cmd_backup(cli.build_parser().parse_args(['backup', 'mirror']))
        assert error.value.code == 130
    else:
        cli.cmd_backup(cli.build_parser().parse_args(['backup', 'mirror']))
    assert not (destination / 'data/current').exists()
    assert not (destination / mirror.CONTROL).exists()


def test_cli_unattended_non_destructive_apply_needs_no_plan_or_digest(cli_mirror, monkeypatch, capsys):
    import cli
    project, destination, mount = cli_mirror
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)
    def forbidden(prompt):
        raise AssertionError('Unattended apply must not prompt')
    monkeypatch.setattr('builtins.input', forbidden)
    cli.cmd_backup(cli.build_parser().parse_args(['backup', 'mirror', '--apply']))
    assert (destination / 'data/current').read_text() == 'current data'
    assert 'synchronized and verified' in capsys.readouterr().out


def test_full_verification_resolves_completed_retained_plan(cli_mirror):
    project, destination, mount = cli_mirror
    preview = mirror.prepare_mirror(project, destination)
    mirror.execute_preview(project, destination, preview)
    assert mirror.restore_verification(project, destination, full=True)['full_payload_verified']
    marker = mirror.read_marker(mirror.destination_context(project, destination))
    retained = destination / mirror.CONTROL / ('metadata-' + marker['last_verified_plan'] + '.sqlite3')
    with sqlite3.connect(retained) as database:
        mirror.metadata(database, 'tampered', True)
    with pytest.raises(mirror.MirrorError, match='Approval does not match'):
        mirror.restore_verification(project, destination, full=True)


def test_full_verification_does_not_infer_an_unverified_plan(cli_mirror):
    project, destination, mount = cli_mirror
    with pytest.raises(mirror.MirrorError, match='completed mirror'):
        mirror.restore_verification(project, destination, full=True)


def test_full_verification_progress_and_cooperative_cancel(cli_mirror):
    project, destination, mount = cli_mirror
    preview = mirror.prepare_mirror(project, destination)
    mirror.execute_preview(project, destination, preview)
    events = []
    result = mirror.restore_verification(project, destination, full=True, progress=events.append)
    assert result['full_payload_verified']
    payload = [event for event in events if event['phase'] == 'payload_checked'][-1]
    assert payload['files_verified'] == payload['total_files'] == 2
    assert payload['sqlite_databases_verified'] == 0
    assert 'population' in {event['phase'] for event in events}
    with pytest.raises(mirror.MirrorCancelled):
        mirror.restore_verification(project, destination, full=True, cancelled=lambda: True)


def test_persistent_mirror_and_full_verification_are_independent(cli_mirror):
    project, destination, mount = cli_mirror
    assert mirror.backup_status(project, destination)['last_mirror'] is None
    preview = mirror.prepare_mirror(project, destination)
    assert mirror.backup_status(project, destination)['last_full_verify'] is None
    mirror.execute_preview(project, destination, preview)
    status = mirror.backup_status(project, destination)
    assert status['last_mirror'] and status['last_full_verify'] is None
    verified = mirror.restore_verification(project, destination, full=True)
    current = mirror.backup_status(project, destination)
    assert current['last_full_verify'] == verified['completed_at'] and current['verification_current']
    (project / 'data/new').write_text('new content')
    mirror.execute_preview(project, destination, mirror.prepare_mirror(project, destination))
    historical = mirror.backup_status(project, destination)
    assert historical['last_full_verify'] == current['last_full_verify']
    assert not historical['verification_current']
    mirror.restore_verification(project, destination, full=True)
    assert mirror.backup_status(project, destination)['verification_current']


@pytest.mark.parametrize('failure', ['cancel', 'population', 'disconnect', 'late_change'])
def test_failed_full_verification_does_not_persist_success(cli_mirror, monkeypatch, failure):
    project, destination, mount = cli_mirror
    mirror.execute_preview(project, destination, mirror.prepare_mirror(project, destination))
    before = (destination / mirror.MARKER).read_bytes()
    stopping = False
    def progress(event):
        nonlocal stopping
        if failure == 'late_change' and event['phase'] == 'final_validation':
            (destination / 'data/current').write_text('changed after checksums')
        if event['phase'] == 'payload_checked':
            if failure == 'population':
                (destination / 'unexpected').write_text('unexpected')
            elif failure == 'disconnect':
                monkeypatch.setattr(mirror, 'mounted_filesystems', lambda: [])
            elif failure == 'cancel':
                stopping = True
    with pytest.raises(mirror.MirrorError):
        mirror.restore_verification(project, destination, full=True, progress=progress, cancelled=lambda: stopping)
    assert (destination / mirror.MARKER).read_bytes() == before


def test_invalid_or_unrelated_status_evidence_is_unknown(cli_mirror, monkeypatch):
    project, destination, mount = cli_mirror
    mirror.execute_preview(project, destination, mirror.prepare_mirror(project, destination))
    marker = mirror.read_marker(mirror.destination_context(project, destination))
    marker['last_verified_at'] = 'yesterday'
    marker['last_full_verification'] = {'completed_at': 12345}
    (destination / mirror.MARKER).write_text(json.dumps(marker))
    status = mirror.backup_status(project, destination)
    assert status['last_mirror'] is status['last_full_verify'] is None
    marker['device_uuid'] = 'another-disk'
    (destination / mirror.MARKER).write_text(json.dumps(marker))
    assert mirror.backup_status(project, destination)['last_mirror'] is None


def test_full_verification_record_is_bound_to_selected_mirror(cli_mirror):
    project, destination, mount = cli_mirror
    mirror.execute_preview(project, destination, mirror.prepare_mirror(project, destination))
    mirror.restore_verification(project, destination, full=True)
    marker = mirror.read_marker(mirror.destination_context(project, destination))
    marker['last_full_verification']['destination'] = '/different/mirror'
    (destination / mirror.MARKER).write_text(json.dumps(marker))
    assert mirror.backup_status(project, destination)['last_full_verify'] is None


def test_status_rejects_incomplete_journal(cli_mirror):
    project, destination, mount = cli_mirror
    mirror.execute_preview(project, destination, mirror.prepare_mirror(project, destination))
    marker = mirror.read_marker(mirror.destination_context(project, destination))
    journal = destination / mirror.CONTROL / ('journal-' + marker['last_verified_plan'] + '.sqlite3')
    with sqlite3.connect(journal) as database:
        database.execute("UPDATE operation SET phase='interrupted'")
    marker['journal_sha256'] = mirror.file_hash(journal)
    (destination / mirror.MARKER).write_text(json.dumps(marker))
    assert mirror.backup_status(project, destination)['last_mirror'] is None


def test_full_verification_sqlite_progress_preserves_payload(cli_mirror):
    project, destination, mount = cli_mirror
    with sqlite3.connect(project / 'data/example.sqlite3') as database:
        database.execute('CREATE TABLE example(value TEXT)')
        database.execute("INSERT INTO example VALUES('fixture')")
    mirror.execute_preview(project, destination, mirror.prepare_mirror(project, destination))
    payload = {path: mirror.file_hash(path) for directory in ('data', 'preferences')
               for path in (destination / directory).rglob('*') if path.is_file()}
    events = []
    result = mirror.restore_verification(project, destination, full=True, progress=events.append)
    assert result['verification']['sqlite_databases_verified'] == 1
    assert events[-1]['phase'] == 'complete' and events[-1]['sqlite_databases_verified'] == 1
    assert all(mirror.file_hash(path) == checksum for path, checksum in payload.items())


@pytest.mark.parametrize('answer', ['yes', 'no', ''])
def test_cli_requires_second_destructive_confirmation(cli_mirror, monkeypatch, answer):
    import cli
    project, destination, mount = cli_mirror
    (destination / 'obsolete').write_text('unique old data')
    answers = iter(['yes', answer])
    prompts = []
    def confirm(prompt):
        prompts.append(prompt)
        return next(answers)
    monkeypatch.setattr('builtins.input', confirm)
    cli.cmd_backup(cli.build_parser().parse_args(['backup', 'mirror']))
    assert len(prompts) == 2 and 'Additionally authorize' in prompts[1]
    assert (destination / 'obsolete').exists() == (answer != 'yes')
    assert (destination / 'data/current').exists() == (answer == 'yes')


def test_cli_unattended_deletion_requires_advanced_exact_approval(cli_mirror, monkeypatch):
    import cli
    project, destination, mount = cli_mirror
    (destination / 'obsolete').write_text('unique old data')
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(SystemExit) as error:
        cli.cmd_backup(cli.build_parser().parse_args(['backup', 'mirror', '--apply']))
    assert error.value.code == 1
    assert (destination / 'obsolete').read_text() == 'unique old data'
    assert not (destination / mirror.CONTROL).exists()


@pytest.mark.parametrize('side', ['source', 'destination'])
def test_cli_stale_preview_refuses_without_replanning(cli_mirror, monkeypatch, side, capsys):
    import cli
    project, destination, mount = cli_mirror
    root = project if side == 'source' else destination
    def confirm(prompt):
        (root / 'changed-after-preview').write_text('new data')
        return 'yes'
    monkeypatch.setattr('builtins.input', confirm)
    with pytest.raises(SystemExit):
        cli.cmd_backup(cli.build_parser().parse_args(['backup', 'mirror']))
    assert 'Restart preview' in capsys.readouterr().err
    assert not (destination / 'data/current').exists()
    assert not (destination / mirror.CONTROL).exists()


@pytest.mark.parametrize('phase', ['before', 'copy', 'verify'])
def test_shared_session_cancels_safely_and_resumes(cli_mirror, phase):
    project, destination, mount = cli_mirror
    preview = mirror.prepare_mirror(project, destination)
    stopping = phase == 'before'
    def progress(event):
        nonlocal stopping
        if event['phase'] == phase:
            stopping = True
    with pytest.raises(mirror.MirrorCancelled):
        mirror.execute_preview(project, destination, preview, progress=progress, cancelled=lambda: stopping)
    if phase == 'before':
        assert not (destination / mirror.CONTROL).exists()
    else:
        journal = destination / mirror.CONTROL / ('journal-' + preview['plan_digest'] + '.sqlite3')
        with sqlite3.connect(journal) as database:
            assert database.execute('SELECT phase FROM operation').fetchone()[0] == 'interrupted'
    result = mirror.execute_preview(project, destination, preview)
    assert result['sha256'] and result['files_verified'] == 2


def test_preparation_does_not_publish_over_existing_evidence_or_verify_mirror(cli_mirror, monkeypatch):
    project, destination, mount = cli_mirror
    report = project / mirror.REPORT_RELATIVE
    evidence = report / 'reconciliation-summary.json'
    evidence.write_text('existing evidence')
    def forbidden(*args, **kwargs):
        raise AssertionError('Ordinary preparation must not run restoration verification or old report publication')
    monkeypatch.setattr(mirror, 'restore_verification', forbidden)
    monkeypatch.setattr(mirror, 'publish_preview', forbidden)
    first = mirror.prepare_mirror(project, destination)
    second = mirror.prepare_mirror(project, destination)
    assert first['plan'] != second['plan']
    assert evidence.read_text() == 'existing evidence'
    assert not (destination / mirror.CONTROL).exists()


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
        return {'preview': 'shared-service-result', 'plan': 'fixture.sqlite3', 'plan_digest': 'digest'}
    monkeypatch.setattr(mirror, 'prepare_mirror', preview)
    monkeypatch.setattr(mirror, 'preview_text', lambda result: result['preview'])
    cli.cmd_backup(SimpleNamespace(backup_subcommand='mirror', project=str(project), apply=False, checksum=False, dry_run=True))
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
        return {'apply_blocked': [], 'mode': 'guarded mirror', 'plan': 'fixture.sqlite3', 'plan_digest': 'digest'}
    def forbidden(*args, **kwargs):
        raise AssertionError('Normal backup must never use legacy additive copy or implicit apply')
    monkeypatch.setattr(mirror, 'prepare_mirror', preview)
    monkeypatch.setattr(mirror, 'preview_text', lambda result: result['mode'])
    monkeypatch.setattr(cli, '_backup_confirm', lambda prompt: False)
    monkeypatch.setattr(mirror, 'apply_mirror', forbidden)
    assert not hasattr(cli, '_backup_update')
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
    def preview(*args, **kwargs):
        calls.append('preview')
        return {'plan': 'fixture.sqlite3', 'plan_digest': 'digest'}
    monkeypatch.setattr(mirror, 'prepare_mirror', preview)
    monkeypatch.setattr(mirror, 'preview_text', lambda result: 'preview')
    def forbidden(*args, **kwargs):
        raise AssertionError('Dry run must override apply')
    monkeypatch.setattr(mirror, 'apply_mirror', forbidden)
    monkeypatch.setattr(cli, '_backup_confirm', forbidden)
    cli.cmd_backup(cli.build_parser().parse_args(['backup', *arguments]))
    assert calls == ['preview']


def test_normal_backup_apply_delegates_to_guarded_mirror(environment, monkeypatch):
    import cli
    project, destination, mount = environment
    (project / 'preferences/preferences.json').write_text(json.dumps({'backup_path': str(destination)}))
    preview = ready(environment)
    calls = []
    def apply(source, backup, plan, **kwargs):
        calls.append((source, backup, plan, kwargs))
        return {'verified': True}
    monkeypatch.setattr(mirror, 'apply_mirror', apply)
    args = cli.build_parser().parse_args(['backup', 'update', '--project', str(project),
                                          '--apply', '--plan', preview['plan'], '--approve-plan', preview['plan_digest']])
    cli.cmd_backup(args)
    assert len(calls) == 1
    source, backup, plan, options = calls[0]
    assert (source, backup, plan) == (str(project), str(destination), preview['plan'])
    assert options['approval'] == options['expected_digest'] == preview['plan_digest']


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