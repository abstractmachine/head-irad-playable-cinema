import json
import os
import sqlite3
from pathlib import Path

import pytest

from data.resource_storage import digest
from services import resource_integration as integrated
from services.resource_migration import _file
from test_resource_graph import _engraving_fixture, _member_fixture
from tool import project_access as access


def _fixture(project):
    plan, _, preparation, records = _engraving_fixture(project, base_factory=_member_fixture)
    with sqlite3.connect(plan) as database:
        for record, instance, legacy, media in records:
            path = project / "media" / record["media_type"] / media["filename"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture source media")
            _file(database, project, str(path.relative_to(project)), None, "Retain", "source-media", hash_bytes=True)
    return plan, digest(plan), preparation, records


def _arguments(checksum):
    return {"approved_digest": checksum, "plan_identity": "2B.3d", "approved": True, "clients_stopped": True}


def test_integrated_owners_close_relocate_and_install():
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _fixture(project)
        before = preparation.read_bytes()
        with access.operation(project):
            pass
        result = integrated.run_fixture_migration(project, plan, **_arguments(checksum))
        assert result["phase"] == "graph-installed" and result["graph_accepted"]
        assert not result["maintenance_released"]
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(project):
                pass
        assert preparation.read_bytes() == before
        assert all(not legacy.exists() for record, instance, legacy, media in records)
        receipt = json.loads((project / access.CONTROL / integrated.RECEIPT).read_text())
        assert receipt["fixture_only"] and receipt["plan_sha256"] == checksum


def test_complete_acceptance_and_explicit_canonical_reopening():
    from services.resource_resolution import resolve_silhouette
    from data.annotate import get_annotation_json_path
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _fixture(project)
        arguments = _arguments(checksum)
        with access.operation(project):
            assert get_annotation_json_path(project, records[0][3]["filename"], "movie").is_file()
        integrated.run_fixture_migration(project, plan, **arguments)
        with pytest.raises(ValueError, match="acceptance"):
            integrated.release_fixture_migration(project, plan, **arguments)
        assert integrated.accept_fixture_migration(project, plan, **arguments)["state"] == "accepted"
        with pytest.raises(access.MaintenanceClosed):
            resolve_silhouette(project, records[0][1])
        assert integrated.release_fixture_migration(project, plan, **arguments)["maintenance_released"]
        for record, instance, legacy, media in records:
            assert resolve_silhouette(project, instance)["layout"] == "media-first-v1"
            assert get_annotation_json_path(project, media["filename"], record["media_type"]).is_file()
            assert not legacy.exists()
        assert access.status(project)["state"] == "open"
        assert integrated.release_fixture_migration(project, plan, **arguments)["maintenance_released"]


@pytest.mark.parametrize("boundary", ["before-enrollment", "maintenance-requested", "active-operation-draining", "maintenance-owned", "integrated:relocating", "before-rename", "between-moves", "completion:journal-synced", "metadata-replaced", "registry-snapshot-created", "registry-pair-committed", "index-staged", "graph-validation", "integrated:graph-installed"])
def test_phase_interruption_and_verified_resume(boundary):
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        def stop(phase):
            if phase == boundary:
                raise RuntimeError("integrated fixture interrupted")
        with pytest.raises(RuntimeError):
            integrated.run_fixture_migration(project, plan, **_arguments(checksum), interrupt=stop)
        if boundary != "before-enrollment":
            with pytest.raises(access.MaintenanceError):
                with access.operation(project):
                    pass
        assert integrated.recover_fixture_migration(project, plan, **_arguments(checksum))["outcome"] == "resume"


@pytest.mark.parametrize("boundary", ["before-acceptance-recording", "acceptance-recorded", "integrated:release-intent", "before-release", "integrated:released", "release-state-published", "release-request-retired"])
def test_acceptance_release_interruption_is_closed_or_accepted(boundary):
    from services.resource_resolution import resolve_silhouette
    with access.temporary_rename_project() as project:
        plan, checksum, _, records = _fixture(project)
        arguments = _arguments(checksum)
        integrated.run_fixture_migration(project, plan, **arguments)
        def stop(phase):
            if phase == boundary:
                raise RuntimeError("acceptance/release interrupted")
        if boundary in ("before-acceptance-recording", "acceptance-recorded"):
            with pytest.raises(RuntimeError):
                integrated.accept_fixture_migration(project, plan, **arguments, interrupt=stop)
            integrated.accept_fixture_migration(project, plan, **arguments)
        else:
            integrated.accept_fixture_migration(project, plan, **arguments)
            with pytest.raises(RuntimeError):
                integrated.release_fixture_migration(project, plan, **arguments, interrupt=stop)
        state = access.status(project)
        if state["state"] == "open":
            assert resolve_silhouette(project, records[0][1])["layout"] == "media-first-v1"
        else:
            with pytest.raises(access.MaintenanceError):
                resolve_silhouette(project, records[0][1])
        assert integrated.release_fixture_migration(project, plan, **arguments)["maintenance_released"]


@pytest.mark.parametrize("boundary", ["integrated:relocated", "metadata-replaced", "registry-pair-committed", "index-staged", "integrated:graph-installed", "accepted", "released"])
def test_complete_rollback_restores_all_original_bytes(boundary):
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _fixture(project)
        original = {str(path.relative_to(project)): path.read_bytes() for path in project.rglob('*') if path.is_file()}
        arguments = _arguments(checksum)
        if boundary in ("accepted", "released"):
            integrated.run_fixture_migration(project, plan, **arguments)
            integrated.accept_fixture_migration(project, plan, **arguments)
            if boundary == "released":
                integrated.release_fixture_migration(project, plan, **arguments)
        else:
            def stop(phase):
                if phase == boundary:
                    raise RuntimeError("rollback qualification boundary")
            with pytest.raises(RuntimeError):
                integrated.run_fixture_migration(project, plan, **arguments, interrupt=stop)
        result = integrated.rollback_fixture_migration(project, plan, **arguments)
        assert result["outcome"] == "rollback", result
        assert all((project / reference).read_bytes() == data for reference, data in original.items())
        assert integrated.rollback_fixture_migration(project, plan, **arguments)["state"] == "rolled-back"
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(project):
                pass


@pytest.mark.parametrize("defect", ["missing-receipt", "corrupt-receipt", "rename-journal", "graph-journal", "resource", "index", "context", "publication", "missing-plan", "request-deletion"])
def test_release_refuses_changed_or_missing_evidence(defect):
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        arguments = _arguments(checksum)
        integrated.run_fixture_migration(project, plan, **arguments)
        integrated.accept_fixture_migration(project, plan, **arguments)
        control = project / access.CONTROL
        if defect == "missing-receipt":
            (control / integrated.RECEIPT).unlink()
        elif defect == "corrupt-receipt":
            (control / integrated.RECEIPT).write_text('{}')
        elif defect.endswith("journal"):
            with (control / (defect.split('-')[0] + '-integrated.sqlite3')).open('ab') as stream:
                stream.write(b"changed journal evidence")
        elif defect == "resource":
            with sqlite3.connect(plan) as database:
                target = database.execute("SELECT destination FROM files WHERE disposition='Move' LIMIT 1").fetchone()[0]
            (project / target).write_bytes(b"independent resource change")
        elif defect == "index":
            (project / 'data/indexes/corpus_stats.json').unlink()
        elif defect == "context":
            (control / access.CANONICAL_CONTEXT).unlink()
        elif defect == "publication":
            with sqlite3.connect(project / 'data/indexes/resources.sqlite3') as database:
                database.execute("UPDATE publication_operations SET state='files_published'")
        elif defect == "missing-plan":
            plan.unlink()
        else:
            (control / 'request.json').unlink()
        with pytest.raises((ValueError, OSError, access.MaintenanceError)):
            integrated.release_fixture_migration(project, plan, **arguments)
        with pytest.raises(access.MaintenanceError):
            with access.operation(project):
                pass


@pytest.mark.parametrize("method", [integrated.run_fixture_migration, integrated.accept_fixture_migration, integrated.release_fixture_migration, integrated.rollback_fixture_migration])
def test_no_unrestricted_fixture_or_production_authorization(method, tmp_path):
    with pytest.raises(access.IncompleteCoverage):
        method(tmp_path, tmp_path / 'absent.sqlite3', **_arguments('0' * 64))
    assert not (tmp_path / access.CONTROL).exists()
    production = '/home/cowpoke/playable/dead-crossing'
    with pytest.raises(access.IncompleteCoverage):
        method(production, production + '/absent.sqlite3', **_arguments('0' * 64))


def test_substituted_project_cannot_release():
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        arguments = _arguments(checksum)
        integrated.run_fixture_migration(project, plan, **arguments)
        integrated.accept_fixture_migration(project, plan, **arguments)
        holding = project.with_name(project.name + '-substituted')
        project.rename(holding)
        project.mkdir()
        try:
            with pytest.raises(access.IncompleteCoverage):
                integrated.release_fixture_migration(project, plan, **arguments)
            assert not (project / access.CONTROL).exists()
        finally:
            project.rmdir()
            holding.rename(project)
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(project):
                pass


@pytest.mark.parametrize("boundary", ["context-retired", "metadata-uninstalled", "integrated:graph-reversed", "integrated:rename-rollback", "rollback:renamed", "integrated:rolled-back"])
def test_interrupted_integrated_rollback_repeats(boundary):
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        arguments = _arguments(checksum)
        integrated.run_fixture_migration(project, plan, **arguments)
        integrated.accept_fixture_migration(project, plan, **arguments)
        def stop(phase):
            if phase == boundary:
                raise RuntimeError("rollback interrupted")
        with pytest.raises(RuntimeError):
            integrated.rollback_fixture_migration(project, plan, **arguments, interrupt=stop)
        assert integrated.rollback_fixture_migration(project, plan, **arguments)["state"] == "rolled-back"


def test_rollback_conflict_requires_restore_without_overwrite():
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        arguments = _arguments(checksum)
        integrated.run_fixture_migration(project, plan, **arguments)
        with sqlite3.connect(plan) as database:
            source = database.execute("SELECT source FROM files WHERE disposition='Move' LIMIT 1").fetchone()[0]
        (project / source).write_bytes(b"independent conflict, must survive")
        result = integrated.recover_fixture_migration(project, plan, action='rollback', **arguments)
        assert result['outcome'] == 'restore-required', result
        assert (project / source).read_bytes() == b"independent conflict, must survive"
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(project):
                pass


def test_post_release_resource_change_blocks_rollback():
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        arguments = _arguments(checksum)
        integrated.run_fixture_migration(project, plan, **arguments)
        integrated.accept_fixture_migration(project, plan, **arguments)
        integrated.release_fixture_migration(project, plan, **arguments)
        with sqlite3.connect(plan) as database:
            destination = database.execute("SELECT destination FROM files WHERE disposition='Move' LIMIT 1").fetchone()[0]
        (project / destination).write_bytes(b'post-release change must not be discarded')
        result = integrated.recover_fixture_migration(project, plan, action='rollback', **arguments)
        assert result['outcome'] == 'restore-required', result
        assert (project / destination).read_bytes() == b'post-release change must not be discarded'
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(project):
                pass


@pytest.mark.parametrize("boundary", ["before-enrollment", "maintenance-requested", "between-moves", "metadata-replaced", "registry-snapshot-created", "index-staged", "acceptance-recorded", "release-state-published", "release-request-retired"])
def test_real_process_death_and_restart(boundary, tmp_path, monkeypatch):
    from services import maintenance_preflight
    from services.resource_resolution import resolve_silhouette
    processes = tmp_path / 'bounded-process-fixture'
    processes.mkdir()
    monkeypatch.setattr(maintenance_preflight, '_PROC_ROOT', processes)
    with access.temporary_rename_project() as project:
        plan, checksum, _, records = _fixture(project)
        arguments = _arguments(checksum)
        child = os.fork()
        if child == 0:
            try:
                def stop(phase):
                    if phase == boundary:
                        os._exit(73)
                integrated.run_fixture_migration(project, plan, **arguments, interrupt=stop)
                integrated.accept_fixture_migration(project, plan, **arguments, interrupt=stop)
                integrated.release_fixture_migration(project, plan, **arguments, interrupt=stop)
            finally:
                os._exit(91)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 73
        if boundary in ('acceptance-recorded', 'release-state-published', 'release-request-retired'):
            integrated.release_fixture_migration(project, plan, **arguments)
        else:
            assert integrated.recover_fixture_migration(project, plan, **arguments)['outcome'] == 'resume'
            integrated.accept_fixture_migration(project, plan, **arguments)
            integrated.release_fixture_migration(project, plan, **arguments)
        reader = os.fork()
        if reader == 0:
            try:
                assert resolve_silhouette(project, records[0][1])['layout'] == 'media-first-v1'
                os._exit(0)
            except BaseException:
                os._exit(92)
        _, status = os.waitpid(reader, 0)
        assert os.waitstatus_to_exitcode(status) == 0


def test_live_operation_lease_draining_and_preflight(tmp_path, monkeypatch):
    from services import maintenance_preflight
    processes = tmp_path / 'bounded-process-fixture'
    processes.mkdir()
    monkeypatch.setattr(maintenance_preflight, '_PROC_ROOT', processes)
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        ready_read, ready_write = os.pipe()
        stop_read, stop_write = os.pipe()
        child = os.fork()
        if child == 0:
            try:
                with access.session(project):
                    os.write(ready_write, b'R')
                    os.read(stop_read, 1)
            finally:
                os._exit(0)
        try:
            assert os.read(ready_read, 1) == b'R'
            with pytest.raises(access.MaintenanceBusy):
                integrated.run_fixture_migration(project, plan, **_arguments(checksum))
            assert access.status(project)['state'] == 'maintenance-requested'
            with pytest.raises(access.MaintenanceClosed):
                with access.operation(project):
                    pass
        finally:
            os.write(stop_write, b'S')
            os.waitpid(child, 0)
            for descriptor in (ready_read, ready_write, stop_read, stop_write):
                os.close(descriptor)
        assert integrated.run_fixture_migration(project, plan, **_arguments(checksum))['graph_accepted']


@pytest.mark.parametrize("boundary", ["integrated:released", "release-state-published"])
def test_late_release_mutation_remains_closed(boundary):
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        arguments = _arguments(checksum)
        integrated.run_fixture_migration(project, plan, **arguments)
        integrated.accept_fixture_migration(project, plan, **arguments)
        with sqlite3.connect(plan) as database:
            target = database.execute("SELECT destination FROM files WHERE disposition='Move' LIMIT 1").fetchone()[0]
        def change(phase):
            if phase == boundary:
                (project / target).write_bytes(b'changed just before reopening')
        with pytest.raises(ValueError):
            integrated.release_fixture_migration(project, plan, **arguments, interrupt=change)
        with pytest.raises(access.MaintenanceError):
            with access.operation(project):
                pass


@pytest.mark.parametrize("approval,clients", [(False, True), (True, False), (1, True), (True, 'yes')])
def test_explicit_operator_acknowledgement_before_mutation(approval, clients):
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        arguments = {**_arguments(checksum), 'approved': approval, 'clients_stopped': clients}
        with pytest.raises(access.MaintenanceError):
            integrated.run_fixture_migration(project, plan, **arguments)
        assert not (project / access.CONTROL).exists()


def test_deleting_request_and_forging_open_state_cannot_reopen():
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        integrated.run_fixture_migration(project, plan, **_arguments(checksum))
        with access._root(project) as (_project, root), access._control(root) as control:
            state = access._read(control, 'state.json')
            access._write(control, 'state.json', {**state, 'state': 'open'})
            os.unlink('request.json', dir_fd=control)
        with pytest.raises(access.MaintenanceError):
            with access.operation(project):
                pass


@pytest.mark.parametrize('phase', ['unknown-phase', 'released'])
def test_inconsistent_durable_phase_is_not_resumed(phase):
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        arguments = _arguments(checksum)
        integrated.run_fixture_migration(project, plan, **arguments)
        with access._root(project) as (_project, root), access._control(root) as control:
            record = access._read(control, integrated.RECEIPT)
            access._write(control, integrated.RECEIPT, {**record, 'phase': phase})
        result = integrated.recover_fixture_migration(project, plan, **arguments)
        assert result['outcome'] == 'restore-required'
        assert 'phase' in result['reason']
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(project):
                pass


@pytest.mark.parametrize("scenario", ['release', 'rollback', 'unsealed-resume'])
def test_integrated_storage_peak_and_retention_lifetimes(scenario, monkeypatch):
    def allocation(project):
        measured = {'allocated_bytes': 0, 'logical_bytes': 0, 'inodes': 0}
        categories = {'controls': 0, 'registry': 0, 'owner_temporary': 0, 'sqlite_sidecars': 0, 'retained_unsealed': 0}
        for parent, directories, filenames in os.walk(project):
            for name in (*directories, *filenames):
                path = Path(parent) / name
                info = path.lstat()
                allocated = info.st_blocks * 512
                measured['allocated_bytes'] += allocated
                measured['logical_bytes'] += info.st_size
                measured['inodes'] += 1
                reference = str(path.relative_to(project))
                if reference.startswith(access.CONTROL + '/'):
                    categories['controls'] += allocated
                if name == 'resources.sqlite3' or name.startswith('.graph-'):
                    categories['registry'] += allocated
                if name.endswith('.tmp') or name.endswith('.new'):
                    categories['owner_temporary'] += allocated
                if name.endswith(('-journal', '-wal', '-shm')):
                    categories['sqlite_sidecars'] += allocated
                if name.endswith('.unsealed'):
                    categories['retained_unsealed'] += allocated
        return measured, categories
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _fixture(project)
        baseline, _ = allocation(project)
        peak = dict(baseline)
        category_peaks = {}
        samples = 0
        def measure():
            nonlocal samples
            samples += 1
            current, categories = allocation(project)
            for key, value in current.items():
                peak[key] = max(peak[key], value)
            for key, value in categories.items():
                category_peaks[key] = max(category_peaks.get(key, 0), value)
        original_sync = os.fsync
        def synced(descriptor):
            result = original_sync(descriptor)
            measure()
            return result
        monkeypatch.setattr(os, 'fsync', synced)
        arguments = _arguments(checksum)
        if scenario == 'unsealed-resume':
            def stop(phase):
                measure()
                if phase == 'index-staged':
                    raise RuntimeError('storage retention qualification')
            with pytest.raises(RuntimeError):
                integrated.run_fixture_migration(project, plan, **arguments, interrupt=stop)
        integrated.run_fixture_migration(project, plan, **arguments, interrupt=lambda phase: measure())
        integrated.accept_fixture_migration(project, plan, **arguments, interrupt=lambda phase: measure())
        if scenario == 'rollback':
            assert integrated.rollback_fixture_migration(project, plan, **arguments, interrupt=lambda phase: measure())['state'] == 'rolled-back'
        else:
            assert integrated.release_fixture_migration(project, plan, **arguments, interrupt=lambda phase: measure())['maintenance_released']
        final, _ = allocation(project)
        assert peak['allocated_bytes'] >= final['allocated_bytes'] >= baseline['allocated_bytes']
        print('TASK4_STORAGE ' + json.dumps({'scenario': scenario, 'baseline': baseline, 'peak': peak, 'final': final, 'growth': {key: peak[key] - baseline[key] for key in peak}, 'category_peaks_nonadditive': category_peaks, 'samples': samples}, sort_keys=True))