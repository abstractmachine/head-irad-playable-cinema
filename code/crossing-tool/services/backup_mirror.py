"""Verified project mirroring; previews never write to the external destination."""
from contextlib import contextmanager, ExitStack
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import struct
import subprocess
import tempfile
import time
import unicodedata
import uuid

MARKER = '.crossing-mirror.json'
CONTROL = '.crossing-mirror-control'
REPORT_RELATIVE = 'outputs/maintenance/backup-mirror'
REQUIRED_TOPS = ('data', 'media', 'preferences')
DEFAULT_LIMITS = {'max_files': 0, 'max_bytes': 0, 'max_percentage': 0.0}


class MirrorError(RuntimeError):
    pass


class MirrorCancelled(MirrorError):
    pass


def checkpoint(progress, cancelled, phase, **details):
    if cancelled is not None and cancelled():
        raise MirrorCancelled('Cancelled at a safe checkpoint; any installed files are journaled. No verified completion claimed.')
    if progress is not None:
        progress({'phase': phase, **details})


def mounted_filesystems():
    result = subprocess.run(
        ['findmnt', '--json', '--list', '--output', 'TARGET,SOURCE,FSTYPE,OPTIONS,LABEL,UUID,MAJ:MIN'],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)['filesystems']


def destination_context(project, configured, *, label='PLAYABLE'):
    project = Path(project).resolve(strict=True)
    if not project.is_dir() or project == Path('/') or not os.access(project, os.R_OK | os.X_OK):
        raise MirrorError('Source project must be a present, readable directory')
    if not configured:
        raise MirrorError('Backup path is not configured')
    matches = [item for item in mounted_filesystems() if item.get('label') == label]
    if len(matches) != 1:
        raise MirrorError('Expected external disk is absent or label is ambiguous')
    mount = matches[0]
    if not mount.get('uuid') or 'rw' not in mount['options'].split(','):
        raise MirrorError('Mounted disk lacks an identity or is not writable')
    mountpoint = Path(mount['target']).resolve(strict=True)
    destination = mountpoint / 'backups' / project.name
    configured = Path(configured)
    historical = any(configured == Path(base) / label / 'backups' / project.name
                     for base in ('/media/' + Path.home().name, '/run/media/' + Path.home().name))
    if configured != destination and not historical:
        raise MirrorError('Configuration does not identify this project backup on the mounted disk')
    if not destination.is_dir() or destination.is_symlink():
        raise MirrorError('Existing backup directory is required; no internal fallback')
    if destination.resolve() != destination or (mountpoint / 'backups').is_symlink():
        raise MirrorError('Backup ancestry must not contain symlinks')
    if destination.stat().st_dev != mountpoint.stat().st_dev:
        raise MirrorError('Backup directory is not on the expected mounted filesystem')
    if project.is_relative_to(destination) or destination.is_relative_to(project):
        raise MirrorError('Source and destination overlap')
    if not os.access(destination, os.W_OK | os.X_OK):
        raise MirrorError('Backup directory is not writable')
    return {'project': str(project), 'destination': str(destination), 'configured': str(configured),
            'source_device': project.stat().st_dev, 'source_inode': project.stat().st_ino,
            'destination_inode': destination.stat().st_ino,
            'destination_device': destination.stat().st_dev,
            'mount': mount, 'configuration_relocated': str(configured) != str(destination)}


def read_marker(context):
    path = Path(context['destination']) / MARKER
    if not path.exists():
        return None
    if path.is_symlink():
        raise MirrorError('Mirror marker must not be a symlink')
    marker = json.loads(path.read_text())
    expected = {'schema_version': 1, 'purpose': 'crossing-project-mirror',
                'source_project': context['project'], 'project': Path(context['project']).name,
                'device_uuid': context['mount']['uuid'], 'mirror_policy': 'current-project-state'}
    if any(marker.get(key) != value for key, value in expected.items()):
        raise MirrorError('Mirror identity does not match source/project/device/policy')
    return marker


def verify_protected_siblings(context, baseline, current, deletion_paths, *, content_hashes=None):
    """Only exFAT's two macOS directory inode fields may differ from baseline."""
    mount = Path(context['mount']['target'])
    destination = Path(context['destination'])
    if set(baseline) != set(current):
        raise MirrorError('Protected sibling membership changed')
    exceptions = []
    for name, before in baseline.items():
        path = Path(name)
        after = current[name]
        if (path.parent not in (mount, mount / 'backups') or path in (destination, mount / 'backups')
                or '..' in path.parts or len(before) != 6 or len(after) != 6):
            raise MirrorError('Invalid protected sibling baseline: ' + name)
        if before[0] != context['destination_device'] or after[0] != before[0]:
            raise MirrorError('Protected sibling device changed: ' + name)
        mac_directory = path in (mount / '.Spotlight-V100', mount / '.fseventsd')
        if mac_directory and (not stat.S_ISDIR(before[2]) or not stat.S_ISDIR(after[2])):
            raise MirrorError('Protected macOS path is not a directory: ' + name)
        if before == after:
            continue
        if (context['mount']['fstype'] == 'exfat' and mac_directory
                and before[:1] + before[2:] == after[:1] + after[2:]):
            exceptions.append({'path': name, 'before_inode': before[1], 'after_inode': after[1],
                               'reason': 'Explicit exFAT macOS-directory inode instability exception; other recorded fields match'})
        else:
            raise MirrorError('Protected sibling metadata changed: ' + name)
    checked = 0
    for relative in deletion_paths:
        path = Path(relative)
        if (not relative or path.is_absolute() or '..' in path.parts or '.' in relative.split('/')
                or path.as_posix() != relative or not (destination / path).is_relative_to(destination)):
            raise MirrorError('Journaled deletion escapes approved destination: ' + relative)
        checked += 1
    verified_hashes = 0
    for name, checksum in (content_hashes or {}).items():
        path = Path(name)
        owners = [Path(sibling) for sibling in baseline if path == Path(sibling) or path.is_relative_to(Path(sibling))]
        if len(owners) != 1 or '..' in path.parts:
            raise MirrorError('Invalid sibling content baseline path: ' + name)
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != mount and parent.is_relative_to(mount)):
            raise MirrorError('Unsafe sibling content baseline path: ' + name)
        if file_hash(path) != checksum:
            raise MirrorError('Protected sibling content changed: ' + name)
        verified_hashes += 1
    return {'metadata_verified': True, 'exfat_inode_exceptions': exceptions,
            'journaled_paths_confined_to_destination': checked, 'baseline_content_hashes_verified': verified_hashes,
            'sibling_contents_proven_unchanged': False,
            'verification_limits': 'Recorded sibling metadata and deletion-path confinement are verified. Available file hashes, if any, cover only those files; no complete pre-clear recursive content baseline exists. Unrelated content changes cannot be ruled out.'}


def verify_cleared_backup(context, baseline, journal, current_siblings, bootstrap, checksum):
    check_context(context)
    destination = Path(context['destination'])
    if (baseline['destination'] != str(destination)
            or baseline['destination_inode'] != context['destination_inode']
            or baseline['destination_device'] != context['destination_device']):
        raise MirrorError('Approved backup root was replaced')
    for mounted in mounted_filesystems():
        target = Path(mounted['target'])
        if target == destination or target.is_relative_to(destination):
            raise MirrorError('Nested mount prevents completed-clear verification')
    descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_dev != baseline['destination_device'] or info.st_ino != baseline['destination_inode']:
            raise MirrorError('Approved backup descriptor identity changed')
        with os.scandir(descriptor) as entries:
            if next(entries, None) is not None:
                raise MirrorError('Approved backup destination is not empty')
        if journal.execute("SELECT 1 FROM clear_entries WHERE state!='deleted' LIMIT 1").fetchone():
            raise MirrorError('Clear journal retains unfinished deletion intents')
        siblings = verify_protected_siblings(context, baseline['siblings'], current_siblings,
                    (row[0] for row in journal.execute('SELECT path FROM clear_entries')),
                    content_hashes=baseline.get('sibling_content_hashes'))
        bootstrap = Path(bootstrap)
        if bootstrap.is_relative_to(destination) or not bootstrap.is_relative_to(Path(context['mount']['target'])):
            raise MirrorError('Bootstrap is not outside the approved clear target on the mounted disk')
        for ancestor in (bootstrap, *bootstrap.parents):
            if ancestor.is_symlink():
                raise MirrorError('Bootstrap ancestry contains a symlink')
            if ancestor == Path(context['mount']['target']):
                break
        if bootstrap.stat().st_dev != context['destination_device'] or file_hash(bootstrap) != checksum:
            raise MirrorError('Bootstrap census differs after clearing')
        check_context(context)
        with os.scandir(descriptor) as entries:
            if next(entries, None) is not None:
                raise MirrorError('Approved backup destination changed during verification')
        return {'destination': str(destination), 'root_preserved': True, 'empty_destination_verified': True,
                'siblings': siblings, 'entries_deleted': siblings['journaled_paths_confined_to_destination'],
                'bootstrap_sha256_retained': checksum, 'formatted': False, 'deletions_repeated': False}
    finally:
        os.close(descriptor)


def policy():
    return {'schema_version': 1, 'mirror_policy': 'current-project-state',
            'scope': 'All project entries, including dot directories, archive, media, models and outputs',
            'required_top_directories': list(REQUIRED_TOPS),
            'excluded_paths': [MARKER, CONTROL, REPORT_RELATIVE],
            'excluded_path_semantics': 'Preserve existing destination entries under excluded paths; never infer deletion from exclusion',
            'symlinks': 'Inherited rsync --no-links: do not transfer links or traverse them; protect matching destination subtrees; record link metadata for restore',
            'special_files': 'Report and block apply; no device/socket/FIFO copy',
            'maintenance_workspace': 'This live report/journal workspace is a documented control namespace, not payload. Adoption explicitly approves this control exception.',
            'comparison': 'Preview uses size and filesystem-rounded mtime; equal metadata is UNKNOWN until fresh SHA256 verification',
            'sqlite': 'Hold a read transaction during consistent raw-file copy; reject WAL/off journal modes and nonempty sidecars. Never checkpoint or mutate source.',
            'metadata': 'Retain Linux mode/uid/gid/mtime/link/hardlink inventory in the verified plan; exFAT data copies alone are not a Linux restore',
            'deletion_limits': DEFAULT_LIMITS,
            'deletion_limits_reason': 'No verified previous mirror or approved research-retention baseline exists. Default permits zero unattended deletions; every first-reconciliation deletion requires exact plan approval.',
            'first_reconciliation': 'Backup-only files require human review. A plan digest approval is mandatory, not a filename-based obsolescence heuristic.'}


def excluded(path):
    return any(path == prefix or path.startswith(prefix + '/') for prefix in (MARKER, CONTROL, REPORT_RELATIVE))


def readonly_database(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def file_hash(path):
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as handle:
        before = os.fstat(handle.fileno())
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if identity(before) != identity(after) or identity(Path(path).lstat()) != identity(before):
        raise MirrorError('File changed during checksum: ' + str(path))
    return digest.hexdigest()


def identity(info):
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev, info.st_mode)


def require_source(project, expected_tops=None):
    project = Path(project)
    for name in REQUIRED_TOPS:
        if not (project / name).is_dir() or (project / name).is_symlink():
            raise MirrorError('Required source directory unavailable: ' + name)
    if expected_tops and any(not (project / name).is_dir() for name in expected_tops):
        raise MirrorError('Previously expected top-level source directory disappeared')


def new_inventory(database):
    database.executescript('''
    CREATE TABLE inventory(side TEXT,path TEXT,kind TEXT,size INTEGER,allocated INTEGER,mtime INTEGER,
      ctime INTEGER,inode INTEGER,device INTEGER,mode INTEGER,uid INTEGER,gid INTEGER,nlink INTEGER,
      target TEXT,sha256 TEXT,PRIMARY KEY(side,path)) WITHOUT ROWID;
    CREATE TABLE issues(side TEXT,path TEXT,reason TEXT);
    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE actions(path TEXT PRIMARY KEY,action TEXT,classification TEXT,source_size INTEGER,
      destination_size INTEGER,reason TEXT) WITHOUT ROWID;
    ''')


def inventory_tree(database, root, side, *, checksums=False, progress=None, cancelled=None):
    root = Path(root)
    pending = [root]
    batch = []
    count = 0
    while pending:
        checkpoint(progress, cancelled, 'preview', side=side, entries=count + len(batch))
        directory = pending.pop()
        with os.scandir(directory) as entries:
            folded = {}
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                info = entry.stat(follow_symlinks=False)
                kind = 'file' if stat.S_ISREG(info.st_mode) else 'directory' if stat.S_ISDIR(info.st_mode) else 'symlink' if stat.S_ISLNK(info.st_mode) else 'special'
                if excluded(relative):
                    kind = 'excluded'
                if side == 'source' and kind not in ('excluded', 'symlink'):
                    key = unicodedata.normalize('NFC', entry.name).casefold()
                    if key in folded:
                        database.execute('INSERT INTO issues VALUES(?,?,?)', (side, relative, 'Case/Unicode collision with ' + folded[key]))
                    folded[key] = entry.name
                    if any(ord(character) < 32 or character in '\\:*?"<>|' for character in entry.name) or entry.name.endswith((' ', '.')):
                        database.execute('INSERT INTO issues VALUES(?,?,?)', (side, relative, 'Filename not portable to exFAT/Windows'))
                if kind == 'special':
                    database.execute('INSERT INTO issues VALUES(?,?,?)', (side, relative, 'Special file requires a separately approved preservation strategy'))
                target = os.readlink(path) if kind == 'symlink' else None
                checksum = file_hash(path) if checksums and kind == 'file' else None
                batch.append((side, relative, kind, info.st_size, info.st_blocks * 512, info.st_mtime_ns,
                              info.st_ctime_ns, info.st_ino, info.st_dev, info.st_mode, info.st_uid,
                              info.st_gid, info.st_nlink, target, checksum))
                if kind == 'directory':
                    pending.append(path)
                if len(batch) >= 2048:
                    checkpoint(progress, cancelled, 'preview', side=side, entries=count + len(batch))
                    database.executemany('INSERT INTO inventory VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', batch)
                    database.commit()
                    count += len(batch)
                    batch.clear()
                    if count % 100352 == 0:
                        print(f'{side}: {count:,} entries inventoried', flush=True)
    if batch:
        database.executemany('INSERT INTO inventory VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', batch)
    database.commit()


def inventory_fingerprint(database, side):
    digest = hashlib.sha256()
    for row in database.execute('SELECT path,kind,size,mtime,ctime,inode,device,mode,target FROM inventory WHERE side=? ORDER BY path', (side,)):
        values = row if row[1] == 'file' else (row[0], row[1], row[7], row[8])
        if row[1] != 'excluded':
            digest.update(json.dumps(values, separators=(',', ':')).encode())
    return digest.hexdigest()


def metadata(database, key, value=None):
    if value is not None:
        database.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (key, json.dumps(value)))
    else:
        row = database.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None


def classify(database, *, exfat, first):
    protected = {row[0] for row in database.execute("SELECT path FROM inventory WHERE side='source' AND kind IN ('symlink','excluded','special')")}
    time_unit = 2_000_000_000 if exfat else 1
    cursor = database.execute('''SELECT source.path,source.kind,source.size,source.mtime,source.sha256,
      destination.kind,destination.size,destination.mtime,destination.sha256
      FROM inventory source LEFT JOIN inventory destination ON destination.side='destination'
      AND destination.path=source.path WHERE source.side='source' ''')
    for path, kind, size, mtime, checksum, other_kind, other_size, other_mtime, other_checksum in cursor:
        if kind in ('excluded', 'symlink', 'special'):
            action, category, reason = 'protect', 'PROTECTED', 'Excluded/control/link/special namespace; destination retained'
        elif other_kind is None:
            action, category, reason = ('copy' if kind == 'file' else 'mkdir'), 'SAFE TO MIRROR', 'Source-only current project entry'
        elif kind != other_kind:
            action, category, reason = 'conflict', 'REQUIRES REVIEW', 'File/directory/type conflict; automatic replacement disabled'
        elif kind == 'directory':
            action, category, reason = 'directory', 'SAFE TO MIRROR', 'Directory structure present on both sides'
        elif checksum and other_checksum:
            action, category, reason = ('same' if checksum == other_checksum else 'update'), 'SAFE TO MIRROR', 'Fresh content SHA256 compared'
        elif size != other_size or mtime // time_unit != other_mtime // time_unit:
            action, category, reason = 'update', 'SAFE TO MIRROR', 'Size/time differs; transfer estimate conservative, content equality not asserted'
        else:
            action, category, reason = 'check', 'UNKNOWN', 'Metadata agrees; contents require fresh checksum before verified status'
        database.execute('INSERT INTO actions VALUES(?,?,?,?,?,?)', (path, action, category, size if kind == 'file' else 0, other_size or 0, reason))
    for path, kind, size in database.execute("SELECT destination.path,destination.kind,destination.size FROM inventory destination LEFT JOIN inventory source ON source.side='source' AND source.path=destination.path WHERE destination.side='destination' AND source.path IS NULL"):
        components = path.split('/')
        protect = excluded(path) or any('/'.join(components[:length]) in protected for length in range(1, len(components) + 1))
        if kind == 'directory' and not protect:
            protect = database.execute("SELECT 1 FROM inventory WHERE side='destination' AND path>=? AND path<? AND kind IN ('symlink','special','excluded') LIMIT 1",
                                       (path + '/', path + '0')).fetchone() is not None
        if protect or kind in ('symlink', 'special', 'excluded'):
            action, category, reason = 'protect', 'PROTECTED', 'Outside approved comparable payload scope'
        else:
            action = 'rmdir' if kind == 'directory' else 'delete'
            category = 'REQUIRES REVIEW' if first else 'SAFE TO MIRROR'
            reason = 'Backup-only; no provenance proves intentional removal versus unique research or accidental source loss' if first else 'Absent from current approved source scope'
            if path.startswith('data/silhouettes/'):
                counterpart = 'archive/' + path[len('data/'):]
                if database.execute("SELECT 1 FROM inventory WHERE side='source' AND path=?", (counterpart,)).fetchone():
                    reason += '; exact source archive counterpart exists at ' + counterpart + ' (contents unverified)'
        database.execute('INSERT INTO actions VALUES(?,?,?,?,?,?)', (path, action, category, 0, size if kind == 'file' else 0, reason))
    database.commit()


def summarize(database, context):
    counts = {row[0]: {'count': row[1], 'source_bytes': row[2], 'destination_bytes': row[3]}
              for row in database.execute('SELECT action,count(*),sum(source_size),sum(destination_size) FROM actions GROUP BY action')}
    source_count = database.execute("SELECT count(*) FROM inventory WHERE side='source' AND kind='file'").fetchone()[0]
    destination_size = database.execute("SELECT coalesce(sum(size),0) FROM inventory WHERE side='destination' AND kind='file'").fetchone()[0]
    transfer = sum(counts.get(action, {}).get('source_bytes', 0) for action in ('copy', 'update'))
    recover = counts.get('delete', {}).get('destination_bytes', 0)
    final = destination_size + counts.get('copy', {}).get('source_bytes', 0) + counts.get('update', {}).get('source_bytes', 0) - counts.get('update', {}).get('destination_bytes', 0) - recover
    usage = shutil.disk_usage(context['destination'])
    return {'actions': counts, 'source_file_count': source_count, 'bytes_to_transfer': transfer,
            'estimated_delete_recovery_bytes': recover, 'destination_payload_bytes': destination_size,
            'estimated_final_payload_bytes': final, 'capacity_bytes': usage.total, 'available_bytes': usage.free,
            'issues': database.execute('SELECT count(*) FROM issues').fetchone()[0],
            'content_verified': False,
            'estimates': 'Apparent payload bytes, not guaranteed block recovery; protected excluded subtree sizes not traversed; update count is conservative until checksummed'}


def plan_mirror(project, configured, report_dir=None, *, checksums=False, progress=None, cancelled=None):
    checkpoint(progress, cancelled, 'preview')
    context = destination_context(project, configured)
    check_context(context)
    require_source(context['project'])
    marker = read_marker(context)
    report = Path(report_dir or Path(context['project']) / REPORT_RELATIVE).resolve()
    if report != Path(context['project']) / REPORT_RELATIVE:
        raise MirrorError('Plans must use the reserved project maintenance workspace')
    report.mkdir(parents=True, exist_ok=True)
    plan = report / ('plan-' + uuid.uuid4().hex + '.sqlite3')
    with sqlite3.connect(plan) as database:
        new_inventory(database)
        metadata(database, 'context', context)
        metadata(database, 'policy', policy())
        metadata(database, 'marker', marker)
        inventory_tree(database, context['project'], 'source', checksums=checksums, progress=progress, cancelled=cancelled)
        inventory_tree(database, context['destination'], 'destination', checksums=checksums, progress=progress, cancelled=cancelled)
        if not database.execute("SELECT 1 FROM inventory WHERE side='source' AND kind='file' LIMIT 1").fetchone():
            raise MirrorError('Unexpectedly empty source; deletion plan refused')
        first = marker is None or not marker.get('reconciled')
        classify(database, exfat=context['mount']['fstype'] == 'exfat', first=first)
        summary = summarize(database, context)
        metadata(database, 'source_fingerprint', inventory_fingerprint(database, 'source'))
        metadata(database, 'destination_fingerprint', inventory_fingerprint(database, 'destination'))
        metadata(database, 'expected_tops', [row[0] for row in database.execute("SELECT path FROM inventory WHERE side='source' AND kind='directory' AND instr(path,'/')=0")])
        metadata(database, 'summary', summary)
        metadata(database, 'created_at', time.time())
        metadata(database, 'first_reconciliation', first)
        database.commit()
    summary.update({'plan': str(plan), 'plan_digest': file_hash(plan), 'context': context,
                    'marker_present': marker is not None, 'first_reconciliation': first,
                    'apply_blocked': ['Explicit adoption required'] if marker is None else [],
                    'checksum_mode': checksums, 'census_backup_verified': False})
    return summary


def prepare_mirror(project, configured, *, plan=None, checksums=False, progress=None, cancelled=None):
    with operation_lock(project):
        checkpoint(progress, cancelled, 'preview')
        if plan is None:
            preview = plan_mirror(project, configured, checksums=checksums, progress=progress, cancelled=cancelled)
            plan = preview['plan']
            os.chmod(plan, 0o444)
        plan, digest = approved_plan(plan)
        with readonly_database(plan) as database:
            context = reviewed_context(database, project, configured)
            check_context(context)
            marker = read_marker(context)
            result = dict(metadata(database, 'summary'))
            largest_update = database.execute("SELECT coalesce(max(source_size),0) FROM actions WHERE action='update'").fetchone()[0]
            conflicts = database.execute("SELECT count(*) FROM actions WHERE action='conflict'").fetchone()[0]
            required = result['actions'].get('copy', {}).get('source_bytes', 0) + largest_update + plan.stat().st_size
            blocks = []
            if marker is None:
                blocks.append('Explicit adoption required; use the advanced adopt workflow')
            if result['issues'] or conflicts:
                blocks.append('Unresolved filename, special-file or type conflicts')
            available = shutil.disk_usage(context['destination']).free
            if available < required:
                blocks.append('Insufficient copy-before-delete capacity')
            result.update(plan=str(plan), plan_digest=digest, context=context,
                          available_bytes=available, required_capacity_bytes=required,
                          apply_blocked=blocks, marker_present=marker is not None,
                          first_reconciliation=metadata(database, 'first_reconciliation'))
        checkpoint(progress, cancelled, 'preview_ready')
        return result


def preview_text(preview):
    actions = preview['actions']
    count = lambda action: actions.get(action, {}).get('count', 0)
    lines = [f"Destination: {preview['context']['destination']}",
             f"Source files: {preview['source_file_count']:,}",
             f"Already present: {count('same') + count('check'):,} (metadata matches are not verified)",
             f"Copy: {count('copy'):,}; update: {count('update'):,}",
             f"Delete: {count('delete'):,} files, {count('rmdir'):,} directories; {preview['estimated_delete_recovery_bytes']:,} bytes",
             f"Transfer: {preview['bytes_to_transfer']:,} bytes",
             f"Capacity: {preview['capacity_bytes']:,} bytes; available: {preview['available_bytes']:,} bytes",
             f"Copy-before-delete required: {preview['required_capacity_bytes']:,} bytes",
             'Verification: every source file is checksummed before deletion and at completion.']
    lines.extend('BLOCKED: ' + message for message in preview['apply_blocked'])
    return '\n'.join(lines)


def execute_preview(project, configured, preview, *, approve_delete=False, approval=None, progress=None, cancelled=None):
    checkpoint(progress, cancelled, 'preflight')
    if preview['apply_blocked']:
        raise MirrorError('; '.join(preview['apply_blocked']))
    digest = preview['plan_digest']
    approved_plan(preview['plan'], digest)
    deleting = any(preview['actions'].get(action, {}).get('count', 0) for action in ('delete', 'rmdir'))
    if deleting and not approve_delete and approval != digest:
        raise MirrorError('Deletion requires additional explicit approval of this exact preview; restart with interactive review or advanced --approve-plan')
    return apply_mirror(project, configured, preview['plan'], approval=digest if approve_delete else approval,
                        expected_digest=digest, progress=progress, cancelled=cancelled)


@contextmanager
def operation_lock(project):
    report = Path(project) / REPORT_RELATIVE
    report.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(report / '.backup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    catalog_descriptor = None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MirrorError('Another backup operation holds the project lock') from error
        catalog_lock = Path(project) / 'data/silhouettes/.catalog-extraction.lock'
        if catalog_lock.exists():
            catalog_descriptor = os.open(catalog_lock, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fcntl.flock(catalog_descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise MirrorError('Conflicting silhouette extraction/recheck operation') from error
        yield
    finally:
        if catalog_descriptor is not None:
            os.close(catalog_descriptor)
        os.close(descriptor)


def conflicting_operations():
    import re
    result = subprocess.run(['ps', '-eo', 'pid=,args='], check=True, capture_output=True, text=True)
    ancestors = set()
    process = os.getpid()
    while process and process not in ancestors:
        ancestors.add(process)
        try:
            process = int(Path(f'/proc/{process}/stat').read_text().rpartition(')')[2].split()[1])
        except (OSError, ValueError, IndexError):
            break
    pattern = re.compile(r'\bcrossing\s+(?:backup\s+(?:update|mirror|adopt)|index\s+silhouette\s+(?:extract|recheck)|silhouette\s+archive\s+(?:execute|rollback)|.*migration.*--apply)\b')
    return [line.strip() for line in result.stdout.splitlines()
            if int(line.strip().split(maxsplit=1)[0]) not in ancestors and pattern.search(line)]


def check_context(context, *, fast=False):
    if fast:
        source = Path(context['project']).lstat()
        destination = Path(context['destination']).lstat()
        if (source.st_ino != context['source_inode'] or source.st_dev != context['source_device']
                or destination.st_ino != context['destination_inode'] or destination.st_dev != context['destination_device']
                or not stat.S_ISDIR(source.st_mode) or not stat.S_ISDIR(destination.st_mode)):
            raise MirrorError('Source/destination unmounted or replaced during operation')
        return context
    current = destination_context(context['project'], context['configured'])
    for key in ('project', 'destination', 'source_device', 'source_inode', 'destination_inode', 'destination_device'):
        if current[key] != context[key]:
            raise MirrorError('Source/destination identity changed: ' + key)
    for key in ('uuid', 'source', 'maj:min', 'fstype'):
        if current['mount'].get(key) != context['mount'].get(key):
            raise MirrorError('Mounted device identity changed')
    if conflicting_operations():
        raise MirrorError('Conflicting backup/migration operation is running')
    return current


def validate_inventory(database, side, root, *, allow_paths=(), context=None):
    root = Path(root)
    allowed = set(allow_paths)
    exfat_destination = False
    removal_directories = set()
    if side == 'destination' and context is not None:
        if root != Path(context['destination']):
            raise MirrorError('Inventory destination does not match the reviewed context')
        check_context(context)
        exfat_destination = context['mount']['fstype'] == 'exfat'
        if exfat_destination:
            removal_directories = {row[0] for row in database.execute("SELECT path FROM actions WHERE action='rmdir'")}
    expected_count = 0
    for row in database.execute('SELECT path,kind,size,mtime,ctime,inode,device,mode FROM inventory WHERE side=?', (side,)):
        path, kind = row[:2]
        if kind == 'excluded' or path in allowed:
            continue
        expected_count += 1
        try:
            info = (root / path).lstat()
        except FileNotFoundError as error:
            raise MirrorError('Inventory entry disappeared: ' + path) from error
        if kind == 'file' and identity(info) != tuple(row[2:]):
            raise MirrorError('Inventory file changed: ' + path)
        require_inode = not exfat_destination or path in removal_directories
        if kind == 'directory' and (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or (require_inode and info.st_ino != row[5]) or info.st_dev != row[6]):
            reason = 'type' if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) else 'identity'
            observed = 'directory' if stat.S_ISDIR(info.st_mode) else 'symlink' if stat.S_ISLNK(info.st_mode) else 'regular file' if stat.S_ISREG(info.st_mode) else 'special'
            raise MirrorError(
                f'Inventory {side} directory {reason} changed: {path}; '
                f'expected type=directory inode={row[5]} device={row[6]} mode={oct(row[7])}; '
                f'observed type={observed} inode={info.st_ino} device={info.st_dev} mode={oct(info.st_mode)}'
            )
        if kind == 'symlink' and not stat.S_ISLNK(info.st_mode):
            raise MirrorError('Source link changed type: ' + path)
    current_count = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path).relative_to(root).as_posix()
                if excluded(path):
                    continue
                if path not in allowed:
                    current_count += 1
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
    if current_count != expected_count:
        raise MirrorError('Source/destination file population changed during synchronization')


def approved_plan(plan, approval=None):
    plan = Path(plan).resolve(strict=True)
    digest = file_hash(plan)
    if approval is not None and approval != digest:
        raise MirrorError('Approval does not match exact reconciliation plan digest')
    return plan, digest


def reviewed_context(database, project, configured):
    recorded = metadata(database, 'context')
    current = destination_context(project, configured)
    required = {'project', 'destination', 'configured', 'source_device', 'source_inode',
                'destination_inode', 'mount', 'configuration_relocated'}
    if (not required.issubset(recorded) or any(current.get(key) != value for key, value in recorded.items())
            or set(current) - set(recorded) - {'destination_device'}):
        raise MirrorError('Reviewed plan source/destination/device/configuration identity changed')
    return current


def adopt_mirror(project, configured, plan, approval, *, limits=None):
    if not approval:
        raise MirrorError('Adoption requires explicit review and exact plan digest approval')
    plan, digest = approved_plan(plan, approval)
    with operation_lock(project), readonly_database(plan) as database:
        context = reviewed_context(database, project, configured)
        check_context(context)
        if read_marker(context) is not None:
            raise MirrorError('Mirror already adopted; marker replacement disabled')
        require_source(project, metadata(database, 'expected_tops'))
        validate_inventory(database, 'source', project)
        validate_inventory(database, 'destination', context['destination'])
        limits = limits or DEFAULT_LIMITS.copy()
        if (set(limits) != set(DEFAULT_LIMITS) or any(not math.isfinite(value) or value < 0 for value in limits.values())
            or limits['max_percentage'] > 100):
            raise MirrorError('Deletion limits must be explicit nonnegative file/byte/percentage values')
        marker = {'schema_version': 1, 'purpose': 'crossing-project-mirror', 'project': Path(project).name,
                  'source_project': context['project'], 'mirror_policy': 'current-project-state',
                  'device_uuid': context['mount']['uuid'], 'reconciled': False,
                  'adoption_plan_digest': digest, 'expected_tops': metadata(database, 'expected_tops'),
                  'source_file_count': metadata(database, 'summary')['source_file_count'],
                  'deletion_limits': limits, 'policy': policy()}
        from data.annotate import atomic_write_text
        atomic_write_text(Path(context['destination']) / MARKER, json.dumps(marker, indent=2))
        return marker


@contextmanager
def parent_descriptor(root_descriptor, path, *, create=False):
    relative = Path(path)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise MirrorError('Unsafe descriptor-relative path')
    descriptor = os.dup(root_descriptor)
    try:
        for component in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(component, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor, relative.name
    finally:
        os.close(descriptor)


@contextmanager
def sqlite_window(path):
    with Path(path).open('rb') as handle:
        header = handle.read(100)
        is_database = header[:16] == b'SQLite format 3\x00'
    if not is_database:
        if Path(path).suffix.lower() in ('.sqlite', '.sqlite3', '.db'):
            raise MirrorError('Recognized SQLite filename has a damaged/non-SQLite header: ' + str(path))
        yield False
        return
    if len(header) < 100:
        raise MirrorError('SQLite header is truncated: ' + str(path))
    if header[18] == 2 or header[19] == 2:
        raise MirrorError('SQLite WAL requires a quiescent coordinated snapshot; source not opened: ' + str(path))
    with readonly_database(path) as database:
        journal_mode = database.execute('PRAGMA journal_mode').fetchone()[0]
        if journal_mode not in ('delete', 'truncate', 'persist'):
            raise MirrorError('SQLite requires a quiescent rollback-journal window; WAL/off database rejected: ' + str(path))
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise MirrorError('SQLite sidecar must be quiesced before backup: ' + str(sidecar))
        database.execute('BEGIN')
        database.execute('SELECT rootpage FROM sqlite_master LIMIT 1').fetchone()
        try:
            yield True
        finally:
            database.rollback()


def sqlite_integrity(path):
    with readonly_database(path) as database:
        result = database.execute('PRAGMA integrity_check').fetchall()
        if result != [('ok',)]:
            raise MirrorError('SQLite integrity verification failed: ' + str(path))


def verify_copy(path, expected):
    if file_hash(path) != expected:
        raise MirrorError('Transferred contents failed SHA256 verification')


def atomic_copy(context, source_descriptor, destination_descriptor, row, destination_row):
    path = row['path']
    root = Path(context['destination'])
    temporary = root / CONTROL / ('part-' + hashlib.sha256(path.encode()).hexdigest())
    with parent_descriptor(source_descriptor, path) as (source_parent, source_name):
        source_file = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_parent)
    try:
        expected = (row['size'], row['mtime'], row['ctime'], row['inode'], row['device'], row['mode'])
        if identity(os.fstat(source_file)) != expected:
            raise MirrorError('Source changed before transfer: ' + path)
        with sqlite_window(Path(context['project']) / path) as is_database:
            temporary_file = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            digest = hashlib.sha256()
            with os.fdopen(temporary_file, 'wb') as output:
                while True:
                    chunk = os.read(source_file, 4 * 1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if identity(os.fstat(source_file)) != expected:
                raise MirrorError('Source changed during transfer: ' + path)
            verify_copy(temporary, digest.hexdigest())
            if is_database:
                sqlite_integrity(temporary)
            os.utime(temporary, ns=(row['mtime'], row['mtime']))
            check_context(context, fast=True)
            with parent_descriptor(destination_descriptor, path, create=True) as (destination_parent, destination_name):
                try:
                    existing = os.stat(destination_name, dir_fd=destination_parent, follow_symlinks=False)
                except FileNotFoundError:
                    existing = None
                if destination_row is None and existing is not None:
                    raise MirrorError('Unexpected destination appeared during transfer: ' + path)
                if destination_row is not None:
                    old = tuple(destination_row[key] for key in ('size', 'mtime', 'ctime', 'inode', 'device', 'mode'))
                    if existing is None or identity(existing) != old:
                        raise MirrorError('Destination changed during transfer: ' + path)
                os.replace(temporary, destination_name, dst_dir_fd=destination_parent)
                os.fsync(destination_parent)
            return digest.hexdigest()
    finally:
        os.close(source_file)


def verify_payload(context, database, journal=None, *, progress=None, cancelled=None):
    checked = 0
    sqlite_checked = 0
    total = database.execute("SELECT count(*) FROM inventory WHERE side='source' AND kind='file'").fetchone()[0]
    for row in database.execute("SELECT path,kind FROM inventory WHERE side='source' AND kind IN ('file','directory') ORDER BY path"):
        path, kind = row
        checkpoint(progress, cancelled, 'verify', files_verified=checked, total_files=total,
                   sqlite_databases_verified=sqlite_checked, path=path)
        source = Path(context['project']) / path
        destination = Path(context['destination']) / path
        if kind == 'directory':
            if not destination.is_dir() or destination.is_symlink():
                raise MirrorError('Missing or unsafe mirror directory: ' + path)
            continue
        with sqlite_window(source) as is_database:
            source_hash = file_hash(source)
            verify_copy(destination, source_hash)
            if is_database:
                sqlite_integrity(destination)
                sqlite_checked += 1
        if journal is not None:
            journal.execute('INSERT OR REPLACE INTO verified VALUES(?,?)', (path, source_hash))
            if checked % 256 == 0:
                journal.commit()
        checked += 1
    if journal is not None:
        journal.commit()
    checkpoint(progress, cancelled, 'payload_checked', files_verified=checked, total_files=total,
               sqlite_databases_verified=sqlite_checked)
    return {'files_verified': checked, 'total_files': total, 'sqlite_databases_verified': sqlite_checked,
            'sha256': True, 'sqlite_integrity': True}


def deletion_authorized(database, marker, approval, digest):
    summary = metadata(database, 'summary')
    count = summary['actions'].get('delete', {}).get('count', 0)
    byte_count = summary['estimated_delete_recovery_bytes']
    directory_count = summary['actions'].get('rmdir', {}).get('count', 0)
    destination_count = database.execute("SELECT count(*) FROM inventory WHERE side='destination' AND kind='file'").fetchone()[0]
    limits = marker.get('deletion_limits', DEFAULT_LIMITS)
    exceeded = count > limits['max_files'] or byte_count > limits['max_bytes'] or 100 * count / max(1, destination_count) > limits['max_percentage']
    population_decreased = summary['source_file_count'] < marker.get('source_file_count', 0)
    must_approve = (not marker.get('reconciled') and (count or directory_count)) or exceeded or population_decreased
    if must_approve and approval != digest:
        raise MirrorError('Deletion threshold/first reconciliation/source population requires exact plan approval')


class SourceGuard:
    """Fail closed on Linux source changes, unavailable watches or event overflow."""

    def __init__(self, database, project):
        library = ctypes.CDLL(None, use_errno=True)
        library.inotify_init1.argtypes = [ctypes.c_int]
        library.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.descriptor = library.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.descriptor < 0:
            raise MirrorError('Cannot initialize source change guard')
        self.paths = {}
        try:
            directories = ['']
            directories.extend(row[0] for row in database.execute("SELECT path FROM inventory WHERE side='source' AND kind='directory'"))
            for relative in directories:
                path = Path(project) / relative
                watch = library.inotify_add_watch(self.descriptor, os.fsencode(path), 0x01002FCE | 0x02000000)
                if watch < 0:
                    raise MirrorError('Cannot guard every source directory; stop instead of weakening change detection: ' + str(path))
                self.paths[watch] = relative
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        while True:
            try:
                events = os.read(self.descriptor, 262144)
            except BlockingIOError:
                return
            offset = 0
            while offset < len(events):
                watch, mask, cookie, length = struct.unpack_from('iIII', events, offset)
                name = events[offset + 16:offset + 16 + length].split(b'\x00', 1)[0]
                relative = '/'.join(part for part in (self.paths.get(watch, ''), os.fsdecode(name)) if part)
                offset += 16 + length
                if mask & (0x4000 | 0x2000 | 0x8000):
                    raise MirrorError('Source change guard overflow/unmount/watch loss; stale deletion plan refused')
                if not excluded(relative):
                    raise MirrorError('Source changed during synchronization: ' + relative)

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def apply_mirror(project, configured, plan, *, approval=None, expected_digest=None, progress=None, cancelled=None):
    checkpoint(progress, cancelled, 'preflight')
    plan, digest = approved_plan(plan, approval)
    if expected_digest is not None and digest != expected_digest:
        raise MirrorError('Reviewed plan changed; restart preview')
    with operation_lock(project), readonly_database(plan) as database:
        database.row_factory = sqlite3.Row
        context = reviewed_context(database, project, configured)
        check_context(context)
        marker = read_marker(context)
        if marker is None:
            raise MirrorError('Explicit mirror adoption required before apply')
        if marker.get('policy') != metadata(database, 'policy'):
            raise MirrorError('Mirror scope/policy changed without reviewed adoption')
        require_source(project, marker.get('expected_tops'))
        if metadata(database, 'summary')['issues'] or database.execute("SELECT 1 FROM actions WHERE action='conflict' LIMIT 1").fetchone():
            raise MirrorError('Unresolved special-file/filename/type conflicts block apply')
        deletion_authorized(database, marker, approval, digest)
        validate_inventory(database, 'source', project)
        summary = metadata(database, 'summary')
        largest_update = database.execute("SELECT coalesce(max(source_size),0) FROM actions WHERE action='update'").fetchone()[0]
        required_capacity = summary['actions'].get('copy', {}).get('source_bytes', 0) + largest_update + plan.stat().st_size
        if shutil.disk_usage(context['destination']).free < required_capacity:
            raise MirrorError('Insufficient copy-before-delete space; obsolete bytes cannot be spent before verification')
        control = Path(context['destination']) / CONTROL
        journal_path = control / ('journal-' + digest + '.sqlite3')
        if not journal_path.exists():
            validate_inventory(database, 'destination', context['destination'], context=context)
        checkpoint(progress, cancelled, 'copy')
        if control.is_symlink():
            raise MirrorError('Control namespace is a symlink')
        control.mkdir(exist_ok=True)
        journal_path = control / ('journal-' + digest + '.sqlite3')
        if journal_path.is_symlink():
            raise MirrorError('Journal must not be a symlink')
        journal = sqlite3.connect(journal_path)
        journal.row_factory = sqlite3.Row
        journal.execute('PRAGMA synchronous=FULL')
        journal.executescript('''CREATE TABLE IF NOT EXISTS entries(path TEXT PRIMARY KEY,action TEXT,state TEXT,sha256 TEXT,error TEXT);
          CREATE TABLE IF NOT EXISTS verified(path TEXT PRIMARY KEY,sha256 TEXT);
          CREATE TABLE IF NOT EXISTS operation(plan_digest TEXT PRIMARY KEY,phase TEXT);''')
        journal.execute('INSERT OR IGNORE INTO operation VALUES(?,?)', (digest, 'copy'))
        journal.executemany('INSERT OR IGNORE INTO entries VALUES(?,?,?,NULL,NULL)',
                            [(row['path'], row['action'], 'pending') for row in database.execute("SELECT path,action FROM actions WHERE action IN ('copy','update','mkdir','delete','rmdir')")])
        journal.commit()
        source_descriptor = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        destination_descriptor = os.open(context['destination'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        source_guard = None
        current_path = None
        try:
            source_guard = SourceGuard(database, project)
            validate_inventory(database, 'source', project)
            changing = [row['path'] for row in journal.execute("SELECT path FROM entries WHERE state!='pending'")]
            added_directories = [row['path'] for row in database.execute("SELECT path FROM actions WHERE action='mkdir'")]
            validate_inventory(database, 'destination', context['destination'], allow_paths=changing + added_directories, context=context)
            for entry in journal.execute("SELECT * FROM entries WHERE action IN ('copy','update','mkdir') ORDER BY path").fetchall():
                path = entry['path']
                checkpoint(progress, cancelled, 'copy', path=path)
                current_path = path
                source_guard.check()
                check_context(context, fast=True)
                if entry['action'] == 'mkdir':
                    with parent_descriptor(destination_descriptor, path + '/placeholder', create=True):
                        pass
                    journal.execute("UPDATE entries SET state='copied' WHERE path=?", (path,))
                    journal.commit()
                    continue
                if entry['state'] == 'copied':
                    verify_copy(Path(context['destination']) / path, entry['sha256'])
                    continue
                source_row = database.execute("SELECT * FROM inventory WHERE side='source' AND path=?", (path,)).fetchone()
                destination_row = database.execute("SELECT * FROM inventory WHERE side='destination' AND path=?", (path,)).fetchone()
                if entry['state'] == 'copying':
                    destination = Path(context['destination']) / path
                    if destination.is_file() and not destination.is_symlink() and file_hash(destination) == file_hash(Path(project) / path):
                        journal.execute("UPDATE entries SET state='copied',sha256=? WHERE path=?", (file_hash(destination), path))
                        journal.commit()
                        continue
                journal.execute("UPDATE entries SET state='copying',error=NULL WHERE path=?", (path,))
                journal.commit()
                checksum = atomic_copy(context, source_descriptor, destination_descriptor, source_row, destination_row)
                journal.execute("UPDATE entries SET state='copied',sha256=? WHERE path=?", (checksum, path))
                journal.commit()
            validate_inventory(database, 'source', project)
            verification = verify_payload(context, database, journal, progress=progress, cancelled=cancelled)
            validate_inventory(database, 'source', project)
            source_guard.check()
            check_context(context)
            for entry in journal.execute("SELECT * FROM entries WHERE action IN ('delete','rmdir') ORDER BY length(path) DESC,path DESC").fetchall():
                path = entry['path']
                checkpoint(progress, cancelled, 'delete', path=path)
                current_path = path
                source_guard.check()
                check_context(context, fast=True)
                if entry['state'] == 'deleted':
                    if os.path.lexists(Path(context['destination']) / path):
                        raise MirrorError('Previously deleted destination reappeared')
                    continue
                if os.path.lexists(Path(project) / path):
                    raise MirrorError('Deletion plan stale; source counterpart appeared: ' + path)
                old = database.execute("SELECT * FROM inventory WHERE side='destination' AND path=?", (path,)).fetchone()
                with parent_descriptor(destination_descriptor, path) as (parent, name):
                    try:
                        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        if entry['state'] != 'deleting':
                            raise MirrorError('Unexplained destination disappearance')
                        info = None
                    if info is not None and entry['action'] == 'delete':
                        expected = tuple(old[key] for key in ('size', 'mtime', 'ctime', 'inode', 'device', 'mode'))
                        if identity(info) != expected:
                            raise MirrorError('Deletion candidate changed since reviewed plan')
                    if info is not None and entry['action'] == 'rmdir' and (not stat.S_ISDIR(info.st_mode) or info.st_ino != old['inode']):
                        raise MirrorError('Directory replacement blocks cleanup')
                    journal.execute("UPDATE entries SET state='deleting' WHERE path=?", (path,))
                    journal.commit()
                    if info is not None:
                        source_guard.check()
                        if entry['action'] == 'delete':
                            os.unlink(name, dir_fd=parent)
                        else:
                            try:
                                os.rmdir(name, dir_fd=parent)
                            except OSError as error:
                                import errno
                                if error.errno != errno.ENOTEMPTY:
                                    raise
                                journal.execute("UPDATE entries SET state='skipped',error='Protected/nonempty directory retained' WHERE path=?", (path,))
                                journal.commit()
                                continue
                        os.fsync(parent)
                    journal.execute("UPDATE entries SET state='deleted' WHERE path=?", (path,))
                    journal.commit()
            validate_inventory(database, 'source', project)
            verification = verify_payload(context, database, journal, progress=progress, cancelled=cancelled)
            source_guard.check()
            with tempfile.TemporaryDirectory(dir=Path(project) / REPORT_RELATIVE) as temporary:
                checkpoint(progress, cancelled, 'final_verify')
                with sqlite3.connect(Path(temporary) / 'final.sqlite3') as final:
                    new_inventory(final)
                    inventory_tree(final, project, 'source')
                    inventory_tree(final, context['destination'], 'destination')
                    classify(final, exfat=context['mount']['fstype'] == 'exfat', first=False)
                    if inventory_fingerprint(final, 'source') != metadata(database, 'source_fingerprint'):
                        raise MirrorError('Source fingerprint changed during final verification')
                    if final.execute("SELECT 1 FROM actions WHERE action IN ('copy','conflict','delete','rmdir') LIMIT 1").fetchone():
                        raise MirrorError('Final mirror contains missing/unexpected comparable files')
            metadata_destination = control / ('metadata-' + digest + '.sqlite3')
            metadata_temporary = control / ('metadata-' + digest + '.part')
            if metadata_destination.is_symlink() or metadata_temporary.is_symlink():
                raise MirrorError('Metadata control path is a symlink')
            if not metadata_destination.exists() or file_hash(metadata_destination) != digest:
                shutil.copyfile(plan, metadata_temporary)
                verify_copy(metadata_temporary, digest)
                sqlite_integrity(metadata_temporary)
                os.replace(metadata_temporary, metadata_destination)
            verify_copy(metadata_destination, digest)
            sqlite_integrity(metadata_destination)
            marker.update({'reconciled': True, 'source_file_count': metadata(database, 'summary')['source_file_count'],
                           'last_verified_plan': digest, 'last_verified_at': time.time()})
            journal.execute("UPDATE operation SET phase='verified' WHERE plan_digest=?", (digest,))
            journal.commit()
            marker['journal_sha256'] = file_hash(journal_path)
            from data.annotate import atomic_write_text
            source_guard.check()
            checkpoint(progress, cancelled, 'finalize')
            atomic_write_text(Path(context['destination']) / MARKER, json.dumps(marker, indent=2))
            if progress is not None:
                progress({'phase': 'complete', **verification})
            return verification
        except BaseException as error:
            if current_path is not None:
                journal.execute('UPDATE entries SET error=? WHERE path=?', (type(error).__name__ + ': ' + str(error), current_path))
            journal.execute("UPDATE operation SET phase='interrupted' WHERE plan_digest=?", (digest,))
            journal.commit()
            raise
        finally:
            if source_guard is not None:
                source_guard.close()
            os.close(source_descriptor)
            os.close(destination_descriptor)
            journal.close()


def restoration_device_identity(context):
    device = context['mount']['source']
    partition = json.loads(subprocess.run(
        ['lsblk', '-J', '-b', '-o', 'SIZE,PARTUUID,UUID', device],
        check=True, capture_output=True, text=True).stdout)['blockdevices'][0]
    parent = subprocess.run(['lsblk', '-ndo', 'PKNAME', device],
                            check=True, capture_output=True, text=True).stdout.strip()
    if not parent or '/' in parent:
        raise MirrorError('Restoration disk parent is missing or unsafe')
    disk = json.loads(subprocess.run(
        ['lsblk', '-J', '-b', '-dn', '-o', 'SIZE,MODEL,SERIAL', '/dev/' + parent],
        check=True, capture_output=True, text=True).stdout)['blockdevices'][0]
    return {'disk_serial': disk['serial'], 'disk_model': disk['model'], 'disk_bytes': disk['size'],
            'partition_uuid': partition['partuuid'], 'partition_bytes': partition['size'],
            'filesystem_uuid': partition['uuid'], 'mountpoint': context['mount']['target'],
            'destination': context['destination']}


@contextmanager
def restoration_context(project, configured, plan, expected_device):
    """Read-only remount verification; mutation workflows never use this exception."""
    context = destination_context(project, configured)
    check_context(context)
    required = {'disk_serial', 'disk_model', 'disk_bytes', 'partition_uuid', 'partition_bytes',
                'filesystem_uuid', 'mountpoint', 'destination'}
    if (set(expected_device) != required or any(not value for value in expected_device.values())
            or restoration_device_identity(context) != expected_device
            or expected_device['filesystem_uuid'] != context['mount']['uuid']):
        raise MirrorError('Pinned restoration disk/partition/mount/target identity changed')
    descriptor = os.open(context['destination'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        anchored = os.fstat(descriptor)
        if anchored.st_ino != context['destination_inode'] or anchored.st_dev != context['destination_device']:
            raise MirrorError('Restoration target replaced before descriptor anchoring')
        marker = read_marker(context)
        if marker is None or not marker.get('reconciled') or marker.get('policy') != policy():
            raise MirrorError('Completed identified mirror required for remount verification')
        digest = marker.get('last_verified_plan', '')
        seal = marker.get('journal_sha256', '')
        if any(len(value) != 64 or any(character not in '0123456789abcdef' for character in value) for value in (digest, seal)):
            raise MirrorError('Malformed restoration evidence seals')
        control = Path(context['destination']) / CONTROL
        if control.is_symlink():
            raise MirrorError('Restoration control namespace is a symlink')
        stored = control / ('metadata-' + digest + '.sqlite3')
        journal = control / ('journal-' + digest + '.sqlite3')
        for path, checksum in ((Path(plan), digest), (stored, digest), (journal, seal)):
            verify_copy(path, checksum)
            sqlite_integrity(path)
        with readonly_database(stored) as database, readonly_database(journal) as ledger:
            recorded = metadata(database, 'context')
            expected_context = dict(recorded)
            if recorded['mount']['fstype'] == context['mount']['fstype'] == 'exfat':
                expected_context['destination_inode'] = context['destination_inode']
            if expected_context != context:
                raise MirrorError('Restoration reviewed identity changed beyond permitted exFAT inode')
            if (marker['source_project'] != recorded['project']
                    or marker['source_file_count'] != metadata(database, 'summary')['source_file_count']
                    or ledger.execute('SELECT phase FROM operation WHERE plan_digest=?', (digest,)).fetchone() != ('verified',)):
                raise MirrorError('Sealed restoration identity/completion mismatch')
        check_context(context)
        evidence = {'read_only': True, 'pinned_device': expected_device,
                    'historical_destination_inode': recorded['destination_inode'],
                    'current_destination_inode': context['destination_inode'],
                    'exfat_remount_inode_exception': recorded['destination_inode'] != context['destination_inode'],
                    'plan_sha256': digest, 'journal_sha256': seal, 'sealed_evidence_integrity': 'ok'}
        yield context, evidence
        check_context(context)
        if restoration_device_identity(context) != expected_device or read_marker(context) != marker:
            raise MirrorError('Restoration hardware/marker changed during verification')
        verify_copy(Path(plan), digest)
        verify_copy(stored, digest)
        verify_copy(journal, seal)
        if os.fstat(descriptor).st_ino != anchored.st_ino or os.fstat(descriptor).st_dev != anchored.st_dev:
            raise MirrorError('Anchored restoration directory changed')
    finally:
        os.close(descriptor)


def _completed_mirror_evidence(context, marker):
    if not marker or marker.get('reconciled') is not True:
        raise MirrorError('No completed mirror evidence')
    digest = marker.get('last_verified_plan', '')
    seal = marker.get('journal_sha256', '')
    if any(not isinstance(value, str) or len(value) != 64 or any(character not in '0123456789abcdef' for character in value)
           for value in (digest, seal)):
        raise MirrorError('Invalid completed mirror seals')
    control = Path(context['destination']) / CONTROL
    plan = control / ('metadata-' + digest + '.sqlite3')
    journal = control / ('journal-' + digest + '.sqlite3')
    if control.is_symlink() or plan.is_symlink() or journal.is_symlink():
        raise MirrorError('Mirror evidence must not use symlinks')
    approved_plan(plan, digest)
    verify_copy(journal, seal)
    with readonly_database(plan) as database, readonly_database(journal) as ledger:
        recorded = metadata(database, 'context')
        if any(recorded.get(key) != context.get(key) for key in ('project', 'destination')) or recorded['mount']['uuid'] != context['mount']['uuid']:
            raise MirrorError('Completed mirror evidence belongs to a different destination')
        total = metadata(database, 'summary')['source_file_count']
        if (ledger.execute('SELECT phase FROM operation WHERE plan_digest=?', (digest,)).fetchone() != ('verified',)
                or ledger.execute('SELECT count(*) FROM verified').fetchone()[0] != total
                or marker.get('source_file_count') != total):
            raise MirrorError('Mirror journal does not prove completed synchronization')
    return plan


def _valid_backup_timestamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value < 253402300800


def backup_status(project, configured):
    result = {'last_mirror': None, 'last_full_verify': None, 'verification_current': False}
    try:
        context = destination_context(project, configured)
        marker = read_marker(context)
        plan = _completed_mirror_evidence(context, marker)
        with readonly_database(plan) as database:
            recorded = metadata(database, 'context')
            if any(recorded.get(key) != context.get(key) for key in ('destination_inode', 'destination_device')):
                raise MirrorError('Selected mirror root no longer matches completed evidence')
        mirrored = marker.get('last_verified_at')
        if _valid_backup_timestamp(mirrored):
            result['last_mirror'] = mirrored
        verification = marker.get('last_full_verification')
        if (isinstance(verification, dict) and _valid_backup_timestamp(verification.get('completed_at'))
                and _valid_backup_timestamp(verification.get('mirror_completed_at'))
            and verification.get('project') == context['project']
            and verification.get('destination') == context['destination']
            and verification.get('device_uuid') == context['mount']['uuid']
                and all(isinstance(verification.get(key), str) and len(verification[key]) == 64
                        and all(character in '0123456789abcdef' for character in verification[key])
                        for key in ('plan_digest', 'journal_sha256'))):
            result['last_full_verify'] = verification['completed_at']
            result['verification_current'] = (verification['plan_digest'] == marker['last_verified_plan']
                                              and verification['journal_sha256'] == marker['journal_sha256']
                                              and verification['mirror_completed_at'] == mirrored)
    except (MirrorError, OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as error:
        result['warning'] = str(error)
    return result


def restore_verification(project, configured, plan=None, *, full=False, remount_identity=None, progress=None, cancelled=None):
    checkpoint(progress, cancelled, 'preparing')
    if full:
        with operation_lock(project):
            return _run_full_verification(project, configured, plan, remount_identity, progress, cancelled)
    if remount_identity is not None:
        raise MirrorError('Remount validation requires full read-only restoration and a sealed plan')
    return _restore_verification(destination_context(project, configured), plan, full=False)


def _run_full_verification(project, configured, plan, remount_identity, progress, cancelled):
    if remount_identity is None:
        context = destination_context(project, configured)
        if plan is None:
            marker = read_marker(context)
            digest = marker.get('last_verified_plan', '') if marker else ''
            if not marker or not marker.get('reconciled') or len(digest) != 64 or any(character not in '0123456789abcdef' for character in digest):
                raise MirrorError('Full verification requires a completed mirror and retained verified plan')
            control = Path(context['destination']) / CONTROL
            plan = control / ('metadata-' + digest + '.sqlite3')
            if control.is_symlink() or plan.is_symlink():
                raise MirrorError('Retained verification plan must not use symlinks')
            approved_plan(plan, digest)
        result = _restore_verification(context, plan, full=True, progress=progress, cancelled=cancelled)
    else:
        if plan is None:
            raise MirrorError('Remount validation requires full read-only restoration and a sealed plan')
        with restoration_context(project, configured, plan, remount_identity) as (context, evidence):
            result = _restore_verification(context, plan, full=True, restoration_identity=evidence,
                                          progress=progress, cancelled=cancelled)
    if progress is not None:
        progress({'phase': 'complete', **result['verification']})
    return result


def _restore_verification(context, plan, *, full, restoration_identity=None, progress=None, cancelled=None):
    project = Path(context['project'])
    marker = read_marker(context)
    destination = Path(context['destination'])
    census_relative = 'outputs/tests/silhouette-identity-migration/census.sqlite3'
    census = destination / census_relative
    census_status = {'path': str(census), 'present': census.is_file(), 'verified': False}
    if census.is_file() and not census.is_symlink():
        sqlite_integrity(census)
        census_status['integrity'] = 'ok'
        source = Path(project) / census_relative
        if source.is_file():
            with sqlite_window(source):
                census_status['verified'] = file_hash(source) == file_hash(census)
    result = {'context': context, 'mirror_identity_verified': marker is not None,
              'archive_present': (destination / 'archive').is_dir(),
              'critical_configuration_present': (destination / 'preferences/preferences.json').is_file(),
              'census': census_status, 'full_payload_verified': False,
              'complete_linux_restore': False,
              'metadata_note': 'exFAT requires verified metadata plan + checksum journal and a Linux restore target; ownership/permissions/hardlinks/symlinks are not represented by ordinary data copies'}
    if full:
        if marker is None or plan is None:
            raise MirrorError('Full restore verification requires an adopted mirror and retained plan')
        with readonly_database(plan) as database, ExitStack() as guards:
            if restoration_identity is None:
                reviewed_context(database, project, context['configured'])
            source_guard = SourceGuard(database, project)
            guards.callback(source_guard.close)
            destination_guard = SourceGuard(database, destination)
            guards.callback(destination_guard.close)
            checkpoint(progress, cancelled, 'source_validation')
            validate_inventory(database, 'source', project)
            result['verification'] = verify_payload(context, database, progress=progress, cancelled=cancelled)
            checkpoint(progress, cancelled, 'population')
            with tempfile.TemporaryDirectory(dir=Path(project) / REPORT_RELATIVE) as temporary:
                with sqlite3.connect(Path(temporary) / 'restore-check.sqlite3') as final:
                    new_inventory(final)
                    def population_progress(event):
                        if progress is not None:
                            progress({**event, 'phase': 'population'})
                    inventory_tree(final, project, 'source', progress=population_progress, cancelled=cancelled)
                    inventory_tree(final, context['destination'], 'destination', progress=population_progress, cancelled=cancelled)
                    classify(final, exfat=context['mount']['fstype'] == 'exfat', first=False)
                    if final.execute("SELECT 1 FROM actions WHERE action IN ('copy','conflict','delete','rmdir') LIMIT 1").fetchone():
                        raise MirrorError('Restore population contains missing/unexpected comparable entries')
            validate_inventory(database, 'source', project)
            source_guard.check()
            destination_guard.check()
            checkpoint(progress, cancelled, 'final_validation')
            if restoration_identity is None:
                check_context(context)
                if read_marker(context) != marker:
                    raise MirrorError('Mirror evidence changed during full verification')
                if marker.get('reconciled') and marker.get('last_verified_plan') == file_hash(plan):
                    _completed_mirror_evidence(context, marker)
                    checkpoint(None, cancelled, 'final_validation')
                    source_guard.check()
                    destination_guard.check()
                    completed = time.time()
                    marker['last_full_verification'] = {
                        'completed_at': completed, 'plan_digest': marker['last_verified_plan'],
                        'journal_sha256': marker['journal_sha256'], 'mirror_completed_at': marker.get('last_verified_at'),
                        'project': context['project'], 'destination': context['destination'],
                        'device_uuid': context['mount']['uuid'],
                    }
                    from data.annotate import atomic_write_text
                    atomic_write_text(destination / MARKER, json.dumps(marker, indent=2))
                    result['completed_at'] = completed
            result['full_payload_verified'] = True
            if restoration_identity is not None:
                result['restoration_identity'] = restoration_identity
    return result


def verify_stored_mirror(destination, *, progress=None):
    destination = Path(destination).resolve(strict=True)
    marker_path = destination / MARKER
    if marker_path.is_symlink():
        raise MirrorError('Mirror marker must not be a symlink')
    marker = json.loads(marker_path.read_text())
    if (marker.get('schema_version') != 1 or marker.get('purpose') != 'crossing-project-mirror'
            or marker.get('mirror_policy') != 'current-project-state' or not marker.get('reconciled')):
        raise MirrorError('Offline verification requires a completed, identified mirror')
    mounts = [item for item in mounted_filesystems() if item.get('uuid') == marker.get('device_uuid') and item.get('label') == 'PLAYABLE']
    if len(mounts) != 1 or destination != Path(mounts[0]['target']).resolve() / 'backups' / marker['project']:
        raise MirrorError('Offline mirror is not on its pinned mounted device/destination')
    digest = marker.get('last_verified_plan', '')
    if len(digest) != 64 or any(character not in '0123456789abcdef' for character in digest):
        raise MirrorError('Verified plan digest missing or malformed')
    control = destination / CONTROL
    if control.is_symlink():
        raise MirrorError('Mirror control namespace is a symlink')
    plan = control / ('metadata-' + digest + '.sqlite3')
    journal = control / ('journal-' + digest + '.sqlite3')
    verify_copy(plan, digest)
    if not marker.get('journal_sha256'):
        raise MirrorError('Checksum journal has no completed verification seal')
    verify_copy(journal, marker['journal_sha256'])
    sqlite_integrity(plan)
    sqlite_integrity(journal)
    with readonly_database(plan) as database, readonly_database(journal) as ledger:
        context = metadata(database, 'context')
        if context['project'] != marker['source_project'] or Path(context['project']).name != marker['project']:
            raise MirrorError('Stored restore identity mismatch')
        if ledger.execute('SELECT phase FROM operation WHERE plan_digest=?', (digest,)).fetchone() != ('verified',):
            raise MirrorError('Stored synchronization was not completed')
        total = metadata(database, 'summary')['source_file_count']
        if total != marker['source_file_count'] or ledger.execute('SELECT count(*) FROM verified').fetchone()[0] != total:
            raise MirrorError('Sealed mirror file-count evidence differs')
        guard = SourceGuard(database, destination)
        try:
            expected = set()
            protected = {row[0] for row in database.execute("SELECT path FROM actions WHERE action='protect'")}
            count = 0
            sqlite_count = 0
            census = False
            if progress:
                progress({'phase': 'payload', 'files_verified': 0, 'total_files': total, 'destination_watches': len(guard.paths)})
            for path, kind in database.execute("SELECT path,kind FROM inventory WHERE side='source' AND kind IN ('file','directory')"):
                guard.check()
                expected.add(path)
                target = destination / path
                if kind == 'directory':
                    if not target.is_dir() or target.is_symlink():
                        raise MirrorError('Stored mirror directory absent/unsafe: ' + path)
                    continue
                recorded = ledger.execute('SELECT sha256 FROM verified WHERE path=?', (path,)).fetchone()
                if recorded is None:
                    raise MirrorError('Payload has no verified checksum: ' + path)
                verify_copy(target, recorded[0])
                with sqlite_window(target) as is_database:
                    if is_database:
                        sqlite_integrity(target)
                        sqlite_count += 1
                if path == 'outputs/tests/silhouette-identity-migration/census.sqlite3':
                    census = True
                count += 1
                if progress and (count % 10000 == 0 or count == total):
                    progress({'phase': 'payload', 'files_verified': count, 'total_files': total,
                              'sqlite_databases_verified': sqlite_count})
            if count != total:
                raise MirrorError('Historical payload count differs from sealed evidence')
            if progress:
                progress({'phase': 'population', 'files_verified': count, 'total_files': total})
            pending = [destination]
            while pending:
                guard.check()
                directory = pending.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        relative = Path(entry.path).relative_to(destination).as_posix()
                        if excluded(relative) or relative in protected:
                            continue
                        if relative not in expected:
                            raise MirrorError('Unexpected stored mirror payload: ' + relative)
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
            verify_copy(plan, digest)
            verify_copy(journal, marker['journal_sha256'])
            if json.loads(marker_path.read_text()) != marker:
                raise MirrorError('Historical mirror marker changed during verification')
            guard.check()
        finally:
            guard.close()
    return {'verified': True, 'files_verified': count, 'primary_project_required': False,
            'sqlite_databases_verified': sqlite_count, 'directory_structure_and_population_verified': True,
            'sha256': True, 'destination_change_monitoring': True, 'plan_sha256': digest,
            'checksum_journal_sha256': marker['journal_sha256'],
            'census_independently_verified': census, 'complete_linux_restore': False,
            'metadata': 'Verified source modes/uid/gid/mtime/link metadata retained; ACLs/xattrs require separate preservation'}


def publish_preview(project, configured, *, checksums=False):
    with operation_lock(project):
        result = plan_mirror(project, configured, checksums=checksums)
        report = Path(project) / REPORT_RELATIVE
        canonical = report / 'reconciliation-plan.sqlite3'
        if not canonical.exists():
            os.rename(result['plan'], canonical)
            result['plan'] = str(canonical)
        os.chmod(result['plan'], 0o444)
        context = result['context']
        restoration = restore_verification(project, configured)
        result['census_backup_verified'] = restoration['census']['verified']
        with readonly_database(result['plan']) as database:
            scope = database.execute('''SELECT side,CASE WHEN instr(path,'/')=0 THEN path ELSE substr(path,1,instr(path,'/')-1) END top,
              count(*),sum(CASE WHEN kind='file' THEN size ELSE 0 END),sum(allocated)
              FROM inventory GROUP BY side,top ORDER BY side,top''').fetchall()
            largest = database.execute("SELECT path,destination_size,classification FROM actions WHERE action='delete' ORDER BY destination_size DESC LIMIT 30").fetchall()
            issue_samples = database.execute('SELECT * FROM issues LIMIT 30').fetchall()
            archived_counterparts = database.execute("SELECT count(*) FROM actions WHERE reason LIKE '%exact source archive counterpart%'").fetchone()[0]
            result['source_fingerprint'] = metadata(database, 'source_fingerprint')
            result['destination_fingerprint'] = metadata(database, 'destination_fingerprint')
        result['source_archive_counterpart_hints'] = archived_counterparts
        result['scope_inventory'] = [{'side': side, 'top': top, 'entries': count, 'apparent_file_bytes': size, 'allocated_bytes': allocated}
                                     for side, top, count, size, allocated in scope]
        result['issue_samples'] = issue_samples
        from data.annotate import atomic_write_text
        atomic_write_text(report / 'mirror-policy.json', json.dumps(policy(), indent=2))
        atomic_write_text(report / 'reconciliation-summary.json', json.dumps(result, indent=2))
        mount = context['mount']
        actions = result['actions']
        rows = '\n'.join(f'| {action} | {values["count"]:,} | {values["source_bytes"]:,} | {values["destination_bytes"]:,} |'
                         for action, values in sorted(actions.items()))
        scope_rows = '\n'.join(f'| {side} | {top} | {count:,} | {size:,} | {allocated:,} |' for side, top, count, size, allocated in scope)
        preview = f'''# First Reconciliation: Preview Only

No existing backup files were deleted, copied, overwritten or adopted.
No production payload was changed. Reports live only in the source maintenance control namespace.

Plan: `{result['plan']}`
Exact approval digest: `{result['plan_digest']}`
Marker present: {result['marker_present']}. Census independently verified: {result['census_backup_verified']}.

| Action | Entries | Source bytes | Destination bytes |
| --- | ---: | ---: | ---: |
{rows}

Transfer estimate: {result['bytes_to_transfer']:,} bytes. Potential deletion recovery:
{result['estimated_delete_recovery_bytes']:,} apparent bytes. Current payload:
{result['destination_payload_bytes']:,} bytes. Estimated final payload:
{result['estimated_final_payload_bytes']:,} bytes. Available: {result['available_bytes']:,} bytes.
Excluded subtrees are protected and not included in payload-size estimates.

All first-reconciliation backup-only files require human review; none is automatically
disposable. UNKNOWN entries match only metadata, not verified contents. Updates are
conservative size/time candidates, not confirmed content differences unless checksummed.
Archive counterpart hints: {archived_counterparts:,}; existence is not content equivalence.
Population/category/action queries cover every inventoried path in SQLite.
No giant JSONL export or full-corpus duplicate project was created.

## Largest Backup-Only Files (Not Authorized for Deletion)

''' + '\n'.join(f'- `{path}`: {size:,} bytes; {classification}' for path, size, classification in largest)
        atomic_write_text(report / 'deletion-preview.md', preview)
        audit = f'''# Existing Backup Audit

Legacy `crossing backup update` calls `rsync -a --no-links --info=progress2`, without
`--delete`. Its fallback only walks source files and uses size/destination-not-older
mtime to skip. It never removes missing files/directories, never checksums or verifies
SQLite integrity, and logs individual copy errors without aggregate failure in the
fallback. `--mirror` was reserved and ignored. rsync interruption can leave transfer
temporaries; the fallback writes ordinary destination filenames directly. Neither has
a durable synchronization manifest, mirror identity, coordinated SQLite snapshot or
post-copy restore verification. Existing tests cover UI free-space/progress, not backup
correctness. Legacy update remains additive; its CLI now shares the backup lock.

Configured path: `{context['configured']}`. Actual mounted destination:
`{context['destination']}`. Historical configuration relocation: {context['configuration_relocated']}.
Device: {mount['source']}, major/minor {mount.get('maj:min')}, UUID {mount['uuid']},
label {mount['label']}, filesystem {mount['fstype']}, options {mount['options']}.
Capacity: {result['capacity_bytes']:,} bytes; available: {result['available_bytes']:,} bytes.
No mount directory was created, no preference was changed, and no disk fallback was used.
Directory layout/name provide inferred project association, not a verified prior identity;
missing marker requires explicit reviewed adoption before apply.

## Actual Scope Inventory

| Side | Top-Level Entry | Entries | Apparent File Bytes | Allocated Bytes |
| --- | --- | ---: | ---: | ---: |
{scope_rows}

All project directories and ordinary dotfiles are inherited scope, including data,
archive, media, models, preferences, outputs and prompts when present. No large cache
exclusion is invented. `.venv.old` and cache directories remain ordinary scope.
Symlinks are inherited no-links exceptions: target dependencies may prevent complete
restoration. Special files block apply. The live maintenance report namespace and
mirror-only marker/control names are documented control exceptions approved by adoption;
existing destination contents there are protected, never deleted due to exclusion.
Documentation/code outside this project root are not in scope and require a separate
repository/package backup for executable-environment restoration.
''' 
        atomic_write_text(report / 'existing-backup-audit.md', audit)
        architecture = '''# Verified Mirror Architecture

One service (`services.backup_mirror`) owns inventory, comparison, identity, adoption,
copy verification, journal, deletion and restore verification. CLI is thin orchestration.
Legacy additive update is retained; mirror defaults to preview. Adoption is explicit and
pins source path, project name, mounted UUID, scope and initial reviewed plan.

Only human-approved exact plan digests enable first reconciliation. Defaults allow zero
unattended deletions because the legacy backup has no verified retention baseline; limits
are explicitly configurable during adoption. Copy additions/modifications to staging,
fsync, compare independent SHA256, verify SQLite, atomically publish, then verify the
entire current payload and rescan source population before obsolete-file deletion.
Revalidate each deletion's audited inode/device/size/time/type and current source absence.
Use descriptor-relative no-follow unlink/rmdir; remove only empty obsolete directories.
Protected exclusions remain protected even when destination-only. Source roots, expected
top directories, population changes, device remounts and concurrent operations block apply.

FULL-synchronous SQLite journal records intent before publication/deletion and completion
after fsync. Resume uses the same immutable plan, fresh identities/checksums and recorded
states, never assumes a .part file completed. Disconnection/copy/integrity/space failure
stops before deletion; interruption after publication is reconciled by fresh SHA256.
Source must be quiescent; metadata-equal preview entries remain UNKNOWN until hashed.
Hidden same-size/time differences abort apply before deletion; replan with --checksum.
Final verification checks content plus structure, not merely process exit status.
'''
        atomic_write_text(report / 'architecture.md', architecture)
        restore = '''# Restore Verification and Procedure

Do not restore over production. On an isolated Linux filesystem, verify mounted UUID,
marker source/project/policy, last verified plan digest, the retained metadata plan and
checksum journal under .crossing-mirror-control. Verify every regular payload's SHA256,
directory population and all SQLite databases with PRAGMA integrity_check. Verify source
archive availability, annotations/indexes, preferences and stored canonical media paths.

Restore ordinary payload to a fresh directory. Use the retained source inventory to
recreate directory/file modes, uid/gid (privileged restoration may be required), exact
mtime and hardlink groups (source device/inode with nlink > 1). Reconstruct recorded
symlinks only after validating their targets; project-absolute targets require an explicit
rebase decision, outside-project targets require separate dependencies. Apply directory
metadata last. exFAT synthesized owners/modes/executable bits are not originals.
exFAT can round timestamps, is case-insensitive, and cannot represent Linux links or
special files. Collision/filename/special-file issues block apply. ACLs/xattrs are not
captured by this initial mode/uid/gid manifest: preserve separately or use a reviewed
Linux archive/container/filesystem before claiming a complete Linux filesystem backup.
No partitions or filesystem formats are changed by this implementation.

Install Crossing from the separately retained code repository/environment; external code
and documentation were never included in the project-root legacy backup. Restore
preferences into the isolated project, then validate and explicitly rebase absolute
project/media/source paths rather than silently editing production. Run canonical
read-only media/index/archive lookups and a separately scheduled full restore rehearsal.
Metadata manifests alone are not proof of recoverability. This preview is not a verified
mirror and not a production restore rehearsal.

SQLite copying is a coordinated read-only/quiescent mechanism: hold a SHARED read
transaction in rollback-journal mode while reading bytes. Writers cannot commit over
that snapshot. Reject WAL/off mode or nonempty journal/WAL/SHM sidecars; do not omit or
checkpoint them silently. Arrange a separate application shutdown/checkpoint window
before applying those files. Destination DB integrity and raw-file hashes are verified.
'''
        atomic_write_text(report / 'restore-verification.md', restore + '\nObserved non-destructive verification:\n\n```json\n' + json.dumps(restoration, indent=2) + '\n```\n')
        atomic_write_text(report / 'implementation-report.md', architecture + '\n## Execution Boundary\n\nOnly preview executed. No adoption/apply/cleanup/migration/full copy or restore ran.\n')
        if not (report / 'tests-report.md').exists():
            atomic_write_text(report / 'tests-report.md', '# Tests\n\nSee tests/test_backup_mirror.py. Test runs are recorded separately; preview does not run or regenerate application artifacts.\n')
        return result