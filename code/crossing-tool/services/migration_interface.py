"""Operator presentation over the existing migration owners; production disabled."""

from contextlib import closing, contextmanager, nullcontext
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time

from data.resource_storage import digest
from services import resource_integration as integrated
from services import resource_migration as migration
from services import resource_rename as renames
from tool import project_access as access


PROJECT = Path("/home/cowpoke/playable/dead-crossing")
PLAN = "outputs/maintenance/media-first-stage-2b3d/migration-plan.sqlite3"
PLAN_DIGEST = "63022d5aecd085e08d4b595c4b0ac46e4f781c263c2efcfa930de73c049dc95d"
PREPARATION = "data/indexes/media-first-stage-2b3d-preparation.sqlite3"
PREPARATION_DIGEST = "5c412d463c0e0da5f736b664b2812d1ca9f40b4e6656aa22bc02721bedb8c13f"
REQUIRED_BYTES = 52506263552
REQUIRED_INODES = 1434197
BACKUP = Path("/run/media/cowpoke/PLAYABLE/backups/dead-crossing")
BACKUP_PLAN = "f43fedd3a5c38294dddf21aa06e640c540312ea56ab006a7727f30b589402542"
BACKUP_JOURNAL = "2482b8117f6ae04061e7dd1f2e9be29154e175d43e290e1a2538271221edb286"
MARKER_DIGEST = "16562d8dd0ea62702d98aea88a7e6d9bc9d80fbe7dcc01dd134944d3e56cb32d"


def _file(project, reference):
    from data.resource_storage import relative_path
    relative_path(project, reference)
    parts = reference.split("/")
    if Path(reference).is_absolute() or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Evidence requires an exact project-relative path")
    with access._root(project) as (_project, root), renames._parent(root, parts) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Evidence must be a regular single-link file")
        finally:
            os.close(descriptor)
    return project / reference


def _stat(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


@contextmanager
def _evidence_database(project, reference):
    from services.backup_mirror import sqlite_window
    window = renames.journal_window() if reference.startswith(access.CONTROL + "/rename-") else nullcontext()
    with window:
        path = _file(project, reference)
        with sqlite_window(path), closing(migration.readonly(path)) as database:
            yield database


def _fixture(project):
    with access._root(project) as (project, root):
        identity = access._identity(project, root)
    return (identity["project"], identity["device"], identity["inode"]) in access._rename_fixtures


def _backup(project):
    from services.backup_mirror import mounted_filesystems, read_marker, MARKER
    matches = [item for item in mounted_filesystems() if item.get("label") == "PLAYABLE"]
    if len(matches) != 1:
        raise ValueError("Accepted PLAYABLE mount is absent or ambiguous")
    mount = matches[0]
    expected = {"uuid": "687B-AC21", "label": "PLAYABLE", "fstype": "exfat",
                "target": str(BACKUP.parent.parent), "source": "/dev/sda1"}
    if any(mount.get(key) != value for key, value in expected.items()):
        raise ValueError("Accepted PLAYABLE mount identity differs")
    with access._root(expected["target"]) as (_mount, mount_root), access._root(BACKUP) as (_backup, backup_root):
        if os.fstat(mount_root).st_dev != os.fstat(backup_root).st_dev:
            raise ValueError("Backup root is not on the accepted mounted filesystem")
    context = {"project": str(project), "destination": str(BACKUP), "mount": mount}
    marker_path = _file(BACKUP, MARKER)
    marker_stat = _stat(marker_path)
    if marker_path.stat().st_size != 2715 or digest(marker_path) != MARKER_DIGEST:
        raise ValueError("Accepted backup marker changed")
    marker = read_marker(context)
    if marker_stat != _stat(marker_path):
        raise ValueError("Backup marker changed during inspection")
    if (marker.get("source_project") != str(project) or marker.get("last_verified_plan") != BACKUP_PLAN
            or marker.get("journal_sha256") != BACKUP_JOURNAL or marker.get("reconciled") is not True):
        raise ValueError("Accepted backup completion evidence differs")
    retained = {}
    for name, size in (("metadata-" + BACKUP_PLAN + ".sqlite3", 2489290752),
                       ("journal-" + BACKUP_PLAN + ".sqlite3", 998649856)):
        path = _file(BACKUP, ".crossing-mirror-control/" + name)
        if path.stat().st_size != size:
            raise ValueError("Retained backup evidence is missing or changed: " + name)
        retained[name] = _stat(path)
    return {"mount": expected, "root": _stat(BACKUP)[:2], "marker_sha256": MARKER_DIGEST,
            "retained": retained, "limitations": "Payload not reverified; full restore untested; Linux metadata reconstruction may be needed"}


def prepare(project, plan, checksum):
    from services.migration_authorization import test_target
    project = Path(project)
    fixture = _fixture(project)
    test = test_target(project)
    if not fixture and not test and (project != PROJECT or str(plan.relative_to(project)) != PLAN or checksum != PLAN_DIGEST):
        raise ValueError("Only the exact Dead Crossing project and frozen Stage 2B.3d plan are approved")
    path, reference, fingerprint = renames._plan(project, plan, checksum, "2B.3d")
    with _evidence_database(project, reference) as database:
        metadata = {row[0]: json.loads(row[1]) for row in database.execute("SELECT * FROM meta")}
        if (metadata.get("project") != str(project)
                or metadata.get("plan_revision", {}).get("stage", metadata.get("stage")) != "2B.3d"):
            raise ValueError("Frozen plan project or revision differs")
        preparation_reference = metadata["identity_registry"]
        preparation = _file(project, preparation_reference)
        allocation_stat = _stat(preparation)
        expected = metadata["inputs"][preparation_reference]
        if not fixture and not test and (preparation_reference != PREPARATION or expected != PREPARATION_DIGEST):
            raise ValueError("Approved canonical identity allocation differs")
        if digest(preparation) != expected:
            raise ValueError("Frozen canonical identity allocation changed")
        if allocation_stat != _stat(preparation) or list(fingerprint) != _stat(path):
            raise ValueError("Frozen input changed during preparation")
        counts = {row[0]: row[1] for row in database.execute("SELECT disposition,count(*) FROM files GROUP BY disposition") } if fixture or test else {
            "Move": 669634, "Transform": 668681, "Rebuild": 15, "identities": 668050}
    with access._root(project) as (project, root):
        identity = access._identity(project, root)
        capacity = os.fstatvfs(root)
    available = capacity.f_bavail * capacity.f_frsize
    inodes = capacity.f_favail
    required = 0 if fixture else REQUIRED_BYTES
    required_inodes = 0 if fixture else REQUIRED_INODES
    if test:
        from services.resource_graph import graph_storage_budget
        budget = graph_storage_budget(path, preparation_bytes=preparation.stat().st_size)
        required, required_inodes = budget["peak_additional_bytes"], budget["additional_inode_bound"]
    blockers = []
    if available < required or inodes < required_inodes:
        blockers.append("Insufficient additional bytes or inodes; no cleanup is authorized")
    try:
        recovery = {"fixture": True} if fixture else _backup(project)
    except (OSError, ValueError, RuntimeError) as error:
        blockers.append("Recovery source check failed: " + str(error))
        recovery = {"error": str(error)}
    binding = {**identity, "plan": reference, "plan_sha256": checksum, "plan_stat": list(fingerprint),
               "plan_revision": "2B.3d", "identity_allocation": preparation_reference,
               "identity_allocation_sha256": expected, "allocation_stat": allocation_stat,
               "recovery_source": recovery, "fixture_only": fixture}
    if not fixture and not test:
        blockers.append("Production execution disabled; separate reviewed enabling authorization required")
    return {"project": str(project), "plan": "Stage 2B.3d", "plan_sha256": checksum,
            "operations": counts, "available_bytes": available, "required_bytes": required,
            "available_inodes": inodes, "required_inodes": required_inodes,
            "backup": recovery, "components": "Existing maintenance, rename, graph and integrated owners available",
            "blocking_conditions": blockers, "authorization_binding": binding,
            "status": "Ready for operator review" if not blockers else "Blocked",
            "effects": "No files moved. No maintenance entry. No backup inventory or payload verification."}


def check_authorization(actual, approved, *, confirmed=False):
    if confirmed is not True:
        raise access.MaintenanceError("Explicit human confirmation is required")
    if actual != approved:
        raise access.MaintenanceError("Project, plan, allocation, filesystem or recovery evidence changed")
    project = Path(actual["project"])
    if not actual.get("fixture_only") or not _fixture(project):
        raise access.IncompleteCoverage("Production execution is disabled pending a separate reviewed enabling change; no maintenance entered")
    if actual.get("recovery_source") != {"fixture": True}:
        raise access.MaintenanceError("Fixture recovery evidence changed")
    with access._root(project) as (_project, root):
        identity = access._identity(project, root)
    if any(actual.get(key) != value for key, value in identity.items()):
        raise access.MaintenanceError("Approved project filesystem identity changed")
    _, _, fingerprint = renames._plan(project, project / actual["plan"], actual["plan_sha256"], "2B.3d")
    if list(fingerprint) != actual["plan_stat"]:
        raise access.MaintenanceError("Approved plan filesystem identity changed")
    with _evidence_database(project, actual["plan"]) as database:
        metadata = {row[0]: json.loads(row[1]) for row in database.execute("SELECT * FROM meta")}
    allocation = _file(project, metadata["identity_registry"])
    if (_stat(allocation) != actual["allocation_stat"] or digest(allocation) != actual["identity_allocation_sha256"]
            or metadata["inputs"][metadata["identity_registry"]] != actual["identity_allocation_sha256"]):
        raise access.MaintenanceError("Approved identity allocation changed")


def status(project, plan, checksum, operation):
    from services.migration_authorization import inspect, test_target
    state = access.status(project)
    result = {"project": str(project), "plan": "Stage 2B.3d", "plan_sha256": checksum,
              "maintenance": state["state"], "phase": "Not started", "status": "Stopped",
              "progress": {}, "recovery_actions": "Prepare; production execution remains disabled",
              "blocking_conditions": [], "production_execution": "disabled"}
    result.update(elapsed_seconds=0, latest_durable_progress=None, activity="Stopped")
    result["approval"] = inspect(project)
    result["workflow_state"] = {"missing": "Prepared but unauthorized", "approved": "Approved but not started",
        "expired": "Expired unused approval", "cancelled": "Unused approval permanently cancelled",
        "running": "Consumed operation; inspect maintenance and progress", "completed": "Completed and reopened",
        "rolled-back": "Rolled back; closed for manual review", "restore-required": "Manual intervention required",
        "blocked": "Authorization evidence blocked; manual review required"}[result["approval"]["state"]]
    if result["approval"].get("operation") and result["approval"]["operation"] != operation:
        result["blocking_conditions"].append("Requested operation differs from the durable approval")
    counts = {}
    if project == PROJECT and str(plan.relative_to(project)) == PLAN and checksum == PLAN_DIGEST:
        counts = {"Move": 669634, "Transform": 668681, "Rebuild": 15}
    elif _fixture(project) or test_target(project):
        with _evidence_database(project, str(plan.relative_to(project))) as frozen:
            counts = {row[0]: row[1] for row in frozen.execute("SELECT disposition,count(*) FROM files GROUP BY disposition")}
    for table, disposition in (("moves", "Move"), ("transforms", "Transform"), ("index_outputs", "Rebuild")):
        if disposition in counts:
            result["progress"][table] = {"completed": 0, "total": counts[disposition], "remaining": counts[disposition],
                                         "direction": "forward", "basis": "Frozen expectation; journal not yet present"}
    result["plan_digest_source"] = "Expected seal; prepare authenticates fixed inputs"
    operation = state.get("operation", operation)
    result["operation_id"] = operation
    clocks = []
    with access._root(project) as (_project, root):
        if not access._exists(root, access.CONTROL):
            return result
        with access._control(root) as control:
            if access._exists(control, integrated.RECEIPT):
                record = access._read(control, integrated.RECEIPT)
                integrated._validate_record(record, {key: state[key] for key in ("project", "device", "inode")} | {
                    "operation": operation, "plan_sha256": checksum, "plan": str(plan.relative_to(project)), "plan_identity": "2B.3d"})
                result.update(phase=record["phase"], status=record["state"])
                result["plan_digest_source"] = "Durable integrated commitment; execution rechecks bytes"
                result["recovery_actions"] = {
                    "accepted": "Release requires separate confirmation and fresh verification",
                    "released": "Crossing reopened; retain all recovery evidence",
                    "rolled-back": "Original state restored; remains closed for manual review",
                    "restore-required": "Stop for manual review; separately authorized restore may be required",
                }.get(record["state"], "Resume or rollback only after journal/filesystem reconciliation")
                if record.get("error"):
                    result["blocking_conditions"].append(record["error"])
            elif state["state"] != "open":
                if any(access._exists(control, kind + "-" + operation + ".sqlite3") for kind in ("rename", "graph")):
                    raise ValueError("Missing integrated evidence beside existing journals; manual review required")
                result["phase"] = "Maintenance entry / relocation"
                result["recovery_actions"] = "Resume requires the existing operation ID and consistent journals"
            if state["state"] not in ("open", "legacy"):
                lock = os.open("owner.lock", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=control)
                try:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        result["activity"] = "Running" if state["state"] == "maintenance" else "Waiting for leases to drain"
                    else:
                        fcntl.flock(lock, fcntl.LOCK_UN)
                        result["activity"] = "Stopped; recovery requires reconciliation"
                finally:
                    os.close(lock)
    for kind, tables in (("rename", (("moves", "done", "undone"),)),
                         ("graph", (("transforms", "done", "undone"), ("index_outputs", "installed", "undone")))):
        journal = project / access.CONTROL / (kind + "-" + operation + ".sqlite3")
        if not os.path.lexists(journal):
            continue
        with _evidence_database(project, str(journal.relative_to(project))) as database:
            header = database.execute("SELECT * FROM meta").fetchall()
            if len(header) != 1:
                raise ValueError("Missing or corrupt journal header; manual review required")
            binding = json.loads(header[0]["payload"])
            if (binding.get("project") != str(project) or binding.get("operation") != operation
                    or binding.get("token") != state.get("token")
                    or binding.get("plan_digest", binding.get("sha256")) != checksum):
                raise ValueError("Journal identity differs; manual review required")
            existing = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            reverse = header[0]["direction"] == "rollback"
            for table, forward, backward in tables:
                if table not in existing:
                    continue
                total, completed = database.execute("SELECT count(*),coalesce(sum(state=?),0) FROM " + table,
                                                     (backward if reverse else forward,)).fetchone()
                result["progress"][table] = {"completed": completed, "total": total, "remaining": total - completed,
                                             "direction": "rollback" if reverse else "forward"}
            if "progress_clock" in existing:
                clock = database.execute("SELECT started,updated,phase FROM progress_clock").fetchone()
                if clock is not None:
                    clocks.append(dict(clock))
    if clocks:
        latest = max(clocks, key=lambda clock: clock["updated"])
        result["latest_durable_progress"] = latest
        result["elapsed_seconds"] = max(0, latest["updated"] - min(clock["started"] for clock in clocks))
    for reference in ("data/indexes/resources.sqlite3", "data/indexes/.graph-" + operation + ".sqlite3"):
        if not os.path.lexists(project / reference):
            continue
        with _evidence_database(project, reference) as database:
            if database.execute("SELECT 1 FROM sqlite_master WHERE name='resource_locations'").fetchone():
                completed = database.execute("SELECT count(*) FROM resource_locations").fetchone()[0]
                result["progress"]["publication_pairs"] = {"completed": completed, "total": 668051 if project == PROJECT else None}
                if _fixture(project):
                    with _evidence_database(project, str(plan.relative_to(project))) as frozen:
                        total = frozen.execute("SELECT count(*) FROM transformations WHERE schema IN ('silhouette-v1','engraving-v2')").fetchone()[0]
                    result["progress"]["publication_pairs"].update(total=total, remaining=max(0, total - completed))
        break
    return result


class Progress:
    def __init__(self, project, plan, checksum, operation, emit, interrupt=None):
        self.project, self.plan, self.checksum, self.operation = project, plan, checksum, operation
        self.emit, self.interrupt = emit, interrupt
        self.last = -float("inf")
        self.started = time.monotonic()
        self.phase = None
        self.lock = threading.RLock()
        self.failure = None
        self.previous = None
        self.current_event = "Preparing"

    def report(self, event):
        with self.lock:
            now = time.monotonic()
            try:
                snapshot = status(self.project, self.plan, self.checksum, self.operation)
            except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
                self.emit({"phase": self.phase or event, "status": "Waiting for a readable durable checkpoint",
                           "detail": str(error)[:300], "elapsed_seconds": round(now - self.started, 1)})
                return
            counters = snapshot["progress"]
            estimate = "Estimating..."
            key = {"integrated:relocating": "moves", "metadata-preparation": "transforms",
                   "registry-preparation": "publication_pairs", "index-rebuilding": "index_outputs"}.get(self.phase)
            current = counters.get(key, {})
            completed, total = current.get("completed", 0), current.get("total")
            if self.previous is not None:
                previous_time, previous_completed = self.previous
                if completed > previous_completed and now - previous_time >= 1 and total is not None and total >= completed:
                    estimate = round((total - completed) * (now - previous_time) / (completed - previous_completed))
            self.previous = now, completed
            self.emit({"phase": self.phase or event, "progress": counters,
                       "elapsed_seconds": round(now - self.started, 1), "remaining_seconds": estimate,
                       "status": "Running" if snapshot.get("activity") == "Running" else snapshot.get("activity", "Running"),
                       "current_operation": self.current_event, "estimate_scope": "Current phase only",
                       "latest_durable_progress": snapshot.get("latest_durable_progress")})

    @contextmanager
    def reporting(self):
        stopped = threading.Event()
        def periodic():
            try:
                while not stopped.wait(5):
                    self.report("Periodic checkpoint")
            except Exception as error:
                self.failure = error
        worker = threading.Thread(target=periodic, name="migration-progress", daemon=False)
        worker.start()
        try:
            yield
        finally:
            stopped.set()
            worker.join()

    def __call__(self, event):
        self.current_event = event
        phase = event if event.startswith("integrated:") or event in (
            "maintenance-requested", "maintenance-owned", "registry-preparation", "registry-installation",
            "metadata-preparation", "index-rebuilding", "graph-validation", "before-acceptance-recording", "before-release") else self.phase
        now = time.monotonic()
        if phase != self.phase or now - self.last >= 5:
            if phase != self.phase:
                self.previous = None
            self.phase, self.last = phase, now
            self.report(event)
        if self.failure is not None:
            raise RuntimeError("Progress reporting failed; inspect durable status: " + str(self.failure))
        if self.interrupt is not None:
            self.interrupt(event)


def dispatch(args, *, emit=None, interrupt=None):
    project = Path(args.project)
    with access._root(project) as (resolved, _root):
        if project != resolved:
            raise ValueError("Use the exact absolute project path, not an alias")
    plan = Path(args.plan)
    plan = plan if plan.is_absolute() else project / plan
    operation = args.operation_id
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", operation):
        raise ValueError("Invalid operation ID")
    action = args.migration_action
    if action == "status":
        return status(project, plan, args.plan_sha256, operation)
    if action == "prepare":
        from services.migration_authorization import proposal
        return proposal(project, plan, args.plan_sha256, operation)
    if action in ("approve", "cancel"):
        from services.migration_authorization import approve, cancel
        if action == "approve":
            return approve(project, plan, args.plan_sha256, operation, args.reviewed_identity_sha256,
                           confirmed=args.confirm, operator=args.operator, interrupt=interrupt)
        return cancel(project, operation, args.reviewed_identity_sha256, confirmed=args.confirm,
                  plan=plan, checksum=args.plan_sha256, interrupt=interrupt)
    if not args.confirm or not args.clients_stopped:
        raise access.MaintenanceError("Review this action, then provide --confirm and --clients-stopped; no action taken")
    if not _fixture(project):
        from services.migration_authorization import execute
        progress = Progress(project, plan, args.plan_sha256, operation, emit or (lambda value: None), interrupt)
        with progress.reporting():
            return execute(project, plan, args.plan_sha256, operation, action, confirmed=args.confirm,
                           clients_stopped=args.clients_stopped, interrupt=progress)
    project, _binding = integrated._binding(project, plan, args.plan_sha256, "2B.3d", operation)
    arguments = {"approved_digest": args.plan_sha256, "plan_identity": "2B.3d", "operation_id": operation,
                 "approved": True, "clients_stopped": True}
    progress = Progress(project, plan, args.plan_sha256, operation, emit or (lambda value: None), interrupt)
    arguments["interrupt"] = progress
    with progress.reporting():
        return _execute(action, args, project, plan, operation, _binding, arguments, progress)


def _execute(action, args, project, plan, operation, binding, arguments, progress):
    if action == "start":
        ready = prepare(project, plan, args.plan_sha256)
        check_authorization(ready["authorization_binding"], ready["authorization_binding"], confirmed=args.confirm)
        return integrated.run_fixture_migration(project, plan, **arguments)
    if action in ("resume", "rollback"):
        return integrated.recover_fixture_migration(project, plan, action=action, **arguments)
    if action == "validate":
        from services.maintenance_preflight import check_known_clients, check_quiescence
        check_known_clients(project, clients_stopped=True)
        with access.maintenance(project, operation, approved=True, interrupt=progress) as owner:
            check_quiescence(owner, clients_stopped=True)
            record = integrated._load(owner, binding)
            return integrated._verify_graph(owner, record, progress)
    method = {"accept": integrated.accept_fixture_migration, "release": integrated.release_fixture_migration}[action]
    return method(project, plan, **arguments)