"""Single-operator integration over existing owners; allocated fixtures only."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

from services import resource_graph as graph
from services import resource_rename as renames
from services import resource_migration as migration
from services.maintenance_preflight import check_quiescence, check_known_clients
from data.resource_storage import digest
from tool import project_access as access


RECEIPT = "integrated-migration.json"


def _binding(project, plan, approved_digest, plan_identity, operation_id):
    with access._root(project) as (project, root):
        identity = access._identity(project, root)
        if (identity["project"], identity["device"], identity["inode"]) not in access._rename_fixtures:
            raise access.IncompleteCoverage("Integrated mutation is restricted to allocated temporary fixtures")
    plan, reference, fingerprint = renames._plan(project, plan, approved_digest, plan_identity)
    with closing(migration.readonly(plan)) as database:
        metadata = {row[0]: json.loads(row[1]) for row in database.execute("SELECT * FROM meta")}
        revision = metadata.get("plan_revision", {}).get("stage", metadata.get("stage"))
        if metadata.get("project") != str(project) or revision != plan_identity:
            raise ValueError("Integrated plan/project identity differs")
    return project, {"version": 1, **identity, "operation": operation_id, "plan": reference,
                     "plan_sha256": approved_digest, "plan_identity": plan_identity,
                     "plan_stat": list(fingerprint), "fixture_only": True}


def _load(owner, binding):
    owner.require_fixture_renames()
    with access._root(owner.project) as (_project, root), access._control(root) as control:
        if not access._exists(control, RECEIPT):
            if any(owner.project.joinpath(access.CONTROL).glob("rename-*.sqlite3")):
                raise ValueError("Missing integrated evidence beside existing rename journal")
            return {**binding, "phase": "maintenance", "state": "incomplete"}
        record = access._read(control, RECEIPT)
    if any(record.get(key) != value for key, value in binding.items()):
        raise ValueError("Integrated commitment binding differs")
    if record.get("state") not in ("incomplete", "failed", "recoverable", "accepted", "released", "rolled-back", "restore-required"):
        raise ValueError("Unknown integrated migration state")
    phases = {"maintenance", "relocating", "relocated", "graph-installing", "graph-installed", "accepted",
              "release-intent", "released", "rollback-intent", "graph-reversed", "rename-rollback", "rolled-back"}
    if record.get("phase") not in phases:
        raise ValueError("Unknown integrated migration phase")
    terminal = {"accepted": "accepted", "release-intent": "accepted", "released": "released", "rolled-back": "rolled-back"}
    if ((record["phase"] in terminal and record["state"] != terminal[record["phase"]])
            or (record["state"] in {"accepted", "released", "rolled-back"} and terminal.get(record["phase"]) != record["state"])):
        raise ValueError("Inconsistent integrated phase and state")
    return record


def _save(owner, record, phase, state, interrupt=None, **details):
    owner.require_fixture_renames()
    record = {**record, **details, "phase": phase, "state": state}
    if len(json.dumps(record, sort_keys=True, allow_nan=False).encode()) > 65536:
        raise ValueError("Integrated evidence exceeds its bounded commitment size")
    with access._root(owner.project) as (_project, root), access._control(root) as control:
        access._write(control, RECEIPT, record)
    renames._event(interrupt, "integrated:" + phase)
    return record


def run_fixture_migration(project, plan, *, approved_digest, plan_identity, operation_id="integrated",
                          approved=False, clients_stopped=False, timeout=0, interrupt=None):
    if approved is not True or clients_stopped is not True:
        raise access.MaintenanceError("Explicit fixture maintenance and stopped-client acknowledgement required")
    project, binding = _binding(project, plan, approved_digest, plan_identity, operation_id)
    check_known_clients(project, clients_stopped=True)
    renames._event(interrupt, "before-enrollment")
    access.enroll(project, approved=True)
    with access.maintenance(project, operation_id, approved=True, timeout=timeout, interrupt=interrupt) as owner:
        check_quiescence(owner, clients_stopped=True)
        record = _load(owner, binding)
        if record["state"] == "accepted":
            _verified_acceptance(owner)
            return {"state": "accepted", "phase": record["phase"], "maintenance_released": False}
        if record["state"] in ("rolled-back", "restore-required", "released"):
            raise ValueError("Integrated state cannot resume forward: " + record["state"])
        arguments = {"approved_digest": approved_digest, "plan_identity": plan_identity,
                     "clients_stopped": True, "interrupt": interrupt}
        record = _save(owner, record, "relocating", "recoverable", interrupt)
        try:
            renames.execute(owner, plan, **arguments)
            record = _save(owner, record, "relocated", "incomplete", interrupt)
            record = _save(owner, record, "graph-installing", "recoverable", interrupt)
            result = graph.install_fixture_graph(owner, plan, **arguments)
            record = _save(owner, record, "graph-installed", "incomplete", interrupt, graph=result)
        except Exception as error:
            _save(owner, record, record["phase"], "recoverable" if isinstance(error, (RuntimeError, OSError)) else "failed", error=str(error)[:2048])
            raise
        return {"state": record["state"], "phase": record["phase"], "graph_accepted": result["graph_accepted"],
                "maintenance_released": False}


def _verify_graph(owner, record, interrupt=None):
    plan = owner.project / record["plan"]
    project, binding = _binding(owner.project, plan, record["plan_sha256"], record["plan_identity"], record["operation"])
    _load(owner, binding)
    with access.fixture_graph_access(owner):
        result = graph.validate_fixture_graph(owner, plan, approved_digest=record["plan_sha256"],
                     plan_identity=record["plan_identity"], clients_stopped=True, interrupt=interrupt)
        if not result["graph_accepted"]:
            raise ValueError("Integrated graph acceptance failed: " + json.dumps(result))
        registry = access.canonical_registry(project)
        with closing(migration.readonly(registry)) as database:
            if database.execute("SELECT 1 FROM publication_operations WHERE state!='finished' LIMIT 1").fetchone():
                raise ValueError("Unrecognized incomplete publication state")
            if database.execute("SELECT 1 FROM generation_runs WHERE state!='complete' LIMIT 1").fetchone():
                raise ValueError("Incomplete generation run")
        from services.resource_resolution import indexed_publications, resolve_silhouette, resolve_engraving
        for kind in ("silhouettes", "engravings"):
            for media_type in ("movie", "gameplay"):
                for item in indexed_publications(project, kind, media_type):
                    resolved = resolve_silhouette(project, item["instance_id"]) if kind == "silhouettes" else resolve_engraving(project, item["source_instance_id"], mode=item["mode"])
                    if resolved["layout"] != "media-first-v1":
                        raise ValueError("Consumer did not resolve canonical layout")
    return result


def _verified_acceptance(owner):
    state = owner.require_fixture_renames()
    with access._root(owner.project) as (_project, root), access._control(root) as control:
        record = access._read(control, RECEIPT)
        context = access._read(control, access.CANONICAL_CONTEXT)
    accepted = record.get("acceptance")
    if record.get("state") not in ("accepted", "released") or not isinstance(accepted, dict):
        raise ValueError("Full integrated acceptance is required before release")
    if accepted.get("canonical_context") != context:
        raise ValueError("Accepted canonical context changed")
    for kind in ("rename", "graph"):
        journal = owner.project / access.CONTROL / (kind + "-" + state["operation"] + ".sqlite3")
        if digest(journal) != accepted.get(kind + "_journal_sha256"):
            raise ValueError("Accepted journal changed: " + kind)
    _verify_graph(owner, record)
    return record


def accept_fixture_migration(project, plan, *, approved_digest, plan_identity, operation_id="integrated",
                             approved=False, clients_stopped=False, interrupt=None):
    if approved is not True or clients_stopped is not True:
        raise access.MaintenanceError("Explicit fixture acceptance and stopped-client acknowledgement required")
    project, binding = _binding(project, plan, approved_digest, plan_identity, operation_id)
    check_known_clients(project, clients_stopped=True)
    with access.maintenance(project, operation_id, approved=True) as owner:
        record = _load(owner, binding)
        if record["phase"] not in ("graph-installed", "accepted"):
            raise ValueError("Graph installation must complete before integrated acceptance")
        _verify_graph(owner, record, interrupt)
        renames._event(interrupt, "before-acceptance-recording")
        _verify_graph(owner, record)
        with access._root(project) as (_project, root), access._control(root) as control:
            context = access._read(control, access.CANONICAL_CONTEXT)
        accepted = {"canonical_context": context,
                    "rename_journal_sha256": digest(project / access.CONTROL / ("rename-" + operation_id + ".sqlite3")),
                    "graph_journal_sha256": digest(project / access.CONTROL / ("graph-" + operation_id + ".sqlite3"))}
        _save(owner, record, "accepted", "accepted", interrupt, acceptance=accepted)
        renames._event(interrupt, "acceptance-recorded")
        return {"state": "accepted", "maintenance_released": False}


def released_evidence(project, root, state, record):
    identity = access._identity(project, root)
    if (identity["project"], identity["device"], identity["inode"]) not in access._rename_fixtures:
        raise access.IncompleteCoverage("Production reopening remains disabled")
    if (record.get("state") != "released" or record.get("phase") != "released" or record.get("fixture_only") is not True
            or record.get("operation") != state.get("operation") or any(record.get(key) != value for key, value in identity.items())
            or not isinstance(record.get("acceptance"), dict)
            or digest(project / access.CONTROL / RECEIPT) != state.get("release_evidence")):
        raise access.MaintenanceError("Invalid durable fixture release evidence")


def release_fixture_migration(project, plan, *, approved_digest, plan_identity, operation_id="integrated",
                              approved=False, clients_stopped=False, interrupt=None):
    if approved is not True or clients_stopped is not True:
        raise access.MaintenanceError("Explicit fixture release and stopped-client acknowledgement required")
    project, binding = _binding(project, plan, approved_digest, plan_identity, operation_id)
    check_known_clients(project, clients_stopped=True)
    state = access.status(project)
    if state and state["state"] == "open" and state.get("release_evidence"):
        access.canonical_registry(project)
        return {"state": "released", "maintenance_released": True, "production_authorized": False}
    with access.maintenance(project, operation_id, approved=True) as owner:
        record = _load(owner, binding)
        _verified_acceptance(owner)
        _save(owner, record, "release-intent", "accepted", interrupt)
        renames._event(interrupt, "before-release")
        return access._release_fixture(owner, interrupt=interrupt)


def _verify_original(owner, record):
    project = owner.project
    with closing(migration.readonly(project / record["plan"])) as frozen:
        for source, checksum in frozen.execute("SELECT source,sha256 FROM files WHERE disposition IN ('Move','Transform','Retain','Rebuild')"):
            if not graph._exact(project, source, checksum):
                raise ValueError("Original resource was not restored: " + source)
        metadata = {row[0]: json.loads(row[1]) for row in frozen.execute("SELECT * FROM meta")}
        for reference, checksum in metadata["inputs"].items():
            if not graph._exact(project, reference, checksum):
                raise ValueError("Original preparation evidence changed")
    if (project / "data/indexes/resources.sqlite3").exists() or (project / access.CONTROL / access.CANONICAL_CONTEXT).exists():
        raise ValueError("Canonical installation/context not reversed")


def rollback_fixture_migration(project, plan, *, approved_digest, plan_identity, operation_id="integrated",
                               approved=False, clients_stopped=False, interrupt=None):
    if approved is not True or clients_stopped is not True:
        raise access.MaintenanceError("Explicit fixture rollback acknowledgement required")
    project, binding = _binding(project, plan, approved_digest, plan_identity, operation_id)
    check_known_clients(project, clients_stopped=True)
    with access.maintenance(project, operation_id, approved=True) as owner, access.fixture_graph_access(owner):
        record = _load(owner, binding)
        if record["state"] == "rolled-back":
            _verify_original(owner, record)
            return {"outcome": "rollback", "state": "rolled-back", "maintenance_released": False}
        record = _save(owner, record, "rollback-intent", "recoverable", interrupt)
        arguments = {"approved_digest": approved_digest, "plan_identity": plan_identity,
                     "clients_stopped": True, "interrupt": interrupt}
        try:
            graph_journal = project / access.CONTROL / ("graph-" + operation_id + ".sqlite3")
            rename_journal = project / access.CONTROL / ("rename-" + operation_id + ".sqlite3")
            reverse = False
            if rename_journal.exists():
                with closing(migration.readonly(rename_journal)) as database:
                    reverse = database.execute("SELECT direction FROM meta").fetchone()[0] == "rollback"
            if graph_journal.exists() and not reverse:
                graph.rollback_fixture_graph(owner, plan, **arguments)
                record = _save(owner, record, "graph-reversed", "recoverable", interrupt)
            if graph_journal.exists() and reverse:
                with closing(migration.readonly(graph_journal)) as database:
                    graph._integrity(database)
                    if database.execute("SELECT 1 FROM transforms WHERE state!='undone' LIMIT 1").fetchone():
                        raise ValueError("Rename rollback started before graph reversal completed")
                    if database.execute("SELECT 1 FROM index_outputs WHERE state!='undone' LIMIT 1").fetchone():
                        raise ValueError("Dependent output reversal is incomplete")
                    for row in database.execute("SELECT source,before_sha FROM index_outputs"):
                        if row[1] is not None and not graph._exact(project, row[0], row[1]):
                            raise ValueError("Changed restored dependent output")
            record = _save(owner, record, "rename-rollback", "recoverable", interrupt)
            if rename_journal.exists():
                renames.execute(owner, plan, action="rollback", **arguments)
            _verify_original(owner, record)
            _save(owner, record, "rolled-back", "rolled-back", interrupt)
            return {"outcome": "rollback", "state": "rolled-back", "maintenance_released": False}
        except (ValueError, OSError, sqlite3.Error) as error:
            _save(owner, record, record["phase"], "restore-required", error=str(error)[:2048])
            return {"outcome": "restore-required", "reason": str(error)[:2048], "maintenance_released": False}


def recover_fixture_migration(project, plan, *, action="resume", **arguments):
    if action not in ("resume", "rollback"):
        raise ValueError("Recovery action must be resume or rollback")
    try:
        if action == "rollback":
            return rollback_fixture_migration(project, plan, **arguments)
        result = run_fixture_migration(project, plan, **arguments)
        return {**result, "outcome": "resume"}
    except (ValueError, OSError, sqlite3.Error) as error:
        return {"outcome": "restore-required", "reason": str(error)[:2048], "maintenance_released": False}