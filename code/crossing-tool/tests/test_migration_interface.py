import json
from contextlib import closing, contextmanager
import os
import sqlite3
import subprocess
import sys
import threading
import traceback
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
    original = cli.prefs.get
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: default)
    monkeypatch.setattr(cli.prefs, "set", lambda *args, **kwargs: None)
    return original


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


@pytest.mark.parametrize("boundary", ["intent-committed", "renamed"])
def test_cli_repeated_resume_reconciles_without_duplicate_moves(prepared, capsys, monkeypatch, boundary):
    from services import resource_rename as renames
    from services.resource_migration import readonly
    project, plan, _ = prepared
    journal = project / access.CONTROL / "rename-cli-fixture.sqlite3"
    with closing(readonly(plan)) as database:
        destinations = {str(project / row[0]) for row in database.execute("SELECT destination FROM files WHERE disposition='Move'")}
    performed = []
    original_rename = renames._rename
    original_dispatch = interface.dispatch

    def rename(source_parent, source, destination_parent, destination):
        target = os.path.join(os.readlink(f"/proc/self/fd/{destination_parent}"), destination)
        original_rename(source_parent, source, destination_parent, destination)
        if target in destinations:
            performed.append(target)

    def interrupted(action, completed):
        count = 0
        def stop(event):
            nonlocal count
            if event == "completion-committed":
                count += 1
            if event == boundary and count == completed:
                raise RuntimeError("Qualification interruption")
        monkeypatch.setattr(interface, "dispatch", lambda args, **options: original_dispatch(args, interrupt=stop, **options))
        with pytest.raises(SystemExit, match="Qualification interruption"):
            _run(prepared, action, capsys)
        capsys.readouterr()
        monkeypatch.setattr(interface, "dispatch", original_dispatch)

    monkeypatch.setattr(renames, "_rename", rename)
    interrupted("start", 4)
    assert _run(prepared, "status", capsys)["progress"]["moves"]["completed"] == 4
    with closing(readonly(journal)) as database:
        completed = {row[0]: ((project / row[0]).stat().st_ino, digest(project / row[0]))
                     for row in database.execute("SELECT destination FROM moves WHERE state='done'")}
        assert database.execute("SELECT count(*) FROM moves WHERE state='intent'").fetchone()[0] == 1
    interrupted("resume", 2)
    assert _run(prepared, "status", capsys)["progress"]["moves"]["completed"] > 4
    assert _run(prepared, "resume", capsys)["outcome"] == "resume"
    assert _run(prepared, "status", capsys)["progress"]["moves"]["completed"] == 44
    assert len(performed) == len(set(performed)) == 44
    assert all(((project / target).stat().st_ino, digest(project / target)) == value for target, value in completed.items())
    with closing(readonly(journal)) as database:
        assert database.execute("SELECT count(*) FROM moves WHERE state!='done'").fetchone()[0] == 0
        assert not database.execute("SELECT source FROM events WHERE state='done' GROUP BY source HAVING count(*)!=1").fetchall()
    with pytest.raises(access.MaintenanceClosed):
        with access.operation(project):
            pytest.fail("Resume released maintenance")


@pytest.mark.parametrize("defect", ["destination", "source", "journal", "plan"])
def test_cli_resume_refuses_altered_relocation_evidence(prepared, capsys, monkeypatch, defect):
    from services.resource_migration import readonly
    project, plan, _ = prepared
    original = interface.dispatch
    def stop(event):
        if event == "intent-committed":
            raise RuntimeError("Qualification interruption")
    monkeypatch.setattr(interface, "dispatch", lambda args, **options: original(args, interrupt=stop, **options))
    with pytest.raises(SystemExit, match="Qualification interruption"):
        _run(prepared, "start", capsys)
    capsys.readouterr()
    monkeypatch.setattr(interface, "dispatch", original)
    journal = project / access.CONTROL / "rename-cli-fixture.sqlite3"
    with closing(readonly(journal)) as database:
        row = database.execute("SELECT source,destination FROM moves WHERE state='intent'").fetchone()
    artifact = project / row[0]
    if defect == "destination":
        artifact = project / row[1]
        artifact.write_bytes(b"independent destination; never overwrite")
    elif defect == "source":
        artifact.write_bytes(b"altered source")
    elif defect == "journal":
        artifact = journal
        with closing(sqlite3.connect(journal)) as database:
            database.execute("DELETE FROM events")
            database.commit()
    else:
        artifact = plan
        artifact.write_bytes(artifact.read_bytes() + b"altered plan")
    before = artifact.read_bytes()
    with pytest.raises(SystemExit):
        _run(prepared, "resume", capsys)
    capsys.readouterr()
    assert artifact.read_bytes() == before
    assert access.status(project)["state"] == "maintenance"


@pytest.mark.parametrize("boundary,reference", [
    ("metadata-replaced", ".crossing-maintenance/graph-cli-fixture.sqlite3"),
    ("index-staged", ".crossing-maintenance/graph-cli-fixture.sqlite3"),
    ("registry-pair-committed", "data/indexes/.graph-cli-fixture.sqlite3"),
])
def test_cli_later_phase_slow_progress_reader(prepared, capsys, monkeypatch, boundary, reference):
    original_window = interface._evidence_database
    original_connect = sqlite3.connect
    original_dispatch = interface.dispatch
    entered = threading.Event()
    release = threading.Event()
    updates = []
    worker = None
    progress = interface.Progress(*prepared, "cli-fixture", updates.append)

    @contextmanager
    def delayed(project, requested):
        with original_window(project, requested) as database:
            if requested == reference and threading.current_thread().name == "qualification-reader":
                entered.set()
                release.wait(0.2)
            yield database

    def connect(path, *arguments, **options):
        if "mode=ro" not in str(path):
            options["timeout"] = 0.05
        return original_connect(path, *arguments, **options)

    def poll(event):
        nonlocal worker
        if event == boundary and worker is None:
            worker = threading.Thread(target=lambda: progress.report(boundary), name="qualification-reader")
            worker.start()
            assert entered.wait(5), "Progress did not inspect the expected database"

    monkeypatch.setattr(interface, "_evidence_database", delayed)
    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(interface, "dispatch", lambda args, **options: original_dispatch(args, interrupt=poll, **options))
    try:
        assert _run(prepared, "start", capsys)["graph_accepted"]
    finally:
        release.set()
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive()
    assert worker is not None
    assert updates


def test_cli_later_phase_uncommitted_progress_defers(prepared, capsys, monkeypatch):
    boundaries = {"metadata-prepared:before-sync", "transform:done:before-sync",
                  "registry-prepared:before-sync", "index-built:before-sync"}
    updates = []
    seen = set()
    progress = interface.Progress(*prepared, "cli-fixture", updates.append)
    original = interface.dispatch
    def poll(event):
        if event in boundaries:
            seen.add(event)
            progress.report(event)
    monkeypatch.setattr(interface, "dispatch", lambda args, **options: original(args, interrupt=poll, **options))
    assert _run(prepared, "start", capsys)["graph_accepted"]
    assert seen == boundaries
    assert updates
    assert all("progress" not in update and "Migration database checkpoint busy" in update["detail"] for update in updates)
    snapshot = _run(prepared, "status", capsys)
    assert snapshot["progress"]["transforms"]["completed"] == 19
    assert snapshot["progress"]["index_outputs"]["completed"] == 15


def test_failed_journal_guard_acquisition_can_retry(tmp_path):
    from services import resource_graph as graph, resource_rename as renames
    with closing(graph._journal_database(tmp_path / "graph.sqlite3")) as database:
        database.execute("CREATE TABLE evidence(value TEXT)")
        with renames.journal_window():
            with pytest.raises(RuntimeError, match="Cannot write while inspecting"):
                database.execute("INSERT INTO evidence VALUES('refused')")
        assert database._window is None
        database.execute("INSERT INTO evidence VALUES('retry')")
        with pytest.raises(RuntimeError, match="checkpoint busy"):
            with renames.journal_window():
                pytest.fail("Retried write bypassed the guard")
        database.rollback()
        with renames.journal_window():
            assert database._window is None


@pytest.mark.parametrize("method,arguments", [
    ("execute", ("SELECT 1",)),
    ("executemany", ("INSERT INTO evidence VALUES(?)", [("closed",)])),
    ("executescript", ("SELECT 1;",)),
    ("commit", ()),
    ("rollback", ()),
])
def test_closed_journal_operations_release_guard(tmp_path, method, arguments):
    from services import resource_graph as graph, resource_rename as renames
    database = graph._journal_database(tmp_path / "graph.sqlite3")
    database.close()
    try:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            getattr(database, method)(*arguments)
        with renames.journal_window():
            assert database._window is None
    finally:
        database._release()


@pytest.mark.parametrize("method,finish,expected", [
    ("execute", "commit", 1),
    ("executemany", "rollback", 0),
    ("executescript", "close", 0),
])
def test_graph_journal_concurrent_readers_wait_for_transaction_end(tmp_path, method, finish, expected):
    from services import resource_graph as graph
    control = tmp_path / access.CONTROL
    control.mkdir()
    reference = access.CONTROL + "/graph-fixture.sqlite3"
    observations = []

    def read():
        try:
            with interface._evidence_database(tmp_path, reference) as snapshot:
                observations.append(snapshot.execute("SELECT count(*) FROM evidence").fetchone()[0])
        except RuntimeError as error:
            observations.append(str(error))

    def poll():
        worker = threading.Thread(target=read)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()

    with closing(graph._journal_database(tmp_path / reference)) as database:
        database.execute("CREATE TABLE evidence(value TEXT)")
        if method == "execute":
            database.execute("INSERT INTO evidence VALUES('uncommitted')")
        elif method == "executemany":
            database.executemany("INSERT INTO evidence VALUES(?)", [("uncommitted",)])
        else:
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                database.executescript("BEGIN; INSERT INTO evidence VALUES('uncommitted'); SELECT * FROM missing;")
        poll()
        assert len(observations) == 1 and "checkpoint busy" in observations[0]
        getattr(database, finish)()
        assert database._window is None
        poll()
        assert observations[-1] == expected


def test_failed_graph_transaction_releases_checkpoint_guard(tmp_path):
    from services import resource_graph as graph, resource_rename as renames
    from services.resource_migration import readonly
    path = tmp_path / "graph.sqlite3"
    with pytest.raises(RuntimeError, match="injected failure"):
        with closing(graph._journal_database(path)) as database:
            database.execute("CREATE TABLE evidence(value TEXT)")
            database.execute("INSERT INTO evidence VALUES('uncommitted')")
            with pytest.raises(RuntimeError, match="checkpoint busy"):
                with renames.journal_window():
                    pytest.fail("Read entered an uncommitted write")
            raise RuntimeError("injected failure")
    with renames.journal_window(), closing(readonly(path)) as database:
        assert database.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0


def test_cli_external_sqlite_reader_refuses_safely_then_resumes(prepared, capsys, monkeypatch, tmp_path):
    from services.resource_migration import readonly
    project = prepared[0]
    journal = project / access.CONTROL / "rename-cli-fixture.sqlite3"
    original = interface.dispatch
    completed = 0
    def stop(event):
        nonlocal completed
        if event == "completion-committed":
            completed += 1
            if completed == 4:
                raise RuntimeError("Qualification interruption")
    with monkeypatch.context() as scoped:
        scoped.setattr(interface, "dispatch", lambda args, **options: original(args, interrupt=stop, **options))
        with pytest.raises(SystemExit, match="Qualification interruption"):
            _run(prepared, "start", capsys)
    capsys.readouterr()
    code = ("import sqlite3,sys; database=sqlite3.connect(sys.argv[1],uri=True); "
            "database.execute('BEGIN'); database.execute('SELECT count(*) FROM moves').fetchone(); "
            "print('ready',flush=True); sys.stdin.read(); database.rollback(); database.close()")
    process = subprocess.Popen([sys.executable, "-c", code, journal.as_uri() + "?mode=ro"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    original_connect = sqlite3.connect
    processes = tmp_path / "unmanaged-reader-proc"
    processes.mkdir()
    def connect(path, *arguments, **options):
        if str(path).startswith("file:/proc/self/fd/"):
            options["timeout"] = 0.05
        return original_connect(path, *arguments, **options)
    try:
        assert process.stdout.readline().strip() == "ready"
        with monkeypatch.context() as scoped:
            scoped.setattr(maintenance_preflight, "_PROC_ROOT", processes)
            scoped.setattr(sqlite3, "connect", connect)
            with pytest.raises(SystemExit, match="database is locked"):
                _run(prepared, "resume", capsys)
        capsys.readouterr()
        assert access.status(project)["state"] == "maintenance"
        with closing(readonly(journal)) as database:
            assert database.execute("SELECT count(*) FROM moves WHERE state='done'").fetchone()[0] == 4
            assert database.execute("SELECT count(*) FROM moves WHERE state='intent'").fetchone()[0] == 0
    finally:
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        process.stdout.close()
    assert _run(prepared, "resume", capsys)["outcome"] == "resume"
    assert _run(prepared, "status", capsys)["progress"]["moves"]["completed"] == 44
    assert access.status(project)["state"] == "maintenance"


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


def test_status_with_real_project_preferences_while_closed(prepared, capsys, monkeypatch, sandbox_prefs, tmp_path):
    project, plan, checksum = prepared
    user_file = tmp_path / "user-prefs.json"
    user_file.write_text(json.dumps({"path": str(project)}))
    monkeypatch.setattr(cli.prefs, "_USER_FILE", user_file)
    monkeypatch.setattr(cli.prefs, "get", sandbox_prefs)
    access.enroll(project, approved=True)
    with access.maintenance(project, "cli-fixture", approved=True):
        pass
    arguments = ["crossing", "migration", "status", "--project", str(project), "--plan", str(plan),
                 "--plan-sha256", checksum, "--operation-id", "cli-fixture", "--json"]
    with patch.object(sys, "argv", arguments):
        cli.main()
    assert json.loads(capsys.readouterr().out)["maintenance"] == "maintenance"
    with pytest.raises(access.MaintenanceClosed):
        with access.operation(project):
            pytest.fail("Status reopened ordinary project admission")
    with patch.object(sys, "argv", ["crossing", "index", "illustration"]):
        with pytest.raises(SystemExit, match="Project access refused"):
            cli.main()


def test_slow_concurrent_progress_read_does_not_abort_relocation(prepared, capsys, monkeypatch):
    original_window = interface._evidence_database
    original_connect = sqlite3.connect
    entered = threading.Event()
    released = threading.Event()
    worker = None
    progress = interface.Progress(*prepared, "cli-fixture", lambda value: None)

    @contextmanager
    def delayed(project, reference):
        with original_window(project, reference) as database:
            if reference.endswith("rename-cli-fixture.sqlite3") and threading.current_thread().name == "held-progress":
                entered.set()
                released.wait(0.2)
            yield database

    def connect(path, *arguments, **options):
        if str(path).startswith("file:/proc/self/fd/"):
            options["timeout"] = 0.05
        return original_connect(path, *arguments, **options)

    def poll(event):
        nonlocal worker
        if event == "between-moves" and worker is None:
            worker = threading.Thread(target=lambda: progress.report("Concurrent checkpoint"), name="held-progress")
            worker.start()
            assert entered.wait(5)

    monkeypatch.setattr(interface, "_evidence_database", delayed)
    monkeypatch.setattr(sqlite3, "connect", connect)
    args = _args(prepared, "start")
    try:
        result = interface.dispatch(args, interrupt=poll)
    finally:
        released.set()
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive()
    assert result["graph_accepted"]
    assert interface.status(*prepared, "cli-fixture")["progress"]["moves"]["completed"] == 44


def test_progress_inside_journal_write_defers_uncommitted_counts(prepared):
    updates = []
    progress = interface.Progress(*prepared, "cli-fixture", updates.append)
    boundaries = {"prepared:before-journal-sync", "intent:before-journal-sync", "completion:before-journal-sync"}

    def poll(event):
        if event in boundaries:
            progress.report(event)

    result = interface.dispatch(_args(prepared, "start"), interrupt=poll)
    assert result["graph_accepted"]
    assert updates
    assert all(update["status"] == "Waiting for a readable durable checkpoint" for update in updates)
    assert all("Migration database checkpoint busy" in update["detail"] for update in updates)
    assert all("progress" not in update for update in updates)
    assert interface.status(*prepared, "cli-fixture")["progress"]["moves"]["completed"] == 44


@pytest.mark.parametrize("boundary", ["intent-committed", "renamed", "completion:before-journal-sync"])
def test_exited_worker_status_and_owned_recovery_preserve_barrier(prepared, capsys, monkeypatch, tmp_path, boundary):
    processes = tmp_path / "proc"
    processes.mkdir()
    monkeypatch.setattr(maintenance_preflight, "_PROC_ROOT", processes)
    args = _args(prepared, "start")
    child = os.fork()
    if child == 0:
        try:
            def stop(event):
                if event == boundary:
                    os._exit(73)
            interface.dispatch(args, interrupt=stop)
        except BaseException:
            traceback.print_exc()
        os._exit(91)
    _, result = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(result) == 73
    project = prepared[0]
    assert access.status(project)["state"] == "maintenance"
    before = {path: path.read_bytes() for path in (project / access.CONTROL).iterdir() if path.is_file()}
    if boundary == "completion:before-journal-sync":
        with pytest.raises(SystemExit, match="readonly|sidecar"):
            _run(prepared, "status", capsys)
        capsys.readouterr()
    else:
        snapshot = _run(prepared, "status", capsys)
        assert snapshot["maintenance"] == "maintenance"
        assert snapshot["progress"]["moves"]["completed"] == 0
        assert snapshot["activity"] == "Stopped; recovery requires reconciliation"
    assert all(path.read_bytes() == content for path, content in before.items())
    with pytest.raises(access.MaintenanceClosed):
        with access.operation(project):
            pytest.fail("Exited migration worker reopened ordinary access")
    assert _run(prepared, "resume", capsys)["graph_accepted"]
    assert _run(prepared, "status", capsys)["progress"]["moves"]["completed"] == 44
    with pytest.raises(access.MaintenanceClosed):
        with access.operation(project):
            pytest.fail("Owned recovery reopened ordinary access without release")


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