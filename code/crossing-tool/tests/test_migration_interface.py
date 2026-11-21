import json
from contextlib import closing
import os
import sqlite3
import sys
import threading
from unittest.mock import patch

import pytest

import cli
from data.resource_storage import digest
from services import migration_interface as interface
from services import maintenance_preflight
from test_resource_integration import _fixture
from tool import project_access as access


@pytest.fixture
def sandbox_prefs(monkeypatch):
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: default)
    monkeypatch.setattr(cli.prefs, "set", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def isolate_entrypoint(sandbox_prefs, monkeypatch):
    monkeypatch.delenv("CROSSING_PROJECT", raising=False)


@pytest.fixture
def prepared():
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        yield project, plan, checksum


def _args(prepared, action, *, confirm=True):
    project, plan, checksum = prepared
    arguments = ["migration", action, "--project", str(project), "--plan", str(plan),
                 "--plan-sha256", checksum, "--operation-id", "cli-fixture", "--json"]
    if action not in ("status", "prepare") and confirm:
        arguments.extend(["--confirm", "--clients-stopped"])
    return cli.build_parser().parse_args(arguments)


def _run(prepared, action, capsys):
    args = _args(prepared, action)
    assert args.func is cli.cmd_migration
    arguments = ["crossing", "migration", action, "--project", args.project, "--plan", args.plan,
                 "--plan-sha256", args.plan_sha256, "--operation-id", args.operation_id, "--json"]
    if action not in ("status", "prepare"):
        arguments.extend(["--confirm", "--clients-stopped"])
    with patch.object(sys, "argv", arguments):
        cli.main()
    output = capsys.readouterr().out
    return json.loads(output)


def test_status_and_prepare_are_read_only(prepared, capsys):
    project, plan, checksum = prepared
    before = {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}
    assert _run(prepared, "status", capsys)["maintenance"] == "legacy"
    assert _run(prepared, "status", capsys)["progress"]["moves"]["remaining"] == 44
    assert _run(prepared, "prepare", capsys)["status"] == "Ready for operator review"
    assert _run(prepared, "prepare", capsys)["plan_sha256"] == checksum
    assert all(path.read_bytes() == value for path, value in before.items())
    assert not (project / access.CONTROL).exists()


@pytest.mark.parametrize("action", ["start", "resume", "rollback", "validate", "accept", "release"])
def test_dispatch_requires_confirmation(prepared, action):
    with pytest.raises(SystemExit, match="--confirm"):
        _args(prepared, action, confirm=False).func(_args(prepared, action, confirm=False))
    assert not (prepared[0] / access.CONTROL).exists()


def test_actual_dispatch_install_validate_accept_and_reopen(prepared, capsys):
    assert _run(prepared, "start", capsys)["graph_accepted"]
    snapshot = _run(prepared, "status", capsys)
    assert snapshot["maintenance"] == "maintenance"
    assert snapshot["progress"]["moves"]["remaining"] == 0
    assert snapshot["progress"]["transforms"]["remaining"] == 0
    assert "latest_durable_progress" in snapshot
    assert _run(prepared, "validate", capsys)["graph_accepted"]
    assert _run(prepared, "accept", capsys)["state"] == "accepted"
    assert _run(prepared, "release", capsys)["maintenance_released"]
    assert _run(prepared, "release", capsys)["maintenance_released"]
    assert _run(prepared, "status", capsys)["maintenance"] == "open"
    with access.operation(prepared[0]):
        pass


@pytest.mark.parametrize("boundary", ["maintenance-requested", "before-rename", "metadata-replaced",
    "registry-pair-committed", "index-staged", "graph-validation"])
def test_dispatch_interruption_status_and_resume(prepared, capsys, monkeypatch, boundary):
    original = interface.dispatch
    def stop(event):
        if event == boundary:
            raise RuntimeError("CLI interrupted at " + event)
    monkeypatch.setattr(interface, "dispatch", lambda args, **options: original(args, interrupt=stop, **options))
    with pytest.raises(SystemExit, match="interrupted"):
        _run(prepared, "start", capsys)
    capsys.readouterr()
    monkeypatch.setattr(interface, "dispatch", original)
    snapshot = _run(prepared, "status", capsys)
    assert snapshot["maintenance"] != "open"
    assert "Resume" in snapshot["recovery_actions"]
    assert _run(prepared, "resume", capsys)["outcome"] == "resume"


def test_dispatch_safe_rollback(prepared, capsys):
    project, _, _ = prepared
    before = {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}
    _run(prepared, "start", capsys)
    assert _run(prepared, "rollback", capsys)["outcome"] == "rollback"
    assert _run(prepared, "rollback", capsys)["state"] == "rolled-back"
    assert all(path.read_bytes() == value for path, value in before.items())
    assert _run(prepared, "status", capsys)["maintenance"] == "maintenance"


def test_accept_and_release_refused_before_validation(prepared, capsys):
    access.enroll(prepared[0], approved=True)
    for action in ("accept", "release"):
        with pytest.raises(SystemExit):
            _run(prepared, action, capsys)
        capsys.readouterr()


def test_changed_destination_requires_review_not_overwrite(prepared, capsys):
    project, plan, _ = prepared
    _run(prepared, "start", capsys)
    with sqlite3.connect(plan) as database:
        destination = database.execute("SELECT destination FROM files WHERE disposition='Move' LIMIT 1").fetchone()[0]
    target = project / destination
    target.write_bytes(b"unexpected independent bytes")
    with pytest.raises(SystemExit):
        _run(prepared, "validate", capsys)
    capsys.readouterr()
    with pytest.raises(SystemExit, match="manual review"):
        _run(prepared, "rollback", capsys)
    assert target.read_bytes() == b"unexpected independent bytes"


@pytest.mark.parametrize("defect", ["project", "plan", "journal"])
def test_wrong_binding_or_corrupt_evidence(prepared, capsys, defect, tmp_path):
    args = _args(prepared, "start")
    if defect == "project":
        args.project = str(tmp_path)
    elif defect == "plan":
        args.plan_sha256 = "0" * 64
    else:
        _run(prepared, "start", capsys)
        (prepared[0] / access.CONTROL / "rename-cli-fixture.sqlite3").write_bytes(b"corrupted")
        args = _args(prepared, "resume")
    if defect == "journal":
        with pytest.raises(SystemExit, match="manual review"):
            _run(prepared, "resume", capsys)
    else:
        with pytest.raises(SystemExit):
            args.func(args)


@pytest.mark.parametrize("field", ["project", "plan_sha256", "identity_allocation_sha256", "device", "inode", "recovery_source"])
def test_authorization_is_not_transferable(prepared, field):
    binding = interface.prepare(*prepared)["authorization_binding"]
    changed = {**binding, field: "different"}
    with pytest.raises(access.MaintenanceError, match="changed"):
        interface.check_authorization(changed, binding, confirmed=True)


def test_production_execution_has_no_enable_flag(tmp_path):
    with pytest.raises(access.IncompleteCoverage, match="disabled"):
        interface.check_authorization({"project": str(tmp_path), "fixture_only": False},
                                      {"project": str(tmp_path), "fixture_only": False}, confirmed=True)
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["migration", "start", "--project", str(tmp_path), "--unsafe"])


def test_matching_production_arguments_are_disabled_before_maintenance(prepared, capsys, monkeypatch):
    project = prepared[0]
    monkeypatch.setattr(interface, "PROJECT", project)
    monkeypatch.setattr(interface, "_fixture", lambda path: False)
    production = project, project / interface.PLAN, interface.PLAN_DIGEST
    with pytest.raises(SystemExit, match="disabled"):
        _run(production, "start", capsys)
    assert not (project / access.CONTROL).exists()


def test_status_ignores_default_project_admission(prepared, capsys, monkeypatch):
    _run(prepared, "start", capsys)
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: str(prepared[0]))
    monkeypatch.setenv("CROSSING_PROJECT", str(prepared[0]))
    assert _run(prepared, "status", capsys)["maintenance"] == "maintenance"


def test_status_corrupt_sqlite_is_a_clean_read_only_refusal(prepared, capsys):
    _run(prepared, "start", capsys)
    journal = prepared[0] / access.CONTROL / "rename-cli-fixture.sqlite3"
    journal.write_bytes(b"corrupted sqlite evidence")
    with pytest.raises(SystemExit, match="Migration stopped"):
        _run(prepared, "status", capsys)
    assert journal.read_bytes() == b"corrupted sqlite evidence"


@pytest.mark.parametrize("mode", ["wal", "sidecar"])
def test_status_rejects_sqlite_sidecar_dependencies_without_writes(prepared, capsys, monkeypatch, mode):
    _run(prepared, "start", capsys)
    journal = prepared[0] / access.CONTROL / "rename-cli-fixture.sqlite3"
    if mode == "wal":
        with closing(sqlite3.connect(journal)) as database:
            assert database.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    else:
        journal.with_name(journal.name + "-journal").write_bytes(b"unquiesced recovery evidence")
    before = {path: path.read_bytes() for path in journal.parent.iterdir() if path.is_file()}
    directory_time = journal.parent.stat().st_mtime_ns
    original = interface.migration.readonly
    def guarded(path):
        assert path != journal, "Dependent SQLite evidence must be rejected before the status connection opens"
        return original(path)
    monkeypatch.setattr(interface.migration, "readonly", guarded)
    with pytest.raises(SystemExit, match="WAL|sidecar|readonly"):
        _run(prepared, "status", capsys)
    assert journal.parent.stat().st_mtime_ns == directory_time
    assert {path: path.read_bytes() for path in journal.parent.iterdir() if path.is_file()} == before


def test_backup_read_only_mount_and_fixed_evidence_only(prepared, monkeypatch, tmp_path):
    from services import backup_mirror
    backup = tmp_path / "mount" / "backups" / prepared[0].name
    backup.mkdir(parents=True)
    monkeypatch.setattr(interface, "BACKUP", backup)
    mount = {"uuid": "687B-AC21", "label": "PLAYABLE", "fstype": "exfat", "options": "ro",
             "target": str(backup.parent.parent), "source": "/dev/sda1"}
    monkeypatch.setattr(backup_mirror, "mounted_filesystems", lambda: [mount])
    marker = {"schema_version": 1, "purpose": "crossing-project-mirror", "source_project": str(prepared[0]),
              "project": prepared[0].name, "device_uuid": "687B-AC21", "mirror_policy": "current-project-state",
              "last_verified_plan": interface.BACKUP_PLAN, "journal_sha256": interface.BACKUP_JOURNAL, "reconciled": True}
    marker_path = backup / backup_mirror.MARKER
    payload = json.dumps(marker).encode()
    marker_path.write_bytes(payload + b" " * (2715 - len(payload)))
    monkeypatch.setattr(interface, "MARKER_DIGEST", digest(marker_path))
    control = backup / ".crossing-mirror-control"
    control.mkdir()
    for prefix, size in (("metadata-", 2489290752), ("journal-", 998649856)):
        with (control / (prefix + interface.BACKUP_PLAN + ".sqlite3")).open("wb") as stream:
            stream.truncate(size)
    read_paths = []
    original = interface.digest
    def bounded_hash(path):
        read_paths.append(path)
        assert path == marker_path, "Large backup evidence content was read"
        return original(path)
    monkeypatch.setattr(interface, "digest", bounded_hash)
    result = interface._backup(prepared[0])
    assert result["mount"]["uuid"] == "687B-AC21"
    assert len(result["retained"]) == 2
    assert read_paths == [marker_path]


@pytest.mark.parametrize("boundary", ["before-acceptance-recording", "acceptance-recorded", "integrated:release-intent",
                                      "before-release", "integrated:released", "release-state-published", "release-request-retired"])
def test_cli_acceptance_and_release_interruptions(prepared, capsys, monkeypatch, boundary):
    _run(prepared, "start", capsys)
    action = "accept" if boundary in ("before-acceptance-recording", "acceptance-recorded") else "release"
    if action == "release":
        _run(prepared, "accept", capsys)
    original = interface.dispatch
    def stop(event):
        if event == boundary:
            raise RuntimeError("CLI interrupted at " + event)
    monkeypatch.setattr(interface, "dispatch", lambda args, **options: original(args, interrupt=stop, **options))
    with pytest.raises(SystemExit, match="interrupted"):
        _run(prepared, action, capsys)
    capsys.readouterr()
    monkeypatch.setattr(interface, "dispatch", original)
    _run(prepared, "status", capsys)
    _run(prepared, action, capsys)
    if action == "accept":
        _run(prepared, "release", capsys)
    assert _run(prepared, "status", capsys)["maintenance"] == "open"


def test_live_lease_refuses_cli_start_and_preserves_drain(prepared, capsys, monkeypatch, tmp_path):
    processes = tmp_path / "processes"
    processes.mkdir()
    monkeypatch.setattr(maintenance_preflight, "_PROC_ROOT", processes)
    ready_read, ready_write = os.pipe()
    stop_read, stop_write = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            with access.session(prepared[0]):
                os.write(ready_write, b"R")
                os.read(stop_read, 1)
        finally:
            os._exit(0)
    try:
        assert os.read(ready_read, 1) == b"R"
        with pytest.raises(SystemExit):
            _run(prepared, "start", capsys)
        capsys.readouterr()
        assert _run(prepared, "status", capsys)["maintenance"] == "maintenance-requested"
    finally:
        os.write(stop_write, b"S")
        os.waitpid(child, 0)
        for descriptor in (ready_read, ready_write, stop_read, stop_write):
            os.close(descriptor)
    assert _run(prepared, "resume", capsys)["outcome"] == "resume"


@pytest.mark.parametrize("evidence", ["plan", "allocation", "recovery", "confirmation"])
def test_authorization_rechecks_live_evidence(prepared, evidence):
    binding = interface.prepare(*prepared)["authorization_binding"]
    if evidence == "confirmation":
        with pytest.raises(access.MaintenanceError, match="confirmation"):
            interface.check_authorization(binding, binding)
        return
    if evidence == "recovery":
        binding["recovery_source"] = {"fixture": False}
    else:
        if evidence == "plan":
            path = prepared[1]
        else:
            with sqlite3.connect(prepared[1]) as database:
                reference = json.loads(database.execute("SELECT value FROM meta WHERE key='identity_registry'").fetchone()[0])
            path = prepared[0] / reference
        path.write_bytes(path.read_bytes() + b"changed approval evidence")
    with pytest.raises((access.MaintenanceError, ValueError)):
        interface.check_authorization(binding, binding, confirmed=True)


@pytest.mark.parametrize("defect", ["unknown_state", "missing_receipt", "wrong_operation"])
def test_status_refuses_inconsistent_evidence_without_changes(prepared, capsys, defect):
    _run(prepared, "start", capsys)
    record_path = prepared[0] / access.CONTROL / "integrated-migration.json"
    if defect == "missing_receipt":
        record_path.unlink()
    else:
        record = json.loads(record_path.read_bytes())
        record["state" if defect == "unknown_state" else "operation"] = "not-recognized"
        record_path.write_text(json.dumps(record))
    before = {path: path.read_bytes() for path in (prepared[0] / access.CONTROL).iterdir() if path.is_file()}
    with pytest.raises(SystemExit):
        _run(prepared, "status", capsys)
    assert all(path.read_bytes() == value for path, value in before.items())


def test_large_cli_workload_reports_periodic_durable_progress(prepared, capsys, monkeypatch):
    project, plan, _ = prepared
    with sqlite3.connect(plan) as database:
        for number in range(1000):
            source = f"data/silhouettes/catalog/movie/synthetic/object-{number}.png"
            destination = f"data/silhouettes/movie/synthetic/objects/object-{number}.png"
            path = project / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic mapped bytes " + str(number).encode())
            info = path.stat()
            database.execute("INSERT INTO files(source,destination,disposition,owner,mode,size,mtime_ns,ctime_ns,inode,device,links,sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (source, destination, "Move", "fixture-owner", "", info.st_size, info.st_mtime_ns,
                 info.st_ctime_ns, info.st_ino, info.st_dev, 1, digest(path)))
    prepared = project, plan, digest(plan)
    snapshots = []
    observed = threading.Event()
    original_report = interface.Progress.report
    def capture(progress, event):
        original_report(progress, event)
        if event == "Periodic checkpoint":
            snapshots.append(interface.status(project, plan, prepared[2], "cli-fixture"))
            observed.set()
    monkeypatch.setattr(interface.Progress, "report", capture)
    original_dispatch = interface.dispatch
    paused = False
    def pause(event):
        nonlocal paused
        if event == "between-moves" and not paused:
            paused = True
            assert observed.wait(12), "No periodic report while the owning operation was blocked"
    monkeypatch.setattr(interface, "dispatch", lambda args, **options: original_dispatch(args, interrupt=pause, **options))
    assert _run(prepared, "start", capsys)["graph_accepted"]
    assert snapshots and snapshots[0]["activity"] == "Running"
    assert snapshots[0]["progress"]["moves"]["total"] == 1044
    final = _run(prepared, "status", capsys)
    assert final["progress"]["moves"]["remaining"] == 0
    assert final["activity"].startswith("Stopped")
    assert not any(thread.name == "migration-progress" for thread in threading.enumerate())