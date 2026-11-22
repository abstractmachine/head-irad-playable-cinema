"""Exact, durable migration approval over the existing maintenance owner."""

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
from uuid import uuid4
from weakref import WeakKeyDictionary

from tool import project_access as access


RECORD = "migration-authorization.json"
PRODUCTION = Path("/home/cowpoke/playable/dead-crossing")
ACTIVATED_IDENTITY_SHA256 = None
LIFETIME_SECONDS = 86400
_owners = WeakKeyDictionary()


def seal(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def test_target(project):
    with access._root(project) as (project, root):
        identity = access._identity(project, root)
    key = identity["project"], identity["device"], identity["inode"]
    return key in getattr(access, "_authorization_fixtures", set())


def activated(identity):
    project = Path(identity["project"])
    if test_target(project):
        return True
    return project == PRODUCTION and ACTIVATED_IDENTITY_SHA256 is not None and seal(identity) == ACTIVATED_IDENTITY_SHA256


def proposal(project, plan, checksum, operation):
    from services.migration_interface import prepare
    import re
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", operation):
        raise access.MaintenanceError("Valid migration operation ID required")
    ready = prepare(project, plan, checksum)
    identity = {**ready["authorization_binding"], "operation": operation,
                "operations": ready["operations"], "required_bytes": ready["required_bytes"],
                "required_inodes": ready["required_inodes"]}
    if activated(identity):
        ready["blocking_conditions"] = [item for item in ready["blocking_conditions"]
                                        if not item.startswith("Production execution disabled;")]
        ready["status"] = "Blocked" if ready["blocking_conditions"] else "Ready for operator review"
    return {**ready, "proposed_identity": identity, "identity_sha256": seal(identity),
            "activation": "active" if activated(identity) else "inactive; separately reviewed exact-identity activation required"}


def _validate(record, project, state):
    fields = {"version", "identity", "identity_sha256", "approval_id", "enrollment", "state",
              "approved_at", "expires_at", "operator_uid", "operator", "record_sha256"}
    if not fields.issubset(record) or record["state"] not in ("approved", "running", "completed", "cancelled", "rolled-back", "restore-required"):
        raise access.MaintenanceError("Incomplete or unknown migration approval")
    unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
    try:
        if seal(unsigned) != record["record_sha256"] or seal(record["identity"]) != record["identity_sha256"]:
            raise access.MaintenanceError("Corrupted migration approval seal")
    except (TypeError, ValueError) as error:
        raise access.MaintenanceError("Corrupted migration approval") from error
    import re
    if (type(record["operator_uid"]) is not int or record["operator_uid"] != os.getuid()
            or not isinstance(record["operator"], str) or not record["operator"].strip() or len(record["operator"]) > 200
            or not isinstance(record["approval_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", record["approval_id"])
            or any(type(record[key]) not in (float, int) or not math.isfinite(record[key]) for key in ("approved_at", "expires_at"))
            or record["expires_at"] != record["approved_at"] + LIFETIME_SECONDS):
        raise access.MaintenanceError("Invalid migration operator, approval ID or lifetime")
    identity = record["identity"]
    from data.resource_storage import relative_path
    required = {"project", "device", "inode", "operation", "plan", "plan_sha256", "plan_revision", "plan_stat",
                "identity_allocation", "identity_allocation_sha256", "allocation_stat", "recovery_source",
                "fixture_only", "operations", "required_bytes", "required_inodes"}
    if not isinstance(identity, dict) or not required.issubset(identity):
        raise access.MaintenanceError("Incomplete approved migration identity")
    if (not isinstance(identity["operation"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", identity["operation"])
            or identity["plan_revision"] != "2B.3d" or type(identity["fixture_only"]) is not bool
            or any(type(identity[key]) is not int or identity[key] < 0 for key in ("device", "inode", "required_bytes", "required_inodes"))
            or any(not isinstance(identity[key], list) or len(identity[key]) != 5
                   or any(type(item) is not int or item < 0 for item in identity[key]) for key in ("plan_stat", "allocation_stat"))
            or any(not isinstance(identity[key], str) or not re.fullmatch(r"[a-f0-9]{64}", identity[key])
                   for key in ("plan_sha256", "identity_allocation_sha256"))
            or not isinstance(identity["recovery_source"], dict) or not isinstance(identity["operations"], dict)
            or any(type(count) is not int or count < 0 for count in identity["operations"].values())):
        raise access.MaintenanceError("Malformed approved migration identity")
    for key in ("plan", "identity_allocation"):
        if not isinstance(identity[key], str) or relative_path(project, identity[key]) != identity[key]:
            raise access.MaintenanceError("Approval requires canonical relative evidence paths")
    if record["state"] in ("running", "completed", "rolled-back", "restore-required"):
        if (record.get("consumed_token") != state.get("token") or record.get("consumed_operation") != identity["operation"]
                or type(record.get("consumed_at")) not in (float, int) or not math.isfinite(record["consumed_at"])):
            raise access.MaintenanceError("Missing or mismatched consumed operation evidence")
    if not isinstance(identity, dict) or any(identity.get(key) != state.get(key) for key in ("project", "device", "inode")):
        raise access.MaintenanceError("Approval project filesystem identity differs")
    if str(project) != identity["project"] or record["enrollment"] != {key: state[key] for key in ("token", "control_inode", "control_device", "lock_inode")}:
        raise access.MaintenanceError("Approval enrollment identity differs")
    return record


def read(project, state=None):
    with access._root(project) as (project, root):
        state = state if state is not None else access._state(project, root)
        if state is None:
            return None
        if any(state.get(key) != value for key, value in access._identity(project, root).items()):
            raise access.MaintenanceError("Approval root was replaced")
        with access._control(root) as control:
            if not access._exists(control, RECORD):
                return None
            try:
                return _validate(access._read(control, RECORD), project, state)
            except (KeyError, TypeError, ValueError) as error:
                raise access.MaintenanceError("Malformed migration approval") from error


def _write(project, record, interrupt=None):
    value = {key: item for key, item in record.items() if key != "record_sha256"}
    value["record_sha256"] = seal(value)
    if interrupt:
        interrupt("before-approval-commit")
    with access._root(project) as (_project, root), access._control(root) as control:
        access._write(control, RECORD, value)
    if interrupt:
        interrupt("approval-committed")
    return value


@contextmanager
def _lock(project):
    with access._root(project) as (project, root), access._control(root) as control:
        state = access._state(project, root)
        descriptor = os.open("owner.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=control)
        try:
            access._acquire(descriptor, 0)
            access._check_path(project, root)
            if os.fstat(descriptor).st_ino != state["lock_inode"]:
                raise access.MaintenanceError("Approval owner lock was substituted")
            yield
        finally:
            os.close(descriptor)


def approve(project, plan, checksum, operation, reviewed_sha256, *, confirmed=False, operator=None, interrupt=None):
    if confirmed is not True or not operator or not operator.strip() or len(operator) > 200:
        raise access.MaintenanceError("Explicit confirmation and operator name required")
    proposed = proposal(project, plan, checksum, operation)
    identity = proposed["proposed_identity"]
    if not activated(identity):
        raise access.IncompleteCoverage("Exact production authorization activation is inactive; no enrollment or approval issued")
    if proposed["identity_sha256"] != reviewed_sha256:
        raise access.MaintenanceError("Reviewed migration identity changed; review again, never automatically reauthorize")
    if proposed["blocking_conditions"]:
        raise access.MaintenanceError("Migration preparation blocked: " + "; ".join(proposed["blocking_conditions"]))
    access.enroll(project, approved=True)
    with _lock(project):
        if read(project) is not None:
            raise access.MaintenanceError("An approval already exists; cancellation/completion cannot be reused")
        state = access.status(project)
        if state["state"] != "open" or state.get("operation"):
            raise access.MaintenanceError("Only an unused open project can be authorized")
        fresh = _fresh_identity(identity)
        _capacity({"identity": identity, "state": "approved"}, fresh, "start")
        now = time.time()
        value = {"version": 1, "identity": identity, "identity_sha256": reviewed_sha256,
                 "approval_id": uuid4().hex, "state": "approved", "approved_at": now,
                 "expires_at": now + LIFETIME_SECONDS, "operator_uid": os.getuid(), "operator": operator.strip(),
                 "enrollment": {key: state[key] for key in ("token", "control_inode", "control_device", "lock_inode")}}
        return _write(project, value, interrupt)


def inspect(project):
    try:
        record = read(project)
        if record is None:
            return {"state": "missing", "valid": False}
        valid = activated(record["identity"])
        state = record["state"]
        if state == "approved" and time.time() >= record["expires_at"]:
            state, valid = "expired", False
        if state in ("cancelled", "completed", "rolled-back", "restore-required"):
            valid = False
        if valid:
            _bounded_evidence(record["identity"])
        return {"state": state, "valid": valid, "approval_id": record["approval_id"],
                "identity_sha256": record["identity_sha256"], "operation": record["identity"]["operation"],
                "expires_at": record["expires_at"], "operator": record["operator"],
                "reason": None if valid else "Inactive, expired or terminal approval cannot start a migration"}
    except (OSError, ValueError, KeyError, TypeError, access.MaintenanceError) as error:
        return {"state": "blocked", "valid": False, "reason": str(error)}


def cancel(project, operation, reviewed_sha256, *, confirmed=False, plan=None, checksum=None, interrupt=None):
    if confirmed is not True:
        raise access.MaintenanceError("Separate cancellation confirmation required")
    with _lock(project):
        record = read(project)
        if record is None or record["identity"]["operation"] != operation or record["identity_sha256"] != reviewed_sha256:
            raise access.MaintenanceError("Cancellation requires the exact unused approval")
        if record["state"] != "approved" or access.status(project)["state"] != "open":
            raise access.MaintenanceError("Only an unused approval can be cancelled")
        if plan is not None:
            from data.resource_storage import relative_path
            if relative_path(project, plan) != record["identity"]["plan"] or checksum != record["identity"]["plan_sha256"]:
                raise access.MaintenanceError("Cancellation request names a different approved plan")
        if not activated(record["identity"]):
            raise access.IncompleteCoverage("Production activation is inactive; no approval changes permitted")
        return _write(project, {**record, "state": "cancelled", "cancelled_at": time.time()}, interrupt)


def require_owner(owner):
    state = owner.inspect()
    capability = _owners.get(owner)
    if capability is None:
        raise access.IncompleteCoverage("Live exact migration authorization owner required")
    record, fingerprint, action = capability
    if (not activated(record["identity"]) or record["state"] != "running"
            or record["identity"]["operation"] != owner.operation_id
            or record["enrollment"]["token"] != state["token"]
            or any(record["identity"][key] != state[key] for key in ("project", "device", "inode"))
            or _approval_fingerprint(owner.project) != fingerprint
            or state["state"] != "maintenance" and action != "release"):
        raise access.MaintenanceError("Migration approval or live ownership changed")
    return state


def require_plan(owner, plan, checksum, revision):
    from data.resource_storage import relative_path
    from services.migration_interface import _stat
    require_owner(owner)
    identity = _owners[owner][0]["identity"]
    if (relative_path(owner.project, plan) != identity["plan"] or checksum != identity["plan_sha256"]
            or revision != "2B.3d" or _stat(owner.project / identity["plan"]) != identity["plan_stat"]):
        raise access.MaintenanceError("Owner cannot execute a different approved migration plan")


def binding_for(project, plan, checksum, revision, operation):
    from data.resource_storage import relative_path
    record = read(project)
    if record is None or not activated(record["identity"]) or record["state"] not in ("running", "completed"):
        raise access.IncompleteCoverage("Integrated mutation requires allocated temporary fixtures or an exact consumed approval")
    identity = record["identity"]
    if (relative_path(project, plan) != identity["plan"] or checksum != identity["plan_sha256"]
            or revision != "2B.3d" or operation != identity["operation"]):
        raise access.MaintenanceError("Requested project, plan or operation differs from approval")
    return {"fixture_only": False, "approval_id": record["approval_id"],
            "approval_identity_sha256": record["identity_sha256"]}


def require_released(project, state, receipt):
    record = read(project, state)
    if (record is None or not activated(record["identity"]) or record["state"] not in ("running", "completed")
            or receipt.get("approval_id") != record["approval_id"]
            or receipt.get("approval_identity_sha256") != record["identity_sha256"]
            or receipt.get("operation") != record["identity"]["operation"]
            or record.get("consumed_token") != state.get("token")):
        raise access.MaintenanceError("Released project has no matching consumed migration approval")


def _bounded_evidence(identity):
    from services.migration_interface import _file, _stat, _backup
    project = Path(identity["project"])
    with access._root(project) as (_project, root):
        if any(identity[key] != value for key, value in access._identity(project, root).items()):
            raise access.MaintenanceError("Approved project filesystem identity changed")
    for path, expected in ((identity["plan"], identity["plan_stat"]),
                           (identity["identity_allocation"], identity["allocation_stat"])):
        if _stat(_file(project, path)) != expected:
            raise access.MaintenanceError("Approved plan or allocation filesystem identity changed")
    if not identity["fixture_only"] and _backup(project) != identity["recovery_source"]:
        raise access.MaintenanceError("Approved recovery source changed")


def _approval_fingerprint(project):
    info = os.stat(Path(project) / access.CONTROL / RECORD, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise access.MaintenanceError("Unsafe or substituted approval record")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _fresh_identity(identity):
    project = Path(identity["project"])
    actual = proposal(project, project / identity["plan"], identity["plan_sha256"], identity["operation"])
    if actual["proposed_identity"] != identity:
        raise access.MaintenanceError("Project, plan, allocation or recovery identity changed; no automatic reauthorization")
    blockers = [item for item in actual["blocking_conditions"] if not item.startswith("Insufficient additional")]
    if blockers:
        raise access.MaintenanceError("Migration preparation blocked: " + "; ".join(blockers))
    return actual


def _invocation(record, plan, checksum, operation, action):
    from data.resource_storage import relative_path
    identity = record["identity"]
    if not activated(identity):
        raise access.IncompleteCoverage("Production execution disabled: exact authorization activation is inactive; no maintenance entered")
    if (relative_path(identity["project"], plan) != identity["plan"] or checksum != identity["plan_sha256"]
            or operation != identity["operation"]):
        raise access.MaintenanceError("Requested project, operation or plan differs from the approved migration")
    if record["state"] == "approved":
        if time.time() >= record["expires_at"] or action not in ("start", "resume"):
            raise access.MaintenanceError("Unused approval is expired or this action requires a consumed operation")
    elif record["state"] == "running":
        if action == "start":
            raise access.MaintenanceError("Approval already consumed; use resume for this same operation")
    elif record["state"] != "completed" or action != "release":
        raise access.MaintenanceError("Terminal approval cannot be replayed")


def _capacity(record, ready, action):
    identity = record["identity"]
    if record["state"] != "approved" and action in ("validate", "accept", "release"):
        required, inodes = 1048576, 256
    elif record["state"] == "running":
        from services.resource_graph import remaining_storage_budget
        budget = remaining_storage_budget(identity["project"], identity, record["enrollment"], action=action,
                                          approval_id=record["approval_id"], approval_identity_sha256=record["identity_sha256"])
        ready["recovery_capacity"] = budget
        required, inodes = budget["peak_additional_bytes"], budget["additional_inode_bound"]
    else:
        required, inodes = max(1048576, identity["required_bytes"]), max(256, identity["required_inodes"])
    if ready["available_bytes"] < required or ready["available_inodes"] < inodes:
        raise access.MaintenanceError("Insufficient remaining-operation capacity; no cleanup or automatic continuation authorized")


def execute(project, plan, checksum, operation, action, *, confirmed=False, clients_stopped=False, interrupt=None):
    from services import resource_integration as integrated
    from services.maintenance_preflight import check_known_clients, check_quiescence
    if confirmed is not True or clients_stopped is not True:
        raise access.MaintenanceError("Separate action confirmation and stopped Crossing clients are required")
    if action not in ("start", "resume", "rollback", "validate", "accept", "release"):
        raise access.MaintenanceError("Unknown approved migration action")
    record = read(project)
    if record is None:
        raise access.IncompleteCoverage("Production execution disabled: no durable approved migration; no maintenance entered")
    _invocation(record, plan, checksum, operation, action)
    ready = _fresh_identity(record["identity"])
    _capacity(record, ready, action)
    check_known_clients(project, clients_stopped=True)
    state = access.status(project)
    if state.get("operation") and state["operation"] != operation:
        raise access.MaintenanceError("Existing maintenance belongs to another operation")
    if record["state"] == "running" and state["state"] == "open" and action != "release":
        raise access.MaintenanceError("Released migration permits only completion of release evidence")
    if action == "release" and state["state"] == "open" and state.get("operation") == operation:
        with _lock(project):
            record = read(project)
            integrated.release_fixture_migration(project, plan, approved_digest=checksum, plan_identity="2B.3d",
                                                operation_id=operation, approved=True, clients_stopped=True)
            if record["state"] == "running":
                _write(project, {**record, "state": "completed", "completed_at": time.time()}, interrupt)
        return {"state": "released", "maintenance_released": True, "already_released": True}
    with access.maintenance(project, operation, approved=True, interrupt=interrupt) as owner:
        record = read(project)
        _invocation(record, plan, checksum, operation, action)
        ready = _fresh_identity(record["identity"])
        _capacity(record, ready, action)
        check_quiescence(owner, clients_stopped=True)
        if record["state"] == "approved":
            record = _write(project, {**record, "state": "running", "consumed_at": time.time(),
                            "consumed_token": owner.inspect()["token"], "consumed_operation": operation}, interrupt)
        _owners[owner] = record, _approval_fingerprint(owner.project), action
        try:
            project, binding = integrated._binding(project, plan, checksum, "2B.3d", operation)
            if action in ("start", "resume"):
                result = integrated.run_owned_migration(owner, plan, binding, interrupt)
                if action == "resume":
                    result["outcome"] = "resume"
            elif action == "rollback":
                result = integrated.rollback_owned_migration(owner, binding, interrupt)
            elif action == "validate":
                result = integrated._verify_graph(owner, integrated._load(owner, binding), interrupt)
            elif action == "accept":
                result = integrated.accept_owned_migration(owner, binding, interrupt)
            else:
                result = integrated.release_owned_migration(owner, binding, interrupt)
            terminal = "completed" if result.get("maintenance_released") else result.get("outcome")
            if terminal in ("completed", "rollback", "restore-required"):
                _write(project, {**record, "state": "rolled-back" if terminal == "rollback" else terminal,
                                 "completed_at": time.time()}, interrupt)
            if "recovery_capacity" in ready:
                result["recovery_capacity"] = ready["recovery_capacity"]
            return result
        finally:
            _owners.pop(owner, None)