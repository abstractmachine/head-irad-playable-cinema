import shutil
import json
import threading
import inspect
from argparse import Namespace
import os
from pathlib import Path
import subprocess
import sys
import asyncio
from unittest.mock import Mock
from importlib import import_module

import pytest

from tool import project_access as access


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    from tool import prefs

    monkeypatch.setattr(prefs, "_USER_FILE", tmp_path / "user-prefs.json")
    monkeypatch.delenv("CROSSING_PROJECT", raising=False)


def _closed(project):
    access.enroll(project, approved=True)
    with access.maintenance(project, "fixture", approved=True):
        pass


def test_legacy_admission_is_nested_and_read_only(tmp_path):
    with access.operation(tmp_path):
        with access.operation(tmp_path):
            assert list(tmp_path.iterdir()) == []
    assert access.status(tmp_path)["state"] == "legacy"


@pytest.mark.parametrize("name,arguments", [
    ("load_book", ("fixture",)),
    ("save_book", ("fixture", {})),
    ("list_books", ()),
    ("create_book", ("fixture",)),
    ("delete_book", ("fixture",)),
    ("import_pdf", ("fixture", "unused.pdf")),
])
def test_book_operations_refuse_before_io(tmp_path, name, arguments):
    from data import book

    _closed(tmp_path)
    with pytest.raises(access.MaintenanceClosed):
        getattr(book, name)(str(tmp_path), *arguments)
    assert not (tmp_path / "outputs").exists()


@pytest.mark.parametrize("module,names", [
    ("data.metadata", "set_metadata prune_metadata save_json_metadata upsert_json_record migrate_csv_to_json check_metadata_sync"),
    ("data.shotlist", "list_shotlists write_shotlist annotate_shot annotate_scene migrate_shotlist_fields"),
    ("data.annotate", "find_latest_prompt load_system_prompt load_user_prompt load_label_list_fields write_annotation_aggregate migrate_annotations_to_stable_ids reindex_annotations_for_merge reindex_annotations_for_split remove_file_annotations"),
    ("services.illustration_index", "invalidate_index rebuild_all query_facets query_field_counts query_shot_type_counts query_page resolve_record_destination query_untyped_records"),
    ("data.resource_locations", "project_file registered_instance current_location resolve_annotation annotation_context_token annotation_commitment annotation_artifacts"),
    ("generators.composition", "build_tableau"),
    ("generators.flipbook", "load_flipbook_data generate_flipbook_for_movie generate_flipbook_for_all_movies"),
    ("generators.mosaic", "mosaic_from_search_results build_shots_results build_scenes_results results_to_mosaic_items mosaic_pdf_from_shots mosaic_pdf_from_scenes mosaic_video_from_search_results export_frames_from_search_results"),
    ("generators.palette", "generate_palette_pdf_for_movie generate_palette_pdf_for_all_movies"),
    ("services.engraving_generate", "validate_models generate_engraving generate_openai_engraving"),
    ("services.engraving_generate_openai", "generate_engraving_openai"),
    ("services.engraving_local_pipeline", "convert_one"),
])
def test_direct_io_boundaries_refuse_before_validation(tmp_path, module, names):
    _closed(tmp_path)
    for name in names.split():
        function = getattr(import_module(module), name)
        arguments = {
            name: tmp_path if name in ("project", "project_path") else None
            for name, parameter in inspect.signature(function).parameters.items()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        }
        with pytest.raises(access.MaintenanceClosed):
            function(**arguments)


def test_enrollment_and_persistent_inspection_ownership(tmp_path):
    assert access.enroll(tmp_path, approved=True)["state"] == "open"
    with access.operation(tmp_path):
        pass
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        assert owner.inspect()["state"] == "maintenance"
        with pytest.raises(access.IncompleteCoverage):
            owner.require_resource_mutation()
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(tmp_path):
                pytest.fail("closed project admitted")
    assert access.status(tmp_path)["state"] == "maintenance"
    with pytest.raises(access.MaintenanceError):
        owner.inspect()
    with pytest.raises(access.MaintenanceError, match="release is unavailable"):
        access.release_maintenance(tmp_path)


def test_lazy_enumeration_created_before_closure_cannot_start(tmp_path):
    from data.resource_locations import annotation_files

    pending = annotation_files(tmp_path, "movie")
    _closed(tmp_path)
    with pytest.raises(access.MaintenanceClosed):
        next(pending)


def test_lazy_iteration_holds_lease_until_closed(tmp_path):
    @access.project_iterator
    def rows(project):
        yield "first"
        yield "second"

    access.enroll(tmp_path, approved=True)
    iterator = rows(tmp_path)
    assert next(iterator) == "first"
    errors = []

    def request():
        try:
            with access.maintenance(tmp_path, "fixture", approved=True):
                errors.append("exclusive")
        except access.MaintenanceBusy:
            errors.append("busy")

    worker = threading.Thread(target=request)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert errors == ["busy"]
    with pytest.raises(access.MaintenanceClosed):
        with access.operation(tmp_path):
            pytest.fail("paused iterator leaked authorization to its caller")
    iterator.close()
    with access.maintenance(tmp_path, "fixture", approved=True):
        pass


def test_saved_context_cannot_outlive_its_operation(tmp_path):
    from contextvars import copy_context

    access.enroll(tmp_path, approved=True)
    with access.operation(tmp_path):
        saved = copy_context()
    with access.maintenance(tmp_path, "fixture", approved=True):
        pass

    def read():
        with access.operation(tmp_path):
            pytest.fail("expired copied context authorized a new operation")

    with pytest.raises(access.MaintenanceClosed):
        saved.run(read)


def test_missing_enrolled_state_does_not_become_legacy(tmp_path):
    access.enroll(tmp_path, approved=True)
    (tmp_path / access.CONTROL / "state.json").unlink()
    with pytest.raises(access.MaintenanceError):
        with access.operation(tmp_path):
            pytest.fail("missing state admitted")


def test_original_io_exception_is_preserved(tmp_path):
    with pytest.raises(PermissionError, match="fixture"):
        with access.operation(tmp_path):
            raise PermissionError("fixture")


def test_removed_control_and_anchor_remain_enrolled(tmp_path):
    access.enroll(tmp_path, approved=True)
    shutil.rmtree(tmp_path / access.CONTROL)
    (tmp_path / access.ANCHOR).unlink()
    with pytest.raises(access.MaintenanceError, match="missing"):
        access.status(tmp_path)


def test_failed_request_write_still_closes_admission(tmp_path, monkeypatch):
    access.enroll(tmp_path, approved=True)
    original = access._write

    def fail_request(descriptor, name, value):
        if name == "request.json":
            raise OSError("fixture interrupted write")
        return original(descriptor, name, value)

    monkeypatch.setattr(access, "_write", fail_request)
    with pytest.raises(OSError, match="fixture"):
        with access.maintenance(tmp_path, "fixture", approved=True):
            pytest.fail("failed request entered")
    with pytest.raises(access.MaintenanceError):
        with access.operation(tmp_path):
            pytest.fail("failed request reopened")


def test_closed_preferences_refuse_parser_reads(tmp_path):
    from tool import prefs
    import cli

    prefs.set("path", str(tmp_path))
    _closed(tmp_path)
    with pytest.raises(access.MaintenanceClosed):
        cli.build_parser()


def test_cli_refuses_before_dispatch(tmp_path, monkeypatch):
    from tool import prefs
    import cli

    prefs.set("path", str(tmp_path))
    _closed(tmp_path)
    parser = Mock(side_effect=AssertionError("parser must not read project defaults"))
    monkeypatch.setattr(cli, "build_parser", parser)
    with pytest.raises(SystemExit, match="Project access refused"):
        cli.main()
    parser.assert_not_called()


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("existing_version", [False, True])
def test_cli_path_closed_target_preserves_preferences_and_files(tmp_path, monkeypatch, configured, existing_version):
    import cli
    from tool import prefs

    target = tmp_path / "target"
    target.mkdir()
    if configured:
        current = tmp_path / "current"
        current.mkdir()
        prefs.set("path", str(current))
    if existing_version:
        (target / "preferences").mkdir()
        (target / "preferences" / "version.txt").write_text("existing\n")
    _closed(target)
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    version_read = Mock(side_effect=AssertionError("closed target version read"))
    version_write = Mock(side_effect=AssertionError("closed target version write"))
    monkeypatch.setattr(cli, "_get_data_version", version_read)
    monkeypatch.setattr(cli, "_set_data_version", version_write)
    monkeypatch.setattr(sys, "argv", ["crossing", "tool", "path", str(target)])
    with pytest.raises(SystemExit, match="Project access refused"):
        cli.main()
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    version_read.assert_not_called()
    version_write.assert_not_called()


@pytest.mark.parametrize("version", [None, "existing"])
def test_cli_path_open_target_is_initialized_normally(tmp_path, monkeypatch, version):
    import cli
    from tool import prefs

    target = tmp_path / "target"
    target.mkdir()
    if version:
        (target / "preferences").mkdir()
        (target / "preferences" / "version.txt").write_text(version + "\n")
    monkeypatch.setattr(sys, "argv", ["crossing", "tool", "path", str(target / ".")])
    cli.main()
    assert prefs.get("path") == str(target)
    assert (target / "preferences" / "version.txt").read_text() == (version or cli._TOOL_VERSION) + "\n"
    assert access.status(target)["state"] == "legacy"
    with pytest.raises(access.MaintenanceError, match="explicitly enrolled"):
        with access.maintenance(target, "fixture", approved=True):
            pytest.fail("ordinary selection must not enroll a project")


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_cli_path_invalid_target_preserves_selection(tmp_path, monkeypatch, kind):
    import cli
    from tool import prefs

    target = tmp_path / "target"
    if kind == "file":
        target.write_text("not a directory")
    monkeypatch.setattr(sys, "argv", ["crossing", "tool", "path", str(target)])
    with pytest.raises(SystemExit, match="Project access refused"):
        cli.main()
    assert prefs.get("path") is None
    assert not prefs._USER_FILE.exists()


def test_cli_path_target_lease_spans_preference_update_and_version_io(tmp_path, monkeypatch):
    import cli
    from tool import prefs

    target = tmp_path / "target"
    target.mkdir()
    access.enroll(target, approved=True)
    original_set = prefs.set
    original_read = cli._get_data_version
    outcomes = []

    def request():
        try:
            with access.maintenance(target, "fixture", approved=True):
                outcomes.append("exclusive")
        except access.MaintenanceBusy:
            outcomes.append("busy")
        except BaseException as error:
            outcomes.append(error)

    def update(key, value):
        worker = threading.Thread(target=request)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        assert outcomes == ["busy"]
        original_set(key, value)

    def read(project):
        assert access.status(project)["state"] == "maintenance-requested"
        with access.operation(project):
            return original_read(project)

    monkeypatch.setattr(prefs, "set", update)
    monkeypatch.setattr(cli, "_get_data_version", read)
    monkeypatch.setattr(sys, "argv", ["crossing", "tool", "path", str(target)])
    cli.main()
    assert (target / "preferences" / "version.txt").read_text() == cli._TOOL_VERSION + "\n"
    with pytest.raises(SystemExit, match="Project access refused"):
        cli.main()
    with access.maintenance(target, "fixture", approved=True):
        pass


def _process_fixture(root, pid, arguments, cwd, parent=1):
    directory = root / str(pid)
    directory.mkdir(parents=True)
    fields = ["S", str(parent), *["0"] * 17, str(pid * 10)]
    (directory / "stat").write_text(f"{pid} (fixture) " + " ".join(fields))
    (directory / "cmdline").write_bytes(b"\0".join(argument.encode() for argument in arguments) + b"\0")
    (directory / "cwd").symlink_to(cwd, target_is_directory=True)
    return directory


@pytest.fixture
def preflight_proc(tmp_path, monkeypatch):
    from services import maintenance_preflight

    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(maintenance_preflight, "_PROC_ROOT", root)
    return root


def _user_manager_fixture(root, monkeypatch, *, readable=False):
    from services import maintenance_preflight

    directory = _process_fixture(root, 123, ["/usr/lib/systemd/systemd", "--user"], "/")
    path = directory / "stat"
    path.write_text(path.read_text().replace("(fixture)", "(systemd)"))
    (directory / "cgroup").write_text(
        f"0::/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/init.scope\n"
    )
    readlink = maintenance_preflight.os.readlink

    def inspect_link(path, *args, **kwargs):
        if Path(path) == directory / "cwd" and not readable:
            raise PermissionError("fixture daemon is not dumpable")
        return readlink(path, *args, **kwargs)

    monkeypatch.setattr(maintenance_preflight.os, "readlink", inspect_link)
    return directory


@pytest.mark.parametrize("readable", [False, True])
def test_preflight_identifies_unrelated_user_manager(preflight_proc, monkeypatch, tmp_path, readable):
    from services import maintenance_preflight

    _user_manager_fixture(preflight_proc, monkeypatch, readable=readable)
    result = maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True)
    assert result["checked_processes"] == 1
    assert result["mutation_authorized"] is False


@pytest.mark.parametrize("changed", ["arguments", "comm", "parent", "cgroup", "missing_cgroup", "unreadable_cgroup"])
def test_preflight_unreadable_manager_requires_independent_evidence(preflight_proc, monkeypatch, tmp_path, changed):
    from services import maintenance_preflight

    directory = _user_manager_fixture(preflight_proc, monkeypatch)
    if changed == "arguments":
        (directory / "cmdline").write_bytes(b"python3\0cli.py\0index\0")
    elif changed == "comm":
        path = directory / "stat"
        path.write_text(path.read_text().replace("(systemd)", "(python3)"))
    elif changed == "parent":
        path = directory / "stat"
        path.write_text(path.read_text().replace(") S 1 ", ") S 42 "))
    elif changed == "cgroup":
        (directory / "cgroup").write_text("0::/user.slice/app.slice\n")
    elif changed == "missing_cgroup":
        (directory / "cgroup").unlink()
    else:
        read = maintenance_preflight._read

        def denied(path):
            if path.name == "cgroup":
                raise PermissionError("unreadable service membership")
            return read(path)

        monkeypatch.setattr(maintenance_preflight, "_read", denied)
    with pytest.raises(access.MaintenanceError):
        maintenance_preflight._snapshot(())


@pytest.mark.parametrize("changed", ["pid_reuse", "exec", "comm_change", "service_move", "termination"])
def test_preflight_unreadable_manager_rechecks_identity(preflight_proc, monkeypatch, tmp_path, changed):
    from services import maintenance_preflight

    directory = _user_manager_fixture(preflight_proc, monkeypatch)
    read = maintenance_preflight._read
    counts = {}

    def changing(path):
        counts[path.name] = counts.get(path.name, 0) + 1
        if path.name == "stat" and counts[path.name] == 2:
            if changed == "termination":
                for entry in directory.iterdir():
                    entry.unlink()
                directory.rmdir()
                raise FileNotFoundError("process exited")
            if changed == "pid_reuse":
                return read(path).rsplit(b" ", 1)[0] + b" 99999"
            if changed == "comm_change":
                return read(path).replace(b"(systemd)", b"(python3)")
        if changed == "exec" and path.name == "cmdline" and counts[path.name] == 2:
            return b"python3\0cli.py\0index\0"
        if changed == "service_move" and path.name == "cgroup" and counts[path.name] == 2:
            return b"0::/other.service\n"
        return read(path)

    monkeypatch.setattr(maintenance_preflight, "_read", changing)
    if changed == "termination":
        assert maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True)["checked_processes"] == 0
    else:
        with pytest.raises(access.MaintenanceError, match="identity changed"):
            maintenance_preflight._snapshot(())


def test_preflight_operator_known_manager_still_blocks(preflight_proc, monkeypatch, tmp_path):
    from services import maintenance_preflight

    _user_manager_fixture(preflight_proc, monkeypatch)
    with pytest.raises(access.MaintenanceBusy, match="remain: 123"):
        maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True, known_pids=[123])


def test_preflight_unreadable_unknown_process_is_not_certified(preflight_proc, monkeypatch, tmp_path):
    from services import maintenance_preflight

    directory = _user_manager_fixture(preflight_proc, monkeypatch)
    (directory / "cmdline").write_bytes(b"/usr/bin/unknown-daemon\0")
    diagnostics = []
    processes = maintenance_preflight._snapshot((), diagnostics=diagnostics)
    assert diagnostics
    assert not processes[123].get("unrelated_user_manager")
    assert processes[123]["partial_inspection"]


def _sd_pam_fixture(root, monkeypatch):
    from services import maintenance_preflight

    parent = _user_manager_fixture(root, monkeypatch)
    child = _process_fixture(root, 124, ["(sd-pam)"], "/", parent=123)
    path = child / "stat"
    path.write_text(path.read_text().replace("(fixture)", "((sd-pam))"))
    (child / "cgroup").write_bytes((parent / "cgroup").read_bytes())
    readlink = maintenance_preflight.os.readlink

    def inspect_link(path, *args, **kwargs):
        if Path(path) == child / "cwd":
            raise PermissionError("fixture PAM helper is not dumpable")
        return readlink(path, *args, **kwargs)

    monkeypatch.setattr(maintenance_preflight.os, "readlink", inspect_link)
    return parent, child


def test_preflight_accepts_parent_verified_sd_pam(tmp_path, preflight_proc, monkeypatch):
    from services import maintenance_preflight

    _sd_pam_fixture(preflight_proc, monkeypatch)
    result = maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True)
    assert result["checked_processes"] == 2
    assert result["mutation_authorized"] is False


@pytest.mark.parametrize("changed", ["parent_argv", "parent_comm", "parent_group", "parent_missing",
                                     "parent_newer", "parent_uid", "child_uid", "child_argv", "child_comm", "child_group"])
def test_preflight_sd_pam_requires_verified_relationship(tmp_path, preflight_proc, monkeypatch, changed):
    from services import maintenance_preflight

    parent, child = _sd_pam_fixture(preflight_proc, monkeypatch)
    if changed == "parent_missing":
        for path in parent.iterdir():
            path.unlink()
        parent.rmdir()
    elif changed in ("parent_uid", "child_uid"):
        original = Path.stat
        target = parent if changed == "parent_uid" else child

        def changed_stat(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == target:
                from types import SimpleNamespace
                return SimpleNamespace(st_uid=os.getuid() + 1)
            return result

        monkeypatch.setattr(Path, "stat", changed_stat)
    else:
        target = parent if changed.startswith("parent_") else child
        if changed.endswith("argv"):
            (target / "cmdline").write_bytes(b"python3\0cli.py\0index\0")
        elif changed.endswith("comm"):
            path = target / "stat"
            path.write_text(path.read_text().replace("(systemd)", "(python3)").replace("((sd-pam))", "(python3)"))
        elif changed.endswith("group"):
            (target / "cgroup").write_text("0::/other.service\n")
        else:
            path = parent / "stat"
            path.write_text(path.read_text().rsplit(" ", 1)[0] + " 99999")
    with pytest.raises(access.MaintenanceError):
        maintenance_preflight._snapshot((124,))


@pytest.mark.parametrize("target", ["parent", "child"])
@pytest.mark.parametrize("record", ["stat", "cmdline", "cgroup"])
@pytest.mark.parametrize("failure", ["missing", "unreadable"])
def test_preflight_sd_pam_rejects_missing_or_unreadable_evidence(tmp_path, preflight_proc, monkeypatch, target, record, failure):
    from services import maintenance_preflight

    parent, child = _sd_pam_fixture(preflight_proc, monkeypatch)
    path = (parent if target == "parent" else child) / record
    read = maintenance_preflight._read

    def denied(source):
        if source == path:
            if failure == "missing":
                raise FileNotFoundError("required evidence missing")
            raise PermissionError("required evidence denied")
        return read(source)

    monkeypatch.setattr(maintenance_preflight, "_read", denied)
    with pytest.raises(access.MaintenanceError):
        maintenance_preflight._snapshot(())


@pytest.mark.parametrize("target", ["parent", "child"])
@pytest.mark.parametrize("record", ["stat", "cmdline", "cgroup"])
def test_preflight_sd_pam_rechecks_parent_and_child(tmp_path, preflight_proc, monkeypatch, target, record):
    from services import maintenance_preflight

    parent, child = _sd_pam_fixture(preflight_proc, monkeypatch)
    read = maintenance_preflight._read
    calls = []
    path = (parent if target == "parent" else child) / record

    def changing(source):
        value = read(source)
        if source == path:
            calls.append(source)
            if len(calls) >= 2:
                if record == "stat":
                    return value.rsplit(b" ", 1)[0] + b" 99999"
                if record == "cmdline":
                    return b"python3\0cli.py\0index\0"
                return b"0::/other.service\n"
        return value

    monkeypatch.setattr(maintenance_preflight, "_read", changing)
    with pytest.raises(access.MaintenanceError):
        maintenance_preflight._snapshot(())


def test_preflight_operator_known_sd_pam_still_blocks(tmp_path, preflight_proc, monkeypatch):
    from services import maintenance_preflight

    _sd_pam_fixture(preflight_proc, monkeypatch)
    with pytest.raises(access.MaintenanceBusy, match="124"):
        maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True, known_pids=[124])


@pytest.mark.parametrize("crossing_active", [False, True])
def test_single_operator_unreadable_infrastructure_is_diagnostic(
    tmp_path, preflight_proc, monkeypatch, crossing_active,
):
    from services import maintenance_preflight

    infrastructure = _process_fixture(preflight_proc, 120, ["/usr/bin/ssh-agent", "-D"], "/")
    readlink = maintenance_preflight.os.readlink

    def denied(path, *args, **kwargs):
        if Path(path) == infrastructure / "cwd":
            raise PermissionError("ordinary SSH infrastructure")
        return readlink(path, *args, **kwargs)

    monkeypatch.setattr(maintenance_preflight.os, "readlink", denied)
    if crossing_active:
        _process_fixture(preflight_proc, 124, ["crossing", "index", "illustration"], tmp_path)
        with pytest.raises(access.MaintenanceBusy, match="124"):
            maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True)
    else:
        result = maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True)
        assert result["survey_complete"] is False
        assert result["diagnostics"]
        assert result["mutation_authorized"] is False


def test_single_operator_missing_survey_is_diagnostic(tmp_path, preflight_proc):
    from services import maintenance_preflight

    preflight_proc.rmdir()
    result = maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True)
    assert result["survey_complete"] is False
    assert result["diagnostics"]
    assert result["mutation_authorized"] is False


@pytest.mark.parametrize("known_job", [False, True])
def test_single_operator_unreadable_command_information(tmp_path, preflight_proc, monkeypatch, known_job):
    from services import maintenance_preflight

    _process_fixture(preflight_proc, 123, ["unknown"], "/")
    read = maintenance_preflight._read

    def denied(path):
        if path.name == "cmdline":
            raise PermissionError("command is unavailable")
        return read(path)

    monkeypatch.setattr(maintenance_preflight, "_read", denied)
    if known_job:
        with pytest.raises(access.MaintenanceBusy, match="123"):
            maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True, known_pids=[123])
    else:
        assert maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True)["diagnostics"]


def test_single_operator_known_command_blocks_with_unreadable_cwd(tmp_path, preflight_proc, monkeypatch):
    from services import maintenance_preflight

    process = _process_fixture(preflight_proc, 123, ["crossing", "engraving", "local-batch"], tmp_path)
    readlink = maintenance_preflight.os.readlink

    def denied(path, *args, **kwargs):
        if Path(path) == process / "cwd":
            raise PermissionError("cwd is unavailable")
        return readlink(path, *args, **kwargs)

    monkeypatch.setattr(maintenance_preflight.os, "readlink", denied)
    with pytest.raises(access.MaintenanceBusy, match="123"):
        maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True)


@pytest.mark.parametrize("job_present", [False, True])
def test_single_operator_explicit_job_after_truncated_survey(
    tmp_path, preflight_proc, monkeypatch, job_present,
):
    from services import maintenance_preflight

    _process_fixture(preflight_proc, 120, ["unrelated"], "/")
    _process_fixture(preflight_proc, 121, ["unrelated"], "/")
    if job_present:
        _process_fixture(preflight_proc, 123, ["custom-job"], "/")
    monkeypatch.setattr(maintenance_preflight, "_MAX_PROCESSES", 1)
    if job_present:
        with pytest.raises(access.MaintenanceBusy, match="123"):
            maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True, known_pids=[123])
    else:
        result = maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True, known_pids=[123])
        assert result["diagnostics"]
        assert result["mutation_authorized"] is False


def test_single_operator_diagnostics_do_not_supply_exclusive_owner(preflight_proc):
    from services import maintenance_preflight

    preflight_proc.rmdir()
    with pytest.raises(access.MaintenanceError, match="exclusive inspection ownership"):
        maintenance_preflight.check_quiescence(None, clients_stopped=True)


def test_preflight_clean_shutdown_is_inspection_only(tmp_path, preflight_proc):
    from services.maintenance_preflight import check_quiescence

    access.enroll(tmp_path, approved=True)
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        result = check_quiescence(owner, clients_stopped=True)
        assert result["checked_processes"] == 0
        assert result["mutation_authorized"] is False
        with pytest.raises(access.IncompleteCoverage):
            owner.require_resource_mutation()
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(tmp_path):
                pytest.fail("preflight reopened ordinary access")
    with pytest.raises(access.MaintenanceError, match="release is unavailable"):
        access.release_maintenance(tmp_path)
    with pytest.raises(access.MaintenanceError, match="no longer owned"):
        check_quiescence(owner, clients_stopped=True)


@pytest.mark.parametrize("acknowledgement", [False, None, 1, "yes"])
def test_preflight_requires_explicit_acknowledgement(tmp_path, preflight_proc, acknowledgement):
    from services.maintenance_preflight import check_quiescence

    access.enroll(tmp_path, approved=True)
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        with pytest.raises(access.MaintenanceError, match="acknowledgement"):
            check_quiescence(owner, clients_stopped=acknowledgement)


@pytest.mark.parametrize("arguments", [
    ["crossing", "annotate"],
    ["crossing", "index", "illustration"],
    ["crossing", "engraving", "local-batch"],
    ["python3", "/old/crossing/mcp_server.py"],
    ["python3", "-m", "mcp_server.mcp_server"],
    ["python3", "/old/shot_visualizer.py"],
    ["python3", "worker.py"],
    ["ffmpeg", "-i", "fixture.mp4"],
])
def test_preflight_incomplete_shutdown_refuses_known_clients(tmp_path, preflight_proc, arguments):
    from services.maintenance_preflight import check_quiescence

    _process_fixture(preflight_proc, 123, arguments, tmp_path)
    access.enroll(tmp_path, approved=True)
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        with pytest.raises(access.MaintenanceBusy, match="remain: 123"):
            check_quiescence(owner, clients_stopped=True)
        with pytest.raises(access.IncompleteCoverage):
            owner.require_resource_mutation()


def test_preflight_operator_known_jobs_and_descendants(tmp_path, preflight_proc):
    from services.maintenance_preflight import check_quiescence

    _process_fixture(preflight_proc, 123, ["custom-job"], "/")
    _process_fixture(preflight_proc, 124, ["decoder"], "/", parent=123)
    access.enroll(tmp_path, approved=True)
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        with pytest.raises(access.MaintenanceBusy, match="123, 124"):
            check_quiescence(owner, clients_stopped=True, known_pids=[123])


@pytest.fixture(params=["entrypoint", "script"])
def launcher_form(request):
    return request.param


def test_preflight_current_uv_launcher_is_not_an_independent_client(tmp_path, preflight_proc, launcher_form):
    from services import maintenance_preflight

    _uv_controller_fixture(preflight_proc, tmp_path, launcher_form)
    result = maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True)
    assert result["mutation_authorized"] is False


def _uv_controller_fixture(root, project, launcher_form):
    arguments = ["migration", "start", "--project", str(project)]
    command = ["crossing"] if launcher_form == "entrypoint" else ["python", "cli.py"]
    target = "/fixture/bin/crossing" if launcher_form == "entrypoint" else "cli.py"
    parent = _process_fixture(root, 123, ["uv", "run", *command, *arguments], project)
    path = parent / "stat"
    path.write_text(path.read_text().rsplit(" ", 1)[0] + " 1")
    controller = _process_fixture(root, os.getpid(), ["python3", target, *arguments],
                                  project, parent=123)
    return parent, controller


@pytest.mark.parametrize("arguments,parent", [
    (["crossing", "index", "illustration"], 1),
    (["uv", "run", "crossing", "migration", "start"], 1),
    (["python3", "-m", "mcp_server.mcp_server"], 1),
    (["python3", "worker.py"], os.getpid()),
    (["crossing", "engraving", "local-batch"], 123),
])
def test_preflight_current_uv_launcher_preserves_independent_jobs(tmp_path, preflight_proc, arguments, parent, launcher_form):
    from services import maintenance_preflight

    _uv_controller_fixture(preflight_proc, tmp_path, launcher_form)
    _process_fixture(preflight_proc, 124, arguments, tmp_path, parent=parent)
    with pytest.raises(access.MaintenanceBusy, match="remain: 124"):
        maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True)


def test_preflight_current_uv_launcher_explicit_operator_pid_still_blocks(tmp_path, preflight_proc, launcher_form):
    from services import maintenance_preflight

    _uv_controller_fixture(preflight_proc, tmp_path, launcher_form)
    with pytest.raises(access.MaintenanceBusy, match="remain: 123"):
        maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True, known_pids=[123])


@pytest.mark.parametrize("changed", ["shell", "arguments", "target", "started", "relationship", "python"])
def test_preflight_current_uv_launcher_requires_exact_invocation(tmp_path, preflight_proc, changed, launcher_form):
    from services import maintenance_preflight

    parent, controller = _uv_controller_fixture(preflight_proc, tmp_path, launcher_form)
    if changed == "started":
        path = parent / "stat"
        path.write_text(path.read_text().rsplit(" ", 1)[0] + " " + str(os.getpid() * 10 + 1))
    elif changed == "arguments":
        (controller / "cmdline").write_bytes(b"python3\0/fixture/bin/crossing\0migration\0resume\0")
    elif changed == "target":
        target = b"crossing\0" if launcher_form == "entrypoint" else b"cli.py\0"
        (parent / "cmdline").write_bytes((parent / "cmdline").read_bytes().replace(target, b"/other/bin/" + target))
    elif changed == "relationship":
        path = controller / "stat"
        path.write_text(path.read_text().replace(") S 123 ", ") S 1 "))
    elif changed == "python":
        (controller / "cmdline").write_bytes((controller / "cmdline").read_bytes().replace(b"python3\0", b"unrelated\0"))
    else:
        (parent / "cmdline").write_bytes(b"bash\0-c\0exec /fixture/bin/crossing\0")
    with pytest.raises(access.MaintenanceBusy, match="remain: 123"):
        maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True)


@pytest.mark.parametrize("target", ["parent", "controller"])
@pytest.mark.parametrize("changed", ["pid_reuse", "exec", "unreadable"])
def test_preflight_current_uv_launcher_rechecks_both_identities(tmp_path, preflight_proc, monkeypatch, target, changed, launcher_form):
    from services import maintenance_preflight

    parent, controller = _uv_controller_fixture(preflight_proc, tmp_path, launcher_form)
    directory = parent if target == "parent" else controller
    process = maintenance_preflight._process
    calls = 0

    def changing(source):
        nonlocal calls
        if source == directory:
            calls += 1
            if calls == 2:
                if changed == "unreadable":
                    raise PermissionError("launcher identity unavailable")
                if changed == "pid_reuse":
                    path = source / "stat"
                    path.write_text(path.read_text().rsplit(" ", 1)[0] + " 99999999")
                else:
                    (source / "cmdline").write_bytes(b"python3\0cli.py\0index\0")
        return process(source)

    monkeypatch.setattr(maintenance_preflight, "_process", changing)
    with pytest.raises(access.MaintenanceBusy, match="remain: 123"):
        maintenance_preflight.check_known_clients(tmp_path, clients_stopped=True)


def test_preflight_current_uv_launcher_real_subprocess(tmp_path):
    source = Path(__file__).resolve().parents[1]
    script = tmp_path / "crossing"
    script.write_text(f"#!{sys.executable}\n" + """
import json
import os
from pathlib import Path
from services import maintenance_preflight as preflight

controller = preflight._process(Path('/proc') / str(os.getpid()))
parent = preflight._process(Path('/proc') / str(os.getppid()))
preflight._snapshot = lambda known_pids, diagnostics=None: {item['pid']: item for item in (controller, parent)}
result = preflight.check_known_clients(Path.cwd(), clients_stopped=True)
print(json.dumps({'parent': parent, 'classified': preflight._known_client(parent, Path.cwd()), 'result': result}))
""")
    script.chmod(0o700)
    environment = {**os.environ, "PYTHONPATH": str(source), "HOME": str(tmp_path)}
    environment.pop("CROSSING_PROJECT", None)
    result = subprocess.run([shutil.which("uv"), "run", str(script), "migration", "start", "--project", str(tmp_path)],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30, check=True)
    observed = json.loads(result.stdout)
    assert Path(observed["parent"]["arguments"][0]).name == "uv"
    assert observed["classified"] is True
    assert observed["result"]["checked_processes"] == 2
    assert observed["result"]["mutation_authorized"] is False


@pytest.mark.parametrize("case", ["clean", "explicit", "independent"])
def test_preflight_current_uv_launcher_actual_wrapper(tmp_path, case):
    source = Path(__file__).resolve().parents[1]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    wrapper = scripts / "crossing"
    shutil.copy2(source / "scripts/crossing", wrapper)
    probe = tmp_path / "cli.py"
    probe.write_text("case = " + repr(case) + "\n" + """
import json
import os
from pathlib import Path
import subprocess
import sys
from services import maintenance_preflight as preflight
from tool.project_access import MaintenanceBusy

controller = preflight._process(Path('/proc') / str(os.getpid()))
parent = preflight._process(Path('/proc') / str(os.getppid()))
processes = {item['pid']: item for item in (controller, parent)}
worker = None
if case == 'independent':
    worker = subprocess.Popen([sys.executable, '-c', "import sys; print('ready', flush=True); sys.stdin.read()"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert worker.stdout.readline().strip() == 'ready'
    processes[worker.pid] = preflight._process(Path('/proc') / str(worker.pid))
preflight._snapshot = lambda known_pids, diagnostics=None: processes
observed = {'parent': parent, 'controller': controller, 'error': None}
known_pids = [parent['pid']] if case == 'explicit' else []
try:
    observed['result'] = preflight.check_known_clients(Path.cwd(), clients_stopped=True, known_pids=known_pids)
except MaintenanceBusy as error:
    observed['error'] = str(error)
finally:
    if worker is not None:
        observed['independent_pid'] = worker.pid
        worker.stdin.close()
        worker.wait(timeout=10)
        worker.stdout.close()
print(json.dumps(observed))
""")
    environment = {**os.environ, "HOME": str(tmp_path), "PYTHONPATH": str(source)}
    environment.pop("CROSSING_PROJECT", None)
    result = subprocess.run([str(wrapper), "migration", "prepare", "--project", str(tmp_path)],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30, check=True)
    observed = json.loads(result.stdout)
    print(result.stdout)
    assert observed["parent"]["arguments"][1:4] == ["run", "python", "cli.py"]
    assert observed["controller"]["arguments"][1] == "cli.py"
    assert observed["controller"]["parent"] == observed["parent"]["pid"]
    assert observed["parent"]["started"] <= observed["controller"]["started"]
    if case == "clean":
        assert observed["error"] is None
        assert observed["result"]["mutation_authorized"] is False
    else:
        blocked = observed["parent"]["pid"] if case == "explicit" else observed["independent_pid"]
        assert observed["error"] == f"Known Crossing clients or descendants remain: {blocked}"


@pytest.mark.parametrize("script", ["services/engraving_local_pipeline.py", "scripts/fixture_job.py"])
def test_preflight_recognizes_checkout_python_jobs_from_other_cwds(tmp_path, preflight_proc, script):
    from services.maintenance_preflight import check_quiescence

    source = Path(__file__).resolve().parents[1] / script
    _process_fixture(preflight_proc, 123, ["python3", str(source)], "/")
    access.enroll(tmp_path, approved=True)
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        with pytest.raises(access.MaintenanceBusy, match="remain: 123"):
            check_quiescence(owner, clients_stopped=True)


def test_preflight_unavailable_operator_job_inspection_refuses(tmp_path, preflight_proc, monkeypatch):
    from services import maintenance_preflight

    _process_fixture(preflight_proc, 123, ["crossing"], tmp_path)
    original = maintenance_preflight._read

    def denied(path):
        if path.name == "cmdline":
            raise PermissionError("fixture inspection denied")
        return original(path)

    monkeypatch.setattr(maintenance_preflight, "_read", denied)
    access.enroll(tmp_path, approved=True)
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        with pytest.raises(access.MaintenanceBusy, match="123"):
            maintenance_preflight.check_quiescence(owner, clients_stopped=True, known_pids=[123])


def test_preflight_missing_proc_is_diagnostic_under_exclusive_owner(tmp_path, preflight_proc):
    from services.maintenance_preflight import check_quiescence

    preflight_proc.rmdir()
    access.enroll(tmp_path, approved=True)
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        result = check_quiescence(owner, clients_stopped=True)
        assert result["diagnostics"]
        assert result["mutation_authorized"] is False
        assert owner.inspect()["state"] == "maintenance"


def test_preflight_requires_live_exclusive_owner(preflight_proc):
    from services.maintenance_preflight import check_quiescence

    with pytest.raises(access.MaintenanceError, match="exclusive inspection ownership"):
        check_quiescence(None, clients_stopped=True)


@pytest.mark.parametrize("identity_changed", [False, True])
def test_preflight_distinguishes_counters_from_process_identity(tmp_path, preflight_proc, monkeypatch, identity_changed):
    from services import maintenance_preflight

    _process_fixture(preflight_proc, 123, ["unrelated"], "/")
    original = maintenance_preflight._read
    reads = []

    def changing(path):
        value = original(path)
        if path.name == "stat":
            reads.append(path)
            if len(reads) == 2:
                prefix, _, suffix = value.rpartition(b")")
                fields = suffix.split()
                fields[19 if identity_changed else 11] = b"99999"
                return prefix + b") " + b" ".join(fields)
        return value

    monkeypatch.setattr(maintenance_preflight, "_read", changing)
    access.enroll(tmp_path, approved=True)
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        if identity_changed:
            result = maintenance_preflight.check_quiescence(owner, clients_stopped=True)
            assert any("identity changed" in item["message"] for item in result["diagnostics"])
        else:
            assert maintenance_preflight.check_quiescence(owner, clients_stopped=True)["checked_processes"] == 1


def test_preflight_bound_reports_incomplete_survey(tmp_path, preflight_proc, monkeypatch):
    from services import maintenance_preflight

    _process_fixture(preflight_proc, 123, ["unrelated"], "/")
    _process_fixture(preflight_proc, 124, ["unrelated"], "/")
    monkeypatch.setattr(maintenance_preflight, "_MAX_PROCESSES", 1)
    access.enroll(tmp_path, approved=True)
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        result = maintenance_preflight.check_quiescence(owner, clients_stopped=True)
        assert result["survey_complete"] is False
        assert any("exceeded its bound" in item["message"] for item in result["diagnostics"])


def test_preflight_real_process_shutdown_and_recheck(tmp_path):
    from services.maintenance_preflight import check_quiescence

    access.enroll(tmp_path, approved=True)
    worker = subprocess.Popen([sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()", "crossing"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert worker.stdout.readline().strip() == "ready"
        with access.maintenance(tmp_path, "fixture", approved=True) as owner:
            with pytest.raises(access.MaintenanceBusy, match="remain"):
                check_quiescence(owner, clients_stopped=True)
            worker.communicate("", timeout=10)
            assert worker.returncode == 0
            result = check_quiescence(owner, clients_stopped=True)
            assert result["mutation_authorized"] is False
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=10)


@pytest.mark.parametrize("media_type", ["movie", "gameplay"])
def test_direct_eager_owners_refuse_closed_project(tmp_path, media_type):
    from data.metadata import get_metadata, load_json_metadata
    from data.shotlist import read_shotlist
    from services.illustration_index import load_index, rebuild_index

    _closed(tmp_path)
    operations = [
        lambda: get_metadata(tmp_path, media_type=media_type),
        lambda: load_json_metadata(project_path=tmp_path, media_type=media_type),
        lambda: read_shotlist(tmp_path, "film.mp4", media_type),
        lambda: load_index(tmp_path, "silhouettes", media_type),
        lambda: rebuild_index(tmp_path, "silhouettes", media_type),
    ]
    for operation in operations:
        with pytest.raises(access.MaintenanceClosed):
            operation()


def test_mcp_reused_request_context_is_refused(tmp_path, monkeypatch):
    from mcp_server import mcp_server as server

    monkeypatch.setenv("CROSSING_PROJECT", str(tmp_path))
    assert json.loads(server.list_movies())["ok"] is True
    _closed(tmp_path)
    result = json.loads(server.list_movies())
    assert result["ok"] is False
    assert result["error"] == "Project access refused"


def test_mcp_request_pins_admitted_project(tmp_path, monkeypatch):
    from data import metadata
    from mcp_server import mcp_server as server

    other = tmp_path / "other"
    other.mkdir()
    _closed(other)
    monkeypatch.setenv("CROSSING_PROJECT", str(tmp_path))
    original = server._ctx
    visited = []

    def switch_selection():
        monkeypatch.setenv("CROSSING_PROJECT", str(other))
        return original()

    def get_metadata(project, **kwargs):
        visited.append(project)
        return []

    monkeypatch.setattr(server, "_ctx", switch_selection)
    monkeypatch.setattr(metadata, "get_metadata", get_metadata)
    assert json.loads(server.list_movies())["ok"] is True
    assert visited == [str(tmp_path)]
    assert server._resolve_project_path() == str(other)


def test_mcp_inflight_request_holds_lease_until_response(tmp_path, monkeypatch):
    from data import metadata
    from mcp_server import mcp_server as server

    monkeypatch.setenv("CROSSING_PROJECT", str(tmp_path))
    access.enroll(tmp_path, approved=True)
    started, finish = threading.Event(), threading.Event()
    results = []

    def get_metadata(*args, **kwargs):
        started.set()
        assert finish.wait(5)
        return []

    monkeypatch.setattr(metadata, "get_metadata", get_metadata)
    worker = threading.Thread(target=lambda: results.append(server.list_movies()))
    worker.start()
    try:
        assert started.wait(5)
        with pytest.raises(access.MaintenanceBusy):
            with access.maintenance(tmp_path, "fixture", approved=True):
                pytest.fail("active MCP request did not drain")
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive()
    assert json.loads(results[0])["ok"] is True
    assert json.loads(server.list_movies())["ok"] is False


def test_session_does_not_authorize_new_callbacks(tmp_path):
    access.enroll(tmp_path, approved=True)
    with access.session(tmp_path):
        with pytest.raises(access.MaintenanceBusy):
            with access.maintenance(tmp_path, "fixture", approved=True):
                pytest.fail("live session did not drain")
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(tmp_path):
                pytest.fail("new callback reused session authorization")
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        assert owner.inspect()["state"] == "maintenance"


@pytest.fixture(scope="module")
def app():
    from PyQt5.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application


def test_real_window_blocks_before_browser_and_drains_on_destroy(tmp_path, monkeypatch, app):
    from PyQt5.QtCore import QCoreApplication, QEvent
    from PyQt5.QtWidgets import QWidget
    from tool import prefs
    from visualizers.window_visualizer import WindowVisualizer

    store = {"path": str(tmp_path)}
    monkeypatch.setattr(prefs, "get", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(prefs, "set", lambda key, value: store.__setitem__(key, value))
    built = []

    class FixtureWindow(WindowVisualizer):
        def create_browser(self):
            built.append(True)
            return QWidget()

        def create_inspector(self):
            return QWidget()

    access.enroll(tmp_path, approved=True)
    window = FixtureWindow()
    with pytest.raises(access.MaintenanceBusy):
        with access.maintenance(tmp_path, "fixture", approved=True):
            pytest.fail("live window did not drain")
    with pytest.raises(access.MaintenanceClosed):
        FixtureWindow()
    assert built == [True]
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    with access.maintenance(tmp_path, "fixture", approved=True):
        pass


def test_project_workers_refuse_before_service_work(tmp_path, monkeypatch, app):
    from services import backup_mirror, corpus_stats
    from visualizers.project_visualizer import _BackupWorker, _ProjectColumnsWorker

    backup = Mock(side_effect=AssertionError("backup must not run"))
    columns = Mock(side_effect=AssertionError("cached data must not be read"))
    monkeypatch.setattr(backup_mirror, "prepare_mirror", backup)
    monkeypatch.setattr(corpus_stats, "get_live_project_columns", columns)
    monkeypatch.setattr(corpus_stats, "get_cached_project_columns", columns)
    _closed(tmp_path)
    worker = _BackupWorker(tmp_path, "unused-fixture-destination")
    worker.run()
    assert "maintenance" in worker.error
    column_worker = _ProjectColumnsWorker(tmp_path)
    errors = []
    column_worker.tier_failed.connect(lambda *args: errors.append(args))
    column_worker.run()
    assert len(errors) == 2
    backup.assert_not_called()
    columns.assert_not_called()


def test_mosaic_workers_refuse_before_job_body(tmp_path, monkeypatch, app):
    from visualizers.mosaic_visualizer import SearchWorker, VocabularyWorker, ShotTypeWorker, ShotTypeFacetWorker

    workers = [
        SearchWorker("fixture", None, None, None, False, str(tmp_path)),
        VocabularyWorker("objects", None, str(tmp_path)),
        ShotTypeWorker(str(tmp_path), "movie"),
        ShotTypeFacetWorker(str(tmp_path), "movie", "wide", purpose="fields"),
    ]
    _closed(tmp_path)
    for worker in workers:
        body = Mock(side_effect=AssertionError("closed job ran"))
        monkeypatch.setattr(worker, "_run_admitted", body)
        errors = []
        worker.error.connect(errors.append)
        worker.run()
        assert len(errors) == 1 and "maintenance" in errors[0]
        body.assert_not_called()


@pytest.mark.parametrize("module,name", [
    ("book_visualizer", "BookVisualizerWindow"),
    ("cloud_visualizer", "CloudVisualizer"),
    ("flipbook_visualizer", "FlipbookVisualizerWindow"),
    ("illustration_visualizer", "IllustrationWindow"),
    ("metadata_visualizer", "MetadataVisualizer"),
    ("mosaic_visualizer", "MosaicVisualizer"),
    ("palette_visualizer", "PaletteVisualizerWindow"),
    ("segmentation_visualizer", "SAMExplorer"),
    ("shot_visualizer", "ShotlistVisualizer"),
])
def test_concrete_constructor_refuses_before_subclass_work(tmp_path, app, module, name):
    constructor = getattr(import_module("visualizers." + module), name)
    arguments = {
        name: str(tmp_path) if name == "project_path" else None
        for name, parameter in inspect.signature(constructor.__init__).parameters.items()
        if name != "self" and parameter.default is inspect.Parameter.empty
        and parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }
    _closed(tmp_path)
    with pytest.raises(access.MaintenanceClosed):
        constructor(**arguments)


def test_palette_workers_refuse_before_model_or_image_work(tmp_path, monkeypatch, app):
    from visualizers.palette_visualizer import _GenerationWorker, _MaskWorker

    workers = [_GenerationWorker(str(tmp_path), "fixture.mp4", "movie"),
               _MaskWorker(str(tmp_path), "cached.png", "fixture")]
    _closed(tmp_path)
    for worker in workers:
        body = Mock(side_effect=AssertionError("model/image work ran"))
        monkeypatch.setattr(worker, "_run_admitted", body)
        errors = []
        worker.failed.connect(errors.append)
        worker.run()
        assert len(errors) == 1 and "maintenance" in errors[0]
        body.assert_not_called()


def test_illustration_ipc_rechecks_delivery_and_queued_navigation(tmp_path, app):
    from types import SimpleNamespace
    from visualizers.illustration_visualizer import _IllIpcServer, _ill_ipc_send_navigate, IllustrationWindow

    server = _IllIpcServer(str(tmp_path))
    messages = []
    server.navigate_requested.connect(lambda *args: messages.append(args))
    navigation = Mock(side_effect=AssertionError("queued navigation ran"))
    window = SimpleNamespace(_project_path=str(tmp_path), _navigate_admitted=navigation)
    _closed(tmp_path)
    server._handle_message({"action": "navigate"})
    IllustrationWindow._on_ipc_navigate(window, "", "", "", "", "")
    assert messages == []
    navigation.assert_not_called()
    with pytest.raises(access.MaintenanceClosed):
        _ill_ipc_send_navigate(str(tmp_path))


def test_cached_delete_path_and_illustration_index_worker_refuse(tmp_path, monkeypatch, app):
    from visualizers.illustration_visualizer import _DeleteWorker, _IllustrationIndexWorker

    cached = tmp_path / "data" / "cached"
    cached.mkdir(parents=True)
    asset = cached / "asset.txt"
    asset.write_text("fixture")
    deleting = _DeleteWorker(cached, project_path=str(tmp_path))
    indexing = _IllustrationIndexWorker(str(tmp_path), ["movie"])
    body = Mock(side_effect=AssertionError("index job ran"))
    monkeypatch.setattr(indexing, "_run_admitted", body)
    errors = []
    indexing.finished.connect(lambda ok, error: errors.append((ok, error)))
    _closed(tmp_path)
    deleting.run()
    indexing.run()
    assert asset.read_text() == "fixture"
    assert len(errors) == 1 and errors[0][0] is False and "maintenance" in errors[0][1]
    body.assert_not_called()


def test_queued_callback_cannot_borrow_cli_operation(tmp_path, app):
    from PyQt5.QtCore import QTimer

    access.enroll(tmp_path, approved=True)
    results = []

    def request():
        with pytest.raises(access.MaintenanceBusy):
            with access.maintenance(tmp_path, "fixture", approved=True):
                pass

    def callback():
        try:
            with access.operation(tmp_path):
                results.append("admitted")
        except access.MaintenanceClosed:
            results.append("closed")

    with access.operation(tmp_path):
        worker = threading.Thread(target=request)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        QTimer.singleShot(0, callback)
        with access.independent_operations():
            app.processEvents()
        assert results == ["closed"]


def test_annotation_and_generation_refuse_before_work(tmp_path):
    from data.annotate import annotate_all_files, annotate_file_shots
    from generators.cloud import cloud_from_annotations
    from services.engraving_batch import batch_generate_engravings

    _closed(tmp_path)
    for function in (annotate_all_files, annotate_file_shots, cloud_from_annotations, batch_generate_engravings):
        arguments = {
            name: tmp_path if name == "project_path" else None
            for name, parameter in inspect.signature(function).parameters.items()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        }
        with pytest.raises(access.MaintenanceClosed):
            function(**arguments)


@pytest.mark.parametrize("selection", ["project", "project_path", "environment"])
def test_cli_explicit_and_environment_project_selection(tmp_path, monkeypatch, selection):
    import cli

    _closed(tmp_path)
    handler = Mock()
    args = Namespace(func=handler)
    if selection == "environment":
        monkeypatch.setenv("CROSSING_PROJECT", str(tmp_path))
    else:
        setattr(args, selection, str(tmp_path))
    parser = Mock()
    parser.parse_known_args.return_value = (args, [])
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    monkeypatch.setattr("sys.argv", ["crossing", "fixture"])
    with pytest.raises(SystemExit, match="Project access refused"):
        cli.main()
    handler.assert_not_called()


_CHILD = """
import os
import sys
from tool import project_access as access
project, phase = sys.argv[1:]
if phase == 'before-request':
    os._exit(73)
if phase == 'requested':
    original = access._acquire
    calls = []
    def acquire(descriptor, timeout):
        calls.append(descriptor)
        if len(calls) == 2:
            os._exit(73)
        return original(descriptor, timeout)
    access._acquire = acquire
if phase == 'exclusive-before-state':
    original = access._write
    def write(descriptor, name, value):
        if name == 'state.json' and value.get('state') == 'maintenance':
            os._exit(73)
        return original(descriptor, name, value)
    access._write = write
if phase == 'operation':
    with access.operation(project):
        os._exit(73)
with access.maintenance(project, 'fixture', approved=True):
    os._exit(73)
"""


@pytest.mark.parametrize("phase", ["before-request", "requested", "exclusive-before-state", "active", "operation"])
def test_process_death_and_restart_persist_state(tmp_path, phase):
    access.enroll(tmp_path, approved=True)
    result = subprocess.run([sys.executable, "-c", _CHILD, str(tmp_path), phase], capture_output=True, text=True, timeout=15)
    assert result.returncode == 73, result.stderr
    if phase in ("before-request", "operation"):
        with access.operation(tmp_path):
            pass
    else:
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(tmp_path):
                pytest.fail("process death reopened maintenance")
        with access.maintenance(tmp_path, "fixture", approved=True) as owner:
            assert owner.inspect()["state"] == "maintenance"


@pytest.mark.parametrize("kind", [
    "missing", "truncated", "version", "boolean-version", "identity", "unknown-state",
    "missing-anchor", "missing-control", "lock-replaced", "public-state", "state-symlink",
    "request-missing", "request-corrupt",
])
def test_corrupt_enrolled_evidence_is_closed(tmp_path, kind):
    access.enroll(tmp_path, approved=True)
    control = tmp_path / access.CONTROL
    state_file = control / "state.json"
    state = json.loads(state_file.read_text())
    if kind == "missing":
        state_file.unlink()
    elif kind == "truncated":
        state_file.write_text('{"version":')
    elif kind in ("version", "boolean-version"):
        state["version"] = 2 if kind == "version" else True
        state_file.write_text(json.dumps(state))
    elif kind == "identity":
        state["project"] = str(tmp_path / "different")
        state_file.write_text(json.dumps(state))
    elif kind == "unknown-state":
        state["state"] = "silently-open"
        state_file.write_text(json.dumps(state))
    elif kind == "missing-anchor":
        (tmp_path / access.ANCHOR).unlink()
    elif kind == "missing-control":
        shutil.rmtree(control)
    elif kind == "lock-replaced":
        (control / "owner.lock").unlink()
        (control / "owner.lock").write_bytes(b"")
    elif kind == "public-state":
        state_file.chmod(0o644)
    elif kind == "state-symlink":
        state_file.rename(control / "old-state.json")
        state_file.symlink_to(control / "old-state.json")
    else:
        _closed(tmp_path)
        if kind == "request-missing":
            (control / "request.json").unlink()
        else:
            (control / "request.json").write_text("{}")
    with pytest.raises(access.MaintenanceError):
        with access.operation(tmp_path):
            pytest.fail("corrupt evidence admitted")


def test_stale_pid_never_reopens_and_resume_requires_same_operation(tmp_path):
    _closed(tmp_path)
    state_file = tmp_path / access.CONTROL / "state.json"
    state = json.loads(state_file.read_text())
    state["pid"] = -12345
    state_file.write_text(json.dumps(state))
    with pytest.raises(access.MaintenanceClosed):
        with access.operation(tmp_path):
            pytest.fail("stale PID reopened")
    with pytest.raises(access.MaintenanceError, match="existing maintenance operation"):
        with access.maintenance(tmp_path, "different", approved=True):
            pass
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        assert owner.inspect()["pid"] == os.getpid()


@pytest.mark.parametrize("target", ["project", "control", "anchor"])
def test_symlink_paths_are_refused(tmp_path, target):
    project = tmp_path / "project"
    project.mkdir()
    if target == "project":
        alias = tmp_path / "alias"
        alias.symlink_to(project, target_is_directory=True)
        project = alias
    elif target == "control":
        (project / access.CONTROL).symlink_to(tmp_path, target_is_directory=True)
    else:
        (project / access.ANCHOR).symlink_to(tmp_path / "missing.json")
    with pytest.raises(access.MaintenanceError):
        with access.operation(project):
            pytest.fail("symlink admitted")


def test_nested_calls_can_finish_but_new_thread_is_refused(tmp_path):
    access.enroll(tmp_path, approved=True)
    outcomes = []
    with access.operation(tmp_path):
        def request():
            try:
                with access.maintenance(tmp_path, "fixture", approved=True):
                    outcomes.append("unexpected")
            except access.MaintenanceBusy:
                outcomes.append("draining")

        worker = threading.Thread(target=request)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        with access.operation(tmp_path):
            pass
        with pytest.raises(access.MaintenanceClosed):
            with access.session(tmp_path):
                pass
    assert outcomes == ["draining"]
    with access.maintenance(tmp_path, "fixture", approved=True):
        pass


def test_no_shared_to_exclusive_upgrade_or_unapproved_bypass(tmp_path, monkeypatch):
    with pytest.raises(access.MaintenanceError, match="approval"):
        access.enroll(tmp_path)
    access.enroll(tmp_path, approved=True)
    with access.operation(tmp_path):
        with pytest.raises(access.MaintenanceError, match="upgrade"):
            with access.maintenance(tmp_path, "fixture", approved=True):
                pass
    _closed(tmp_path)
    monkeypatch.setenv("CROSSING_IGNORE_MAINTENANCE", "1")
    with pytest.raises(access.MaintenanceClosed):
        with access.operation(tmp_path):
            pass
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        with pytest.raises(access.IncompleteCoverage):
            owner.require_resource_mutation()


def test_competing_owners_cannot_acquire_even_in_same_process(tmp_path):
    access.enroll(tmp_path, approved=True)
    with access.maintenance(tmp_path, "fixture", approved=True):
        with pytest.raises(access.MaintenanceBusy):
            with access.maintenance(tmp_path, "fixture", approved=True):
                pytest.fail("competing owner acquired")


def test_subprocess_does_not_inherit_parent_admission(tmp_path):
    access.enroll(tmp_path, approved=True)
    with access.operation(tmp_path):
        def request():
            with pytest.raises(access.MaintenanceBusy):
                with access.maintenance(tmp_path, "fixture", approved=True):
                    pass

        worker = threading.Thread(target=request)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        script = "from tool.project_access import operation; import sys;\nwith operation(sys.argv[1]): pass"
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=15)
        assert result.returncode != 0
        assert "MaintenanceClosed" in result.stderr


def test_project_switch_requires_new_admission(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _closed(second)
    with access.operation(first):
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(second):
                pass


def test_concurrent_readers_and_successful_drain(tmp_path):
    access.enroll(tmp_path, approved=True)
    ready = threading.Barrier(3)
    finish = threading.Event()
    outcomes = []

    def reader():
        with access.operation(tmp_path):
            ready.wait(timeout=5)
            assert finish.wait(5)

    readers = [threading.Thread(target=reader) for _ in range(2)]
    for reader_thread in readers:
        reader_thread.start()
    try:
        ready.wait(timeout=5)
        with pytest.raises(access.MaintenanceBusy):
            with access.maintenance(tmp_path, "fixture", approved=True):
                pytest.fail("concurrent readers did not hold leases")
    finally:
        finish.set()
        for reader_thread in readers:
            reader_thread.join(5)
            assert not reader_thread.is_alive()
    with access.maintenance(tmp_path, "fixture", approved=True) as owner:
        outcomes.append(owner.inspect()["state"])
    assert outcomes == ["maintenance"]


def test_async_child_cannot_reuse_parent_operation(tmp_path):
    access.enroll(tmp_path, approved=True)

    def request():
        with pytest.raises(access.MaintenanceBusy):
            with access.maintenance(tmp_path, "fixture", approved=True):
                pass

    async def child():
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(tmp_path):
                pytest.fail("child task inherited authorization")

    async def parent():
        with access.operation(tmp_path):
            await asyncio.to_thread(request)
            await asyncio.create_task(child())
            with access.operation(tmp_path):
                pass

    asyncio.run(parent())


def test_fork_drops_authorization_without_unlocking_parent(tmp_path):
    script = """
import os
import sys
from tool import project_access as access
project = sys.argv[1]
access.enroll(project, approved=True)
child = None
with access.operation(project):
    child = os.fork()
    if child:
        os.waitpid(child, 0)
if child == 0:
    with access.maintenance(project, 'fixture', approved=True):
        raise AssertionError('fork child unlocked parent')
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "MaintenanceBusy" in result.stderr
    assert access.status(tmp_path)["state"] == "maintenance-requested"
    with access.maintenance(tmp_path, "fixture", approved=True):
        pass


@pytest.mark.parametrize("phase", ["request", "pending-state", "active-state"])
def test_interrupted_state_sync_leaves_closed_evidence(tmp_path, monkeypatch, phase):
    access.enroll(tmp_path, approved=True)
    original = access._write

    def interrupted(descriptor, name, value):
        if ((phase == "request" and name == "request.json")
                or (phase == "pending-state" and value.get("state") == "maintenance-requested")
                or (phase == "active-state" and value.get("state") == "maintenance")):
            raise OSError(28, "fixture ENOSPC")
        return original(descriptor, name, value)

    monkeypatch.setattr(access, "_write", interrupted)
    with pytest.raises(OSError):
        with access.maintenance(tmp_path, "fixture", approved=True):
            pytest.fail("interrupted state entered")
    with pytest.raises(access.MaintenanceError):
        with access.operation(tmp_path):
            pytest.fail("state failure reopened project")


def test_lazy_and_coroutine_decorators_require_explicit_lifetime():
    def lazy(project):
        yield project

    async def asynchronous(project):
        return project

    for function in (lazy, asynchronous):
        with pytest.raises(TypeError, match="eager"):
            access.project_operation(function)







