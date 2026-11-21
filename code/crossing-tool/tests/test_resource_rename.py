import os
import json
import sqlite3
import errno
import traceback
import subprocess

import pytest

from data.resource_storage import digest
from services import maintenance_preflight, resource_rename as rename
from services.resource_migration import _schema, execute_fixture_renames
from tool import project_access as access


def test_ordinary_project_cannot_authorize_fixture_renames(tmp_path):
    access.enroll(tmp_path, approved=True)
    with access.maintenance(tmp_path, "rename", approved=True) as owner:
        with pytest.raises(access.IncompleteCoverage, match="temporary fixtures"):
            owner.require_fixture_renames()


def test_fixture_rename_capability_does_not_grant_general_mutation():
    with access.temporary_rename_project() as project:
        assert list(project.iterdir()) == []
        assert project.stat().st_mode & 0o077 == 0
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            assert owner.require_fixture_renames()["state"] == "maintenance"
            with pytest.raises(access.IncompleteCoverage):
                owner.require_resource_mutation()
        with pytest.raises(access.MaintenanceError, match="no longer owned"):
            owner.require_fixture_renames()
        with pytest.raises(access.MaintenanceError, match="release is unavailable"):
            access.release_maintenance(project)


def test_unenrolled_fixture_cannot_enter_maintenance():
    with access.temporary_rename_project() as project:
        with pytest.raises(access.MaintenanceError, match="explicitly enrolled"):
            with access.maintenance(project, "rename", approved=True):
                pytest.fail("unenrolled fixture authorized")
        assert list(project.iterdir()) == []


def _plan(project, count=2):
    plan = project / "plan.sqlite3"
    files = []
    with sqlite3.connect(plan) as database:
        _schema(database)
        database.executemany("INSERT INTO meta VALUES(?,?)", [("project", json.dumps(str(project))), ("plan_revision", json.dumps({"stage": "2B.3d"}))])
        for number in range(count):
            media = "movie" if number % 2 == 0 else "gameplay"
            source = f"data/silhouettes/catalog/{media}/film/bird/object_{number:04}.png"
            destination = f"data/silhouettes/{media}/film/animals/bird/instance-{number}/silhouette.png"
            path = project / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"unchanged opaque PNG fixture " + str(number).encode())
            info = path.stat()
            row = (source, destination, "Move", "fixture-owner", "", info.st_size, info.st_mtime_ns,
                   info.st_ctime_ns, info.st_ino, info.st_dev, 1, digest(path))
            database.execute("INSERT INTO files(source,destination,disposition,owner,mode,size,mtime_ns,ctime_ns,inode,device,links,sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)
            files.append((source, destination, path.read_bytes(), info.st_ino))
        metadata = project / "data/semantic.json"
        metadata.write_text(json.dumps({"shot_id": "tmdb_1@f000001-f000002", "BEST": 1, "field": "animals", "label": "bird", "instance_id": "tmdb_1@f000001-f000002#0001"}))
        database.execute("INSERT INTO files(source,destination,disposition) VALUES('data/semantic.json','data/semantic-new.json','Transform')")
    return plan, digest(plan), files


def _execute(owner, plan, checksum, **kwargs):
    return execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True, **kwargs)


def test_forward_repeat_and_rollback_preserve_bytes_and_unmapped_children():
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        semantic = (project / "data/semantic.json").read_bytes()
        unmapped = project / "data/silhouettes/catalog/movie/film/bird/unmapped.txt"
        unmapped.write_bytes(b"must not move")
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            for _ in range(2):
                result = _execute(owner, plan, checksum)
                assert result["state"] == "files_moved"
                assert result["graph_accepted"] is False
                for source, destination, payload, inode in files:
                    assert not (project / source).exists()
                    assert (project / destination).read_bytes() == payload
                    assert (project / destination).stat().st_ino == inode
            for _ in range(2):
                assert _execute(owner, plan, checksum, action="rollback")["state"] == "rolled_back"
            for source, destination, payload, inode in files:
                assert (project / source).read_bytes() == payload
                assert (project / source).stat().st_ino == inode
                assert not (project / destination).exists()
            with pytest.raises(ValueError, match="cannot resume forward"):
                _execute(owner, plan, checksum)
        assert unmapped.read_bytes() == b"must not move"
        assert (project / "data/semantic.json").read_bytes() == semantic
        assert digest(plan) == checksum


def test_executor_rejects_nonowner_before_any_io(tmp_path):
    before = list(tmp_path.iterdir())
    with pytest.raises(access.MaintenanceError, match="exclusive inspection owner"):
        _execute(None, tmp_path / "absent", "0" * 64)
    assert list(tmp_path.iterdir()) == before


@pytest.mark.parametrize("boundary", [
    "before-intent", "intent:before-journal-sync", "intent:journal-synced", "intent-committed",
    "before-rename", "renamed", "before-directory-sync", "directories-synced",
    "before-completion", "completion:before-journal-sync", "completion:journal-synced",
    "completion-committed", "between-moves",
])
@pytest.mark.parametrize("reverse", [False, True])
def test_interrupted_move_or_rollback_resumes_idempotently(boundary, reverse):
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            if reverse:
                _execute(owner, plan, checksum)
            action = "rollback" if reverse else "forward"

            def stop(phase):
                if phase == ("rollback:" if reverse else "") + boundary:
                    raise RuntimeError("fixture interruption")

            with pytest.raises(RuntimeError, match="fixture interruption"):
                _execute(owner, plan, checksum, action=action, interrupt=stop)
            for _ in range(2):
                assert _execute(owner, plan, checksum, action=action)["state"] == ("rolled_back" if reverse else "files_moved")
            for source, destination, payload, inode in files:
                path = project / (source if reverse else destination)
                assert path.read_bytes() == payload and path.stat().st_ino == inode


def test_missing_intent_evidence_refuses_without_guessing():
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            result = _execute(owner, plan, checksum)
            with sqlite3.connect(result["journal"]) as database:
                database.execute("DELETE FROM events")
            with pytest.raises(ValueError, match="Incomplete journal"):
                _execute(owner, plan, checksum)
        assert all((project / destination).read_bytes() == payload for _, destination, payload, _ in files)


@pytest.mark.parametrize("boundary", ["before-intent", "intent:before-journal-sync", "intent-committed", "before-rename", "renamed", "before-directory-sync", "completion:before-journal-sync", "completion:journal-synced", "between-moves"])
@pytest.mark.parametrize("reverse", [False, True])
def test_process_exit_then_fresh_worker_recovers(boundary, reverse, tmp_path, monkeypatch):
    processes = tmp_path / "isolated-proc"
    processes.mkdir()
    monkeypatch.setattr(maintenance_preflight, "_PROC_ROOT", processes)
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        access.enroll(project, approved=True)
        if reverse:
            with access.maintenance(project, "rename", approved=True) as owner:
                _execute(owner, plan, checksum)
        child = os.fork()
        if child == 0:
            try:
                def stop(phase):
                    if phase == ("rollback:" if reverse else "") + boundary:
                        os._exit(73)
                with access.maintenance(project, "rename", approved=True) as owner:
                    _execute(owner, plan, checksum, action="rollback" if reverse else "forward", interrupt=stop)
            except BaseException:
                traceback.print_exc()
            finally:
                os._exit(91)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 73
        assert access.status(project)["state"] == "maintenance"
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(project):
                pytest.fail("process exit reopened project")
        resumed = os.fork()
        if resumed == 0:
            try:
                with access.maintenance(project, "rename", approved=True) as owner:
                    _execute(owner, plan, checksum, action="rollback" if reverse else "forward")
            except BaseException:
                traceback.print_exc()
                os._exit(92)
            os._exit(0)
        _, status = os.waitpid(resumed, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        with access.maintenance(project, "rename", approved=True) as owner:
            _execute(owner, plan, checksum, action="rollback" if reverse else "forward")
        for source, destination, payload, inode in files:
            path = project / (source if reverse else destination)
            assert path.read_bytes() == payload and path.stat().st_ino == inode


@pytest.mark.parametrize("phase", ["before-journal-create", "intent:before-journal-sync", "before-completion", "completion:before-journal-sync", "before-directory-sync"])
@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES])
def test_filesystem_failure_never_reports_completion(phase, code):
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            def fail(event):
                if event == phase:
                    raise OSError(code, "fixture filesystem failure")
            with pytest.raises(OSError, match="fixture filesystem failure"):
                _execute(owner, plan, checksum, interrupt=fail)
            assert _execute(owner, plan, checksum)["state"] == "files_moved"
        assert all((project / destination).read_bytes() == payload for _, destination, payload, _ in files)


@pytest.mark.parametrize("kind", ["both-present", "both-absent", "wrong-destination", "wrong-source", "new-inode", "directory", "symlink"])
def test_recovery_conflicts_stop_and_preserve_evidence(kind):
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project, count=1)
        source, destination, payload, _ = files[0]
        source, destination = project / source, project / destination
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            def stop(event):
                if event == "intent-committed":
                    raise RuntimeError("stop")
            with pytest.raises(RuntimeError):
                _execute(owner, plan, checksum, interrupt=stop)
            if kind == "both-present":
                destination.write_bytes(payload)
            elif kind == "both-absent":
                source.unlink()
            elif kind == "wrong-destination":
                source.rename(destination)
                destination.write_bytes(b"unexpected")
            elif kind == "wrong-source":
                source.write_bytes(b"unexpected")
            elif kind == "new-inode":
                replacement = source.with_suffix(".replacement")
                replacement.write_bytes(payload)
                replacement.replace(source)
            elif kind == "directory":
                source.unlink()
                source.mkdir()
            else:
                source.rename(source.with_suffix(".saved"))
                source.symlink_to(source.with_suffix(".saved"))
            journal = project / access.CONTROL / "rename-rename.sqlite3"
            before = journal.read_bytes()
            with pytest.raises((ValueError, OSError)):
                _execute(owner, plan, checksum)
            assert journal.read_bytes() == before


def test_atomic_noreplace_refuses_a_late_collision(monkeypatch):
    original = rename._rename
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project, count=1)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            def collide(source_parent, source, destination_parent, destination):
                descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=destination_parent)
                os.write(descriptor, b"late collision")
                os.close(descriptor)
                original(source_parent, source, destination_parent, destination)
            monkeypatch.setattr(rename, "_rename", collide)
            with pytest.raises(FileExistsError):
                _execute(owner, plan, checksum)
        assert (project / files[0][0]).read_bytes() == files[0][2]
        assert (project / files[0][1]).read_bytes() == b"late collision"


def test_symlink_ancestry_substitution_before_rename_is_refused():
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project, count=1)
        access.enroll(project, approved=True)
        parent = (project / files[0][1]).parent
        with access.maintenance(project, "rename", approved=True) as owner:
            def substitute(event):
                if event == "before-rename":
                    parent.rename(parent.with_name("saved-parent"))
                    parent.symlink_to(parent.with_name("saved-parent"), target_is_directory=True)
            with pytest.raises(OSError):
                _execute(owner, plan, checksum, interrupt=substitute)
        assert (project / files[0][0]).read_bytes() == files[0][2]
        assert not (parent.with_name("saved-parent") / "silhouette.png").exists()


@pytest.mark.parametrize("kind", ["checksum", "revision", "project", "blocking-issue", "absolute", "escape", "control", "self", "source-links", "missing-hash", "device"])
def test_plan_applicability_failure_never_moves_resources(kind):
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project, count=1)
        with sqlite3.connect(plan) as database:
            if kind in ("revision", "project"):
                database.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps({"stage": "wrong"} if kind == "revision" else "/another-project"), "plan_revision" if kind == "revision" else "project"))
            elif kind == "blocking-issue":
                database.execute("INSERT INTO issues VALUES('fixture','','blocked',1)")
            elif kind in ("absolute", "escape", "control", "self"):
                destination = {"absolute": "/tmp/outside-fixture", "escape": "../escape", "control": access.CONTROL + "/evidence", "self": files[0][0]}[kind]
                database.execute("UPDATE files SET destination=? WHERE disposition='Move'", (destination,))
            elif kind == "source-links":
                database.execute("UPDATE files SET links=2 WHERE disposition='Move'")
            elif kind == "missing-hash":
                database.execute("UPDATE files SET sha256=NULL WHERE disposition='Move'")
            elif kind == "device":
                database.execute("UPDATE files SET device=device+1 WHERE disposition='Move'")
        checksum = "0" * 64 if kind == "checksum" else digest(plan)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            with pytest.raises(ValueError):
                _execute(owner, plan, checksum)
        assert all((project / source).read_bytes() == payload and not (project / destination).exists() for source, destination, payload, _ in files)


@pytest.mark.parametrize("kind", ["header", "mapping", "state", "parents", "bytes", "sidecar", "hardlink"])
def test_changed_journal_is_an_explicit_stop(kind):
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            result = _execute(owner, plan, checksum)
            journal = project / access.CONTROL / "rename-rename.sqlite3"
            if kind in ("header", "mapping", "state", "parents"):
                with sqlite3.connect(journal) as database:
                    statement = {"header": "UPDATE meta SET payload='{}'", "mapping": "UPDATE moves SET frozen='{}'",
                                 "state": "UPDATE moves SET state='invented'", "parents": "UPDATE moves SET parents='[]'"}[kind]
                    database.execute(statement)
            elif kind == "bytes":
                journal.write_bytes(b"corrupt journal")
            elif kind == "sidecar":
                journal.with_name(journal.name + "-journal").symlink_to(plan)
            else:
                os.link(journal, journal.with_name("extra-hardlink"))
            with pytest.raises((ValueError, sqlite3.DatabaseError)):
                _execute(owner, plan, checksum)
            assert all((project / destination).read_bytes() == payload for _, destination, payload, _ in files)
            assert result["resource_mutation_authorized"] is False


@pytest.mark.parametrize("phase", ["prepared:before-journal-sync", "prepared:journal-synced"])
def test_interrupted_initialization_is_complete_or_explicitly_blocked(phase):
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            def stop(event):
                if event == phase:
                    raise RuntimeError("initialization interrupted")
            with pytest.raises(RuntimeError):
                _execute(owner, plan, checksum, interrupt=stop)
            assert all((project / source).read_bytes() == payload for source, _, payload, _ in files)
            if phase == "prepared:before-journal-sync":
                with pytest.raises(ValueError, match="Journal identity"):
                    _execute(owner, plan, checksum)
            else:
                assert _execute(owner, plan, checksum)["state"] == "files_moved"


@pytest.mark.parametrize("code", [errno.EXDEV, errno.ENOSYS, errno.EOPNOTSUPP, errno.EACCES, errno.ENOSPC])
def test_rename_syscall_failure_does_not_copy_or_overwrite(code, monkeypatch):
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            def fail(*args):
                raise OSError(code, "fixture rename failure")
            with monkeypatch.context() as patch:
                patch.setattr(rename, "_rename", fail)
                with pytest.raises(OSError):
                    _execute(owner, plan, checksum)
            assert all((project / source).read_bytes() == payload and not (project / destination).exists() for source, destination, payload, _ in files)
            _execute(owner, plan, checksum, action="rollback")


@pytest.mark.parametrize("resource", ["bytes", "inodes"])
def test_initial_capacity_failure_has_no_journal_or_moves(resource, monkeypatch):
    class Capacity:
        f_bavail = 0 if resource == "bytes" else 10**12
        f_frsize = 4096
        f_favail = 0 if resource == "inodes" else 10**12
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            monkeypatch.setattr(rename.os, "fstatvfs", lambda descriptor: Capacity())
            with pytest.raises(OSError, match="headroom"):
                _execute(owner, plan, checksum)
            assert not (project / access.CONTROL / "rename-rename.sqlite3").exists()
            assert all((project / source).read_bytes() == payload for source, _, payload, _ in files)


@pytest.mark.parametrize("phase", ["intent:before-journal-sync", "before-directory-sync", "completion:before-journal-sync"])
def test_real_directory_fsync_error_retains_recoverable_state(phase, monkeypatch):
    original = os.fsync
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            def fail(descriptor):
                raise OSError(errno.ENOSPC, "fixture fsync failure")
            def arm(event):
                if event == phase:
                    monkeypatch.setattr(os, "fsync", fail)
            with pytest.raises(OSError, match="fsync failure"):
                _execute(owner, plan, checksum, interrupt=arm)
            monkeypatch.setattr(os, "fsync", original)
            assert _execute(owner, plan, checksum)["state"] == "files_moved"
            assert all((project / destination).read_bytes() == payload for _, destination, payload, _ in files)


def test_sqlite_write_failure_leaves_durable_intent_and_can_rollback(monkeypatch):
    original = sqlite3.connect
    class FailingConnection(sqlite3.Connection):
        def execute(self, query, parameters=(), /):
            if query.startswith("UPDATE moves") and parameters[0] == "done":
                raise sqlite3.OperationalError("database or disk is full")
            return super().execute(query, parameters)
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            def connect(*args, **kwargs):
                return original(*args, **kwargs, factory=FailingConnection)
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3, "connect", connect)
                with pytest.raises(sqlite3.OperationalError, match="disk is full"):
                    _execute(owner, plan, checksum)
            assert _execute(owner, plan, checksum, action="rollback")["state"] == "rolled_back"
        assert all((project / source).read_bytes() == payload for source, _, payload, _ in files)


def test_no_stopped_client_acknowledgement_means_no_journal():
    with access.temporary_rename_project() as project:
        plan, checksum, _ = _plan(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            with pytest.raises(access.MaintenanceError, match="acknowledgement"):
                execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d")
            assert not (project / access.CONTROL / "rename-rename.sqlite3").exists()


def test_plan_change_immediately_before_rename_is_refused():
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            def change(event):
                if event == "before-rename":
                    with sqlite3.connect(plan) as database:
                        database.execute("INSERT INTO meta VALUES('unexpected','true')")
            with pytest.raises(ValueError, match="Frozen plan changed"):
                _execute(owner, plan, checksum, interrupt=change)
            assert all((project / source).read_bytes() == payload for source, _, payload, _ in files)


def test_rollback_refuses_original_path_collision_without_overwrite():
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project, count=1)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            _execute(owner, plan, checksum)
            (project / files[0][0]).write_bytes(b"new original-path occupant")
            with pytest.raises(ValueError):
                _execute(owner, plan, checksum, action="rollback")
        assert (project / files[0][0]).read_bytes() == b"new original-path occupant"
        assert (project / files[0][1]).read_bytes() == files[0][2]


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES])
def test_directory_creation_failure_is_recoverable(code, monkeypatch):
    original = os.mkdir
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "rename", approved=True) as owner:
            def fail(path, mode=0o777, *, dir_fd=None):
                if dir_fd is not None:
                    raise OSError(code, "fixture mkdir failure")
                return original(path, mode)
            with monkeypatch.context() as patch:
                patch.setattr(os, "mkdir", fail)
                with pytest.raises(OSError, match="mkdir failure"):
                    _execute(owner, plan, checksum)
            assert _execute(owner, plan, checksum, action="rollback")["state"] == "rolled_back"
        assert all((project / source).read_bytes() == payload for source, _, payload, _ in files)


def test_explicit_known_live_client_blocks_executor_then_can_be_rechecked():
    with access.temporary_rename_project() as project:
        plan, checksum, _ = _plan(project)
        access.enroll(project, approved=True)
        child = subprocess.Popen(["/bin/cat"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, cwd="/tmp")
        try:
            with access.maintenance(project, "rename", approved=True) as owner:
                with pytest.raises(access.MaintenanceBusy, match="clients"):
                    _execute(owner, plan, checksum, known_pids=(child.pid,))
                assert not (project / access.CONTROL / "rename-rename.sqlite3").exists()
                child.communicate()
                assert _execute(owner, plan, checksum)["state"] == "files_moved"
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


def test_bounded_fixture_storage_measurement():
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project, count=256)
        access.enroll(project, approved=True)
        def allocated():
            return sum(path.lstat().st_blocks * 512 for path in project.rglob("*"))
        before = allocated()
        with access.maintenance(project, "rename", approved=True) as owner:
            result = _execute(owner, plan, checksum)
            forward = allocated()
            journal_bytes = os.stat(result["journal"]).st_size
            _execute(owner, plan, checksum, action="rollback")
            after = allocated()
        assert max(forward, after) - before < result["reserve_bytes"]
        assert all((project / source).stat().st_ino == inode and (project / source).read_bytes() == payload for source, _, payload, inode in files)
        print("STORAGE_MEASUREMENT " + json.dumps({"files": len(files), "journal_forward_bytes": journal_bytes,
              "journal_rollback_bytes": os.stat(result["journal"]).st_size, "baseline_allocated_bytes": before,
              "forward_allocated_bytes": forward, "rollback_allocated_bytes": after, "reserve_bytes": result["reserve_bytes"]}, sort_keys=True))


def test_parent_substitution_after_rename_never_records_completion():
    with access.temporary_rename_project() as project:
        plan, checksum, files = _plan(project, count=1)
        access.enroll(project, approved=True)
        parent = (project / files[0][1]).parent
        with access.maintenance(project, "rename", approved=True) as owner:
            def substitute(event):
                if event == "directories-synced":
                    parent.rename(parent.with_name("saved-parent"))
                    parent.mkdir()
            with pytest.raises(ValueError, match="after rename"):
                _execute(owner, plan, checksum, interrupt=substitute)
            with sqlite3.connect(project / access.CONTROL / "rename-rename.sqlite3") as database:
                assert database.execute("SELECT state FROM moves").fetchone()[0] == "intent"
        assert (parent.with_name("saved-parent") / "silhouette.png").read_bytes() == files[0][2]