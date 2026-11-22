import json
import errno
import os
from pathlib import Path
import sys
import threading
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import cli
from data.resource_storage import digest
from services import migration_interface as interface
from services import backup_mirror
from test_resource_integration import _fixture

from services import migration_authorization as authorization
from tool import project_access as access


@pytest.fixture
def authorized_target(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: default)
    monkeypatch.setattr(cli.prefs, "set", lambda *args, **kwargs: None)
    monkeypatch.delenv("CROSSING_PROJECT", raising=False)
    with access.temporary_authorization_project() as project:
        plan, checksum, allocation, originals = _fixture(project)
        originals = {str(path.relative_to(project)): digest(path) for path in project.rglob("*") if path.is_file()}
        backup = tmp_path / "mount" / "backups" / project.name
        backup.mkdir(parents=True)
        monkeypatch.setattr(interface, "BACKUP", backup)
        mount = {"uuid": "687B-AC21", "label": "PLAYABLE", "fstype": "exfat", "options": "ro",
                 "target": str(backup.parent.parent), "source": "/dev/sda1"}
        monkeypatch.setattr(backup_mirror, "mounted_filesystems", lambda: [mount])
        marker = {"schema_version": 1, "purpose": "crossing-project-mirror", "source_project": str(project),
                  "project": project.name, "device_uuid": "687B-AC21", "mirror_policy": "current-project-state",
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
        yield project, plan, checksum, allocation, originals, backup


def _cli(target, action, capsys, *, reviewed=None, operation="authorized-cli", confirm=True):
    project, plan, checksum = target[:3]
    arguments = ["crossing", "migration", action, "--project", str(project), "--plan", str(plan),
                 "--plan-sha256", checksum, "--operation-id", operation, "--json"]
    if action not in ("prepare", "status") and confirm:
        arguments += ["--confirm"]
    if action not in ("prepare", "status", "approve", "cancel"):
        arguments += ["--clients-stopped"]
    if action in ("approve", "cancel"):
        arguments += ["--reviewed-identity-sha256", reviewed]
    if action == "approve":
        arguments += ["--operator", "isolated qualification operator"]
    with patch.object(sys, "argv", arguments):
        cli.main()
    return json.loads(capsys.readouterr().out)


def _approve(target, capsys):
    prepared = _cli(target, "prepare", capsys)
    assert prepared["proposed_identity"]["fixture_only"] is False
    assert prepared["backup"].get("fixture") is None
    return _cli(target, "approve", capsys, reviewed=prepared["identity_sha256"])


def test_actual_cli_authorization_install_accept_release_and_replay(authorized_target, capsys):
    project = authorized_target[0]
    assert _cli(authorized_target, "status", capsys)["approval"]["state"] == "missing"
    with pytest.raises(SystemExit, match="no durable approved"):
        _cli(authorized_target, "start", capsys)
    capsys.readouterr()
    assert not (project / access.CONTROL).exists()
    record = _approve(authorized_target, capsys)
    assert record["state"] == "approved"
    assert access.status(project)["state"] == "open"
    assert _cli(authorized_target, "start", capsys)["graph_accepted"]
    with pytest.raises(SystemExit, match="consumed"):
        _cli(authorized_target, "start", capsys)
    capsys.readouterr()
    assert _cli(authorized_target, "validate", capsys)["graph_accepted"]
    assert _cli(authorized_target, "accept", capsys)["state"] == "accepted"
    assert access.status(project)["state"] == "maintenance"
    assert _cli(authorized_target, "release", capsys)["maintenance_released"]
    assert authorization.read(project)["state"] == "completed"
    assert _cli(authorized_target, "release", capsys)["already_released"]
    with access.operation(project):
        assert access.canonical_registry(project)
    with pytest.raises(SystemExit, match="Terminal"):
        _cli(authorized_target, "resume", capsys)


def test_remaining_capacity_uses_verified_journals_through_cli(authorized_target, capsys, monkeypatch):
    _approve(authorized_target, capsys)
    original = interface.dispatch
    def stop(event):
        if event == "metadata-replaced":
            raise RuntimeError("capacity interruption")
    monkeypatch.setattr(interface, "dispatch", lambda args, **options: original(args, interrupt=stop, **options))
    with pytest.raises(SystemExit, match="capacity interruption"):
        _cli(authorized_target, "start", capsys)
    capsys.readouterr()
    monkeypatch.setattr(interface, "dispatch", original)
    result = _cli(authorized_target, "resume", capsys)
    assert result["outcome"] == "resume"
    budget = result["recovery_capacity"]
    assert budget["components"]["safety_margin"] == 4 * 1024**3
    assert budget["peak_additional_bytes"] < authorization.read(authorized_target[0])["identity"]["required_bytes"]


def _interrupt_cli(target, action, event, occurrence, capsys, monkeypatch):
    original = interface.dispatch
    seen = 0
    def stop(value):
        nonlocal seen
        if value == event:
            seen += 1
            if seen == occurrence:
                raise RuntimeError("recovery capacity interruption")
    with monkeypatch.context() as scoped:
        scoped.setattr(interface, "dispatch", lambda args, **options: original(args, interrupt=stop, **options))
        with pytest.raises(SystemExit, match="recovery capacity interruption"):
            _cli(target, action, capsys)
    capsys.readouterr()


def _remaining(target, action):
    record = authorization.read(target[0])
    ready = authorization._fresh_identity(record["identity"])
    authorization._capacity(record, ready, action)
    return ready["recovery_capacity"]


def _limit_space(monkeypatch, available, inodes=1000000):
    original = os.fstatvfs
    def limited(descriptor):
        actual = original(descriptor)
        return SimpleNamespace(f_bavail=available // actual.f_frsize, f_frsize=actual.f_frsize,
                               f_favail=inodes)
    monkeypatch.setattr(os, "fstatvfs", limited)


@pytest.mark.parametrize("event,occurrence", [("before-rename", 1), ("between-moves", 3), ("between-moves", 43),
    ("metadata-retained", 1), ("registry-pair-committed", 1), ("index-staged", 1)])
@pytest.mark.parametrize("action", ["resume", "rollback"])
def test_verified_low_space_recovery_boundaries(authorized_target, capsys, monkeypatch, event, occurrence, action):
    approved = _approve(authorized_target, capsys)
    _interrupt_cli(authorized_target, "start", event, occurrence, capsys, monkeypatch)
    budget = _remaining(authorized_target, action)
    assert budget["components"]["safety_margin"] == 4 * 1024**3
    assert budget["peak_additional_bytes"] < approved["identity"]["required_bytes"]
    assert budget["direction"] == ("rollback" if action == "rollback" else "forward")
    before = {str(path): path.read_bytes() for path in authorized_target[0].rglob("*") if path.is_file()}
    with monkeypatch.context() as scoped:
        _limit_space(scoped, budget["peak_additional_bytes"] - 4096)
        with pytest.raises(SystemExit, match="Insufficient remaining"):
            _cli(authorized_target, action, capsys)
    capsys.readouterr()
    assert all(Path(path).read_bytes() == value for path, value in before.items())
    with monkeypatch.context() as scoped:
        _limit_space(scoped, budget["peak_additional_bytes"] + 4096, budget["additional_inode_bound"] + 1)
        result = _cli(authorized_target, action, capsys)
    assert result["outcome"] == action
    retained = authorization.read(authorized_target[0])
    assert retained["approval_id"] == approved["approval_id"]
    assert retained["identity_sha256"] == approved["identity_sha256"]
    assert retained["identity"]["operation"] == "authorized-cli"
    if action == "rollback":
        for reference, checksum in authorized_target[4].items():
            assert digest(authorized_target[0] / reference) == checksum


def test_repeated_capacity_interruption_and_rollback_continuation(authorized_target, capsys, monkeypatch):
    approved = _approve(authorized_target, capsys)
    _interrupt_cli(authorized_target, "start", "between-moves", 3, capsys, monkeypatch)
    _interrupt_cli(authorized_target, "resume", "metadata-replaced", 2, capsys, monkeypatch)
    assert _cli(authorized_target, "resume", capsys)["outcome"] == "resume"
    _interrupt_cli(authorized_target, "rollback", "rollback:between-moves", 3, capsys, monkeypatch)
    budget = _remaining(authorized_target, "rollback")
    with monkeypatch.context() as scoped:
        _limit_space(scoped, budget["peak_additional_bytes"] + 4096)
        assert _cli(authorized_target, "rollback", capsys)["outcome"] == "rollback"
    assert authorization.read(authorized_target[0])["approval_id"] == approved["approval_id"]


@pytest.mark.parametrize("action", ["resume", "rollback"])
def test_enospc_during_authorized_recovery(authorized_target, capsys, monkeypatch, action):
    from services import resource_rename
    _approve(authorized_target, capsys)
    _interrupt_cli(authorized_target, "start", "between-moves", 3, capsys, monkeypatch)
    def fail(*args):
        raise OSError(errno.ENOSPC, "injected filesystem ENOSPC")
    with monkeypatch.context() as scoped:
        scoped.setattr(resource_rename, "_rename", fail)
        with pytest.raises(SystemExit, match="ENOSPC|manual review"):
            _cli(authorized_target, action, capsys)
    capsys.readouterr()
    assert authorization.read(authorized_target[0])["state"] == ("running" if action == "resume" else "restore-required")
    if action == "resume":
        assert _cli(authorized_target, "resume", capsys)["outcome"] == "resume"
    else:
        for reference, checksum in authorized_target[4].items():
            if (authorized_target[0] / reference).exists():
                assert digest(authorized_target[0] / reference) == checksum


@pytest.mark.parametrize("defect", ["missing", "corrupt", "mapping", "graph-mapping", "event", "changed-file", "changed-mtime", "unexpected-temp"])
def test_capacity_refuses_incomplete_or_changed_evidence(authorized_target, capsys, monkeypatch, defect):
    _approve(authorized_target, capsys)
    _interrupt_cli(authorized_target, "start", "metadata-replaced", 1, capsys, monkeypatch)
    project = authorized_target[0]
    rename = project / access.CONTROL / "rename-authorized-cli.sqlite3"
    graph = project / access.CONTROL / "graph-authorized-cli.sqlite3"
    if defect == "missing":
        graph.rename(graph.with_suffix(".retained"))
    elif defect == "corrupt":
        graph.write_bytes(b"broken SQLite evidence")
    elif defect in ("mapping", "event"):
        with sqlite3.connect(rename) as database:
            database.execute("DELETE FROM moves WHERE source=(SELECT min(source) FROM moves)" if defect == "mapping" else "DELETE FROM events")
    elif defect == "graph-mapping":
        with sqlite3.connect(graph) as database:
            database.execute("DELETE FROM transforms WHERE number=0")
    elif defect in ("changed-file", "changed-mtime"):
        with sqlite3.connect(rename) as database:
            destination = database.execute("SELECT destination FROM moves WHERE state='done' LIMIT 1").fetchone()[0]
        path = project / destination
        if defect == "changed-file":
            path.write_bytes(b"unexpected changed bytes")
        else:
            os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1))
    else:
        (project / access.CONTROL / "graph-authorized-cli" / "unrecognized.tmp").write_bytes(b"unexpected retained bytes")
    before = {str(path): path.read_bytes() for path in project.rglob("*") if path.is_file()}
    for action in ("resume", "rollback"):
        with pytest.raises(SystemExit):
            _cli(authorized_target, action, capsys)
        capsys.readouterr()
        assert all(Path(path).read_bytes() == value for path, value in before.items())


@pytest.mark.parametrize("event", ["registry-prepared:synced", "index-built:synced"])
def test_sealed_builds_credit_allocation_before_installation(authorized_target, capsys, monkeypatch, event):
    _approve(authorized_target, capsys)
    _interrupt_cli(authorized_target, "start", event, 1, capsys, monkeypatch)
    budget = _remaining(authorized_target, "resume")
    assert budget["components"]["one_working_registry"] == 0
    assert budget["components"]["safety_margin"] == 4 * 1024**3
    with monkeypatch.context() as scoped:
        _limit_space(scoped, budget["peak_additional_bytes"] + 4096)
        assert _cli(authorized_target, "resume", capsys)["outcome"] == "resume"


@pytest.mark.parametrize("action", ["resume", "rollback"])
def test_remaining_capacity_requires_inode_headroom(authorized_target, capsys, monkeypatch, action):
    _approve(authorized_target, capsys)
    _interrupt_cli(authorized_target, "start", "between-moves", 3, capsys, monkeypatch)
    budget = _remaining(authorized_target, action)
    before = {str(path): path.read_bytes() for path in authorized_target[0].rglob("*") if path.is_file()}
    with monkeypatch.context() as scoped:
        _limit_space(scoped, budget["peak_additional_bytes"] + 4096, budget["additional_inode_bound"] - 1)
        with pytest.raises(SystemExit, match="Insufficient remaining"):
            _cli(authorized_target, action, capsys)
    assert all(Path(path).read_bytes() == payload for path, payload in before.items())


@pytest.mark.parametrize("boundary", ["maintenance-requested", "maintenance-owned", "approval-committed",
    "before-rename", "metadata-replaced", "registry-pair-committed", "index-staged", "graph-validation"])
def test_actual_cli_approval_interruption_and_same_operation_resume(authorized_target, capsys, monkeypatch, boundary):
    _approve(authorized_target, capsys)
    original = interface.dispatch
    def stop(event):
        if event == boundary:
            raise RuntimeError("authorization interruption at " + event)
    monkeypatch.setattr(interface, "dispatch", lambda args, **options: original(args, interrupt=stop, **options))
    with pytest.raises(SystemExit, match="interruption"):
        _cli(authorized_target, "start", capsys)
    capsys.readouterr()
    monkeypatch.setattr(interface, "dispatch", original)
    assert access.status(authorized_target[0])["state"] != "open"
    with pytest.raises(SystemExit):
        _cli(authorized_target, "resume", capsys, operation="another-operation")
    capsys.readouterr()
    assert _cli(authorized_target, "resume", capsys)["outcome"] == "resume"


@pytest.mark.parametrize("boundary", ["before-acceptance-recording", "acceptance-recorded", "before-release",
    "integrated:released", "release-state-published", "release-request-retired"])
def test_actual_cli_authorized_acceptance_release_interruptions(authorized_target, capsys, monkeypatch, boundary):
    _approve(authorized_target, capsys)
    _cli(authorized_target, "start", capsys)
    action = "accept" if "acceptance" in boundary else "release"
    if action == "release":
        _cli(authorized_target, "accept", capsys)
    original = interface.dispatch
    def stop(event):
        if event == boundary:
            raise RuntimeError("authorization interruption at " + event)
    monkeypatch.setattr(interface, "dispatch", lambda args, **options: original(args, interrupt=stop, **options))
    with pytest.raises(SystemExit, match="interruption"):
        _cli(authorized_target, action, capsys)
    capsys.readouterr()
    monkeypatch.setattr(interface, "dispatch", original)
    assert _cli(authorized_target, action, capsys)["state"] == ("accepted" if action == "accept" else "released")


def test_actual_cli_cancel_expiry_and_no_reissue(authorized_target, capsys, monkeypatch):
    record = _approve(authorized_target, capsys)
    monkeypatch.setattr(authorization.time, "time", lambda: record["expires_at"] + 1)
    assert authorization.inspect(authorized_target[0])["state"] == "expired"
    with pytest.raises(SystemExit, match="expired"):
        _cli(authorized_target, "start", capsys)
    capsys.readouterr()
    assert access.status(authorized_target[0])["state"] == "open"
    assert _cli(authorized_target, "cancel", capsys, reviewed=record["identity_sha256"])["state"] == "cancelled"
    with pytest.raises(SystemExit, match="already exists"):
        _approve(authorized_target, capsys)


@pytest.mark.parametrize("change", ["plan", "allocation", "recovery", "root", "record", "incomplete", "capacity"])
def test_changed_authorization_evidence_refuses_before_maintenance(authorized_target, capsys, monkeypatch, change):
    project, plan, checksum, allocation, _, backup = authorized_target
    _approve(authorized_target, capsys)
    if change in ("plan", "allocation"):
        path = plan if change == "plan" else allocation
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1))
    elif change == "recovery":
        (backup / backup_mirror.MARKER).write_bytes(b"different recovery")
    elif change in ("record", "incomplete"):
        path = project / access.CONTROL / authorization.RECORD
        if change == "record":
            path.write_text("{}")
            path.chmod(0o600)
        else:
            value = authorization.read(project)
            del value["identity"]["plan_stat"]
            authorization._write(project, value)
    elif change == "root":
        key = access._identity_key_for_authorization(project)
        access._authorization_fixtures.remove(key)
        monkeypatch.setattr(authorization, "test_target", lambda path: False)
    else:
        original = authorization._fresh_identity
        def no_capacity(identity):
            ready = original(identity)
            return {**ready, "available_bytes": 0, "available_inodes": 0}
        monkeypatch.setattr(authorization, "_fresh_identity", no_capacity)
    with pytest.raises(SystemExit):
        _cli(authorized_target, "start", capsys)
    assert access.status(project)["state"] == "open"
    assert not any((project / access.CONTROL).glob("*.sqlite3"))


@pytest.mark.parametrize("boundary", ["before-approval-commit", "approval-committed"])
def test_approval_commit_interruption_never_starts(authorized_target, capsys, boundary):
    project, plan, checksum = authorized_target[:3]
    proposed = authorization.proposal(project, plan, checksum, "commit-test")
    def stop(event):
        if event == boundary:
            raise RuntimeError("commit interruption")
    with pytest.raises(RuntimeError, match="commit interruption"):
        authorization.approve(project, plan, checksum, "commit-test", proposed["identity_sha256"],
                              confirmed=True, operator="test operator", interrupt=stop)
    assert access.status(project)["state"] == "open"
    record = authorization.read(project)
    assert (record is None) == (boundary == "before-approval-commit")
    assert not any((project / access.CONTROL).glob("*.sqlite3"))


def test_consumed_approval_remains_recoverable_after_unused_expiry(authorized_target, capsys, monkeypatch):
    record = _approve(authorized_target, capsys)
    project, plan, checksum = authorized_target[:3]
    def stop(event):
        if event == "before-rename":
            raise RuntimeError("interrupted")
    with pytest.raises(RuntimeError, match="interrupted"):
        authorization.execute(project, plan, checksum, "authorized-cli", "start", confirmed=True,
                              clients_stopped=True, interrupt=stop)
    monkeypatch.setattr(authorization.time, "time", lambda: record["expires_at"] + 1)
    assert authorization.inspect(project)["state"] == "running"
    assert _cli(authorized_target, "resume", capsys)["outcome"] == "resume"


def test_simultaneous_starts_and_owner_lifetime(authorized_target, capsys):
    _approve(authorized_target, capsys)
    project, plan, checksum = authorized_target[:3]
    entered, proceed = threading.Event(), threading.Event()
    failures = []
    def checkpoint(event):
        if event == "maintenance-owned":
            entered.set()
            assert proceed.wait(10)
        if event == "before-rename":
            raise RuntimeError("stop race after consumption")
    def first():
        try:
            authorization.execute(project, plan, checksum, "authorized-cli", "start", confirmed=True,
                                  clients_stopped=True, interrupt=checkpoint)
        except RuntimeError as error:
            failures.append(str(error))
    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert entered.wait(10)
        with pytest.raises(access.MaintenanceError):
            authorization.execute(project, plan, checksum, "authorized-cli", "start", confirmed=True,
                                  clients_stopped=True)
    finally:
        proceed.set()
        thread.join(15)
    assert not thread.is_alive()
    assert failures == ["stop race after consumption"]
    assert authorization.read(project)["state"] == "running"
    with access.maintenance(project, "authorized-cli", approved=True) as owner:
        owner._migration_authorization = authorization.read(project), interface._stat(project / access.CONTROL / authorization.RECORD), "resume"
        with pytest.raises(access.IncompleteCoverage):
            owner.require_migration()
    with pytest.raises(access.MaintenanceError):
        owner.require_migration()


def test_exact_review_confirmation_and_plan_are_required(authorized_target, capsys):
    prepared = _cli(authorized_target, "prepare", capsys)
    for reviewed, confirm in (("0" * 64, True), (prepared["identity_sha256"], False)):
        with pytest.raises(SystemExit):
            _cli(authorized_target, "approve", capsys, reviewed=reviewed, confirm=confirm)
        capsys.readouterr()
        assert not (authorized_target[0] / access.CONTROL).exists()
    _approve(authorized_target, capsys)
    project, plan, checksum = authorized_target[:3]
    with pytest.raises(access.MaintenanceError, match="differs"):
        authorization.execute(project, plan, "0" * 64, "authorized-cli", "start", confirmed=True, clients_stopped=True)
    assert access.status(project)["state"] == "open"


def test_authorized_rollback_terminal_and_original_integrity(authorized_target, capsys):
    _approve(authorized_target, capsys)
    _cli(authorized_target, "start", capsys)
    result = _cli(authorized_target, "rollback", capsys)
    assert result["state"] == "rolled-back"
    assert authorization.read(authorized_target[0])["state"] == "rolled-back"
    assert access.status(authorized_target[0])["state"] == "maintenance"
    for reference, checksum in authorized_target[4].items():
        assert digest(authorized_target[0] / reference) == checksum


def test_private_approval_record_survives_read_and_cancellation(monkeypatch):
    with access.temporary_rename_project() as project:
        identity = {"project": str(project), "device": project.stat().st_dev, "inode": project.stat().st_ino,
                    "operation": "approved-operation", "plan": "plan.sqlite3", "plan_sha256": "0" * 64,
                    "plan_revision": "2B.3d", "plan_stat": [0] * 5, "identity_allocation": "allocation.sqlite3",
                    "identity_allocation_sha256": "0" * 64, "allocation_stat": [0] * 5,
                    "recovery_source": {"fixture": True}, "fixture_only": True, "operations": {},
                    "required_bytes": 0, "required_inodes": 0}
        monkeypatch.setattr(authorization, "proposal", lambda *args: {"proposed_identity": identity,
            "identity_sha256": authorization.seal(identity), "blocking_conditions": []})
        monkeypatch.setattr(authorization, "activated", lambda value: value == identity)
        monkeypatch.setattr(authorization, "_fresh_identity", lambda value: {"available_bytes": 1048576, "available_inodes": 256})
        monkeypatch.setattr(authorization, "_bounded_evidence", lambda value: None)
        record = authorization.approve(project, None, None, "approved-operation", authorization.seal(identity),
                                       confirmed=True, operator="test operator")
        assert record == authorization.read(project)
        assert access.status(project)["state"] == "open"
        assert authorization.inspect(project)["state"] == "approved"
        authorization.cancel(project, "approved-operation", authorization.seal(identity), confirmed=True)
        assert authorization.inspect(project)["state"] == "cancelled"
        with pytest.raises(access.MaintenanceError, match="already exists"):
            authorization.approve(project, None, None, "approved-operation", authorization.seal(identity),
                                  confirmed=True, operator="test operator")


def test_production_activation_is_inactive_without_writes(monkeypatch):
    identity = {"project": str(authorization.PRODUCTION)}
    assert authorization.activated(identity) is False
    monkeypatch.setattr(authorization, "proposal", lambda *args: {"proposed_identity": identity})
    with pytest.raises(access.IncompleteCoverage, match="inactive"):
        authorization.approve(authorization.PRODUCTION, None, None, "production", "0" * 64,
                              confirmed=True, operator="test operator")


def test_authorization_test_root_has_no_legacy_or_general_mutation_capability():
    with access.temporary_authorization_project() as project:
        assert authorization.test_target(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "not-approved", approved=True) as owner:
            with pytest.raises(access.IncompleteCoverage):
                owner.require_fixture_renames()
            with pytest.raises(access.IncompleteCoverage):
                owner.require_migration()
            with pytest.raises(access.IncompleteCoverage):
                owner.require_resource_mutation()