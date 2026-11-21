"""Persistent project admission and inspection-only maintenance ownership.

Lock order: maintenance owner, project directory lease, registry/catalog locks.
No graph-access bypass or production reopening capability is provided here.
"""

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps
import asyncio
import errno
import fcntl
import inspect
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4


CONTROL = ".crossing-maintenance"
ANCHOR = ".crossing-maintenance.enrolled"
ENROLLMENT_ATTRIBUTE = "user.crossing.maintenance"
CANONICAL_CONTEXT = "canonical-context.json"
_held = ContextVar("crossing_project_operations", default=())
_fixture_graph_owner = ContextVar("crossing_fixture_graph_owner", default=None)
_canonical_registry_check = ContextVar("crossing_canonical_registry_check", default=None)
_descriptors = set()
_active = set()
_rename_fixtures = set()


class MaintenanceError(RuntimeError):
    """Project admission or maintenance evidence is unsafe."""


class MaintenanceClosed(MaintenanceError):
    """Ordinary project access is closed."""


class MaintenanceBusy(MaintenanceError):
    """An operation or maintenance owner has not drained."""


class IncompleteCoverage(MaintenanceError):
    """Resource maintenance requires entry-point qualification not yet available."""


def _track(descriptor):
    _descriptors.add(descriptor)
    return descriptor


def _close(descriptor):
    _descriptors.discard(descriptor)
    os.close(descriptor)


def _after_fork():
    for descriptor in tuple(_descriptors):
        os.close(descriptor)
    _descriptors.clear()
    _active.clear()
    _held.set(())


os.register_at_fork(after_in_child=_after_fork)


@contextmanager
def _root(project):
    path = Path(os.path.abspath(os.fspath(project)))
    if path == Path("/"):
        raise MaintenanceError("Filesystem root is not a project")
    descriptor = _track(os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
    process = os.getpid()
    try:
        for component in path.parts[1:]:
            child = _track(os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                   dir_fd=descriptor))
            _close(descriptor)
            descriptor = child
    except OSError as error:
        _close(descriptor)
        raise MaintenanceError(f"Unsafe or unavailable project/control path: {path}") from error
    try:
        yield path, descriptor
    finally:
        if os.getpid() == process:
            _close(descriptor)


def _identity(path, descriptor):
    info = os.fstat(descriptor)
    return {"project": str(path), "device": info.st_dev, "inode": info.st_ino}


def _exists(descriptor, name):
    try:
        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _read(descriptor, name):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
    try:
        info = os.fstat(handle)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                or info.st_uid != os.getuid() or info.st_size > 65536):
            raise MaintenanceError("Unsafe maintenance evidence file")
        with os.fdopen(handle, "r", encoding="utf-8", closefd=False) as stream:
            value = json.load(stream)
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
            raise MaintenanceError("Unknown maintenance evidence version")
        return value
    finally:
        os.close(handle)


def _write(descriptor, name, value):
    from data.annotate import atomic_write_text

    path = Path(f"/proc/self/fd/{descriptor}") / name
    atomic_write_text(path, json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)
    os.fsync(descriptor)


@contextmanager
def _control(root):
    descriptor = _track(os.open(CONTROL, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=root))
    process = os.getpid()
    try:
        info = os.fstat(descriptor)
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise MaintenanceError("Maintenance control directory must be private")
        yield descriptor
    finally:
        if os.getpid() == process:
            _close(descriptor)


def _state(path, root):
    anchored, controlled = _exists(root, ANCHOR), _exists(root, CONTROL)
    try:
        enrollment = os.getxattr(root, ENROLLMENT_ATTRIBUTE).decode("ascii")
    except OSError as error:
        if error.errno not in (errno.ENODATA, errno.ENOTSUP):
            raise MaintenanceError("Cannot validate project enrollment") from error
        enrollment = None
    except UnicodeError as error:
        raise MaintenanceError("Corrupt project enrollment attribute") from error
    if not anchored and not controlled and enrollment is None:
        return None
    try:
        if not anchored or not controlled or enrollment is None:
            raise MaintenanceError("Enrolled maintenance evidence is missing")
        anchor = _read(root, ANCHOR)
        if anchor.get("token") != enrollment:
            raise MaintenanceError("Project enrollment attribute mismatch")
        identity = _identity(path, root)
        if any(anchor.get(key) != value for key, value in identity.items()):
            raise MaintenanceError("Maintenance project identity mismatch")
        with _control(root) as control:
            info = os.fstat(control)
            lock_info = os.stat("owner.lock", dir_fd=control, follow_symlinks=False)
            if (anchor.get("control_inode") != info.st_ino or anchor.get("control_device") != info.st_dev
                    or anchor.get("lock_inode") != lock_info.st_ino or not stat.S_ISREG(lock_info.st_mode)
                    or lock_info.st_nlink != 1 or lock_info.st_mode & 0o077 or lock_info.st_uid != os.getuid()):
                raise MaintenanceError("Maintenance control/lock identity changed")
            state = _read(control, "state.json")
            if any(state.get(key) != value for key, value in anchor.items()):
                raise MaintenanceError("Maintenance state binding mismatch")
            if state.get("state") not in ("open", "maintenance-requested", "maintenance"):
                raise MaintenanceError("Unknown maintenance state")
            if state["state"] == "open" and state.get("operation"):
                from services.resource_integration import RECEIPT, released_evidence
                if not _exists(control, RECEIPT):
                    raise MaintenanceError("Missing fixture release evidence")
                released_evidence(path, root, state, _read(control, RECEIPT))
            if _exists(control, "request.json"):
                request = _read(control, "request.json")
                if (any(request.get(key) != value for key, value in anchor.items())
                        or not isinstance(request.get("operation"), str) or not request["operation"]):
                    raise MaintenanceError("Maintenance request binding mismatch")
                if state["state"] == "open":
                    state = {**state, "state": "maintenance-requested", "operation": request["operation"]}
                if state.get("operation") != request["operation"]:
                    raise MaintenanceError("Maintenance operation mismatch")
            elif state["state"] != "open":
                raise MaintenanceError("Maintenance request evidence is missing")
            return state
    except (OSError, ValueError, UnicodeError) as error:
        raise MaintenanceError("Missing or corrupt maintenance evidence; project remains closed") from error


def _check_path(path, root):
    with _root(path) as (current, descriptor):
        if _identity(current, descriptor) != _identity(path, root):
            raise MaintenanceError("Project path was substituted during admission")


def _execution():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return os.getpid(), threading.get_ident(), task


def status(project):
    """Read only maintenance control evidence, never the resource graph."""
    with _root(project) as (path, root):
        return _state(path, root) or {"state": "legacy", **_identity(path, root)}


def _authorized(key):
    return any(held_key == key and lease in _active for held_key, lease in _held.get())


@contextmanager
def independent_operations():
    """Keep event-loop callbacks independent of their synchronous launch operation."""
    token = _held.set(())
    try:
        yield
    finally:
        _held.reset(token)


@contextmanager
def operation(project):
    """Hold admission for one complete eager operation, including nested calls."""
    process = os.getpid()
    with _root(project) as (path, root):
        key = (_execution(), str(path), os.fstat(root).st_dev, os.fstat(root).st_ino)
        fixture_owner = _fixture_graph_owner.get()
        if fixture_owner is not None and str(fixture_owner.project) == str(path):
            state = fixture_owner.require_fixture_renames()
            if (state["device"], state["inode"]) != key[2:]:
                raise MaintenanceError("Fixture graph project identity changed")
            _check_path(path, root)
            yield path
            return
        if _authorized(key):
            _check_path(path, root)
            yield path
            return
        state = _state(path, root)
        if state is not None and state["state"] != "open":
            raise MaintenanceClosed(f"Project is in {state['state']}: {path}")
        try:
            fcntl.flock(root, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MaintenanceBusy("Exclusive project maintenance is active") from error
        token = None
        authorization = object()
        try:
            _check_path(path, root)
            state = _state(path, root)
            if state is not None and state["state"] != "open":
                raise MaintenanceClosed(f"Project is in {state['state']}: {path}")
            _active.add(authorization)
            token = _held.set((*_held.get(), (key, authorization)))
            yield path
        finally:
            if os.getpid() == process:
                _active.discard(authorization)
                if token is not None:
                    _held.reset(token)
                fcntl.flock(root, fcntl.LOCK_UN)


def project_operation(function):
    """Guard an eager public API with an explicit project/project_path parameter."""
    signature = inspect.signature(function)
    parameter = next((name for name in ("project_path", "project") if name in signature.parameters), None)
    if parameter is None or inspect.isgeneratorfunction(function) or inspect.iscoroutinefunction(function):
        raise TypeError("Project decorator requires an eager function with an explicit project")

    @wraps(function)
    def guarded(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs)
        arguments.apply_defaults()
        project = arguments.arguments[parameter]
        if project is None:
            return function(*args, **kwargs)
        with operation(project):
            if "registry" in signature.parameters and arguments.arguments.get("registry") is None:
                arguments.arguments["registry"] = canonical_registry(project)
            return function(*arguments.args, **arguments.kwargs)

    return guarded


def project_iterator(function):
    """Keep admission through iteration, including pauses between yielded values."""
    signature = inspect.signature(function)
    parameter = next((name for name in ("project_path", "project") if name in signature.parameters), None)
    if parameter is None or not inspect.isgeneratorfunction(function):
        raise TypeError("Project iterator requires a generator with an explicit project")

    @wraps(function)
    def guarded(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs)
        arguments.apply_defaults()
        execution = _execution()
        context = copy_context()
        context.run(_held.set, ())

        def iterate():
            with operation(arguments.arguments[parameter]):
                if "registry" in signature.parameters and arguments.arguments.get("registry") is None:
                    arguments.arguments["registry"] = canonical_registry(arguments.arguments[parameter])
                yield from function(*arguments.args, **arguments.kwargs)

        iterator = iterate()
        try:
            while True:
                if execution != _execution():
                    raise MaintenanceError("Project iterator cannot change execution owner")
                try:
                    value = context.run(next, iterator)
                except StopIteration:
                    return
                yield value
        finally:
            context.run(iterator.close)

    return guarded


def canonical_registry(project):
    """Resolve only a durable accepted context; this never grants admission or release."""
    with operation(project), _root(project) as (path, root):
        if not _exists(root, CONTROL):
            return None
        with _control(root) as control:
            if not _exists(control, CANONICAL_CONTEXT):
                state = _state(path, root)
                journal = path / CONTROL / ("graph-" + state["operation"] + ".sqlite3") if state and state.get("operation") else None
                if journal and journal.is_file():
                    import sqlite3
                    with sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True) as database:
                        if database.execute("SELECT 1 FROM sqlite_master WHERE name='acceptance'").fetchone():
                            row = database.execute("SELECT payload FROM acceptance").fetchone()
                            if row and json.loads(row[0]).get("canonical_context"):
                                raise MaintenanceError("Missing canonical consumer commitment")
                return None
            context = _read(control, CANONICAL_CONTEXT)
        if any(context.get(key) != value for key, value in _identity(path, root).items()):
            raise MaintenanceError("Canonical context project identity differs")
        owner = _fixture_graph_owner.get()
        released = False
        if owner is None:
            state = _state(path, root)
            if state and state["state"] == "open" and state.get("release_evidence"):
                from services.resource_integration import RECEIPT, released_evidence
                with _control(root) as control:
                    record = _read(control, RECEIPT)
                released_evidence(path, root, state, record)
                if record["acceptance"]["canonical_context"] != context:
                    raise MaintenanceError("Released canonical context differs")
                released = True
        if context.get("state") != "task3-qualified-awaiting-task4" or (owner is None and not released):
            raise MaintenanceClosed("Canonical context awaits Task 4 acceptance and release")
        if owner is not None:
            owner.require_fixture_renames()
        from data.resource_locations import project_file
        from data.resource_storage import digest
        import sqlite3
        registry = project_file(path, context["registry"])
        journal = project_file(path, context["journal"])
        with sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True) as database:
            rows = database.execute("SELECT payload FROM acceptance").fetchall()
            direction = database.execute("SELECT direction FROM meta").fetchone()[0]
            indexes = database.execute("SELECT source,after_sha FROM index_outputs WHERE state='installed' ORDER BY source").fetchall()
        if len(rows) != 1 or direction != "forward" or json.loads(rows[0][0]).get("canonical_context") != context or json.loads(rows[0][0]).get("graph_accepted") is not True:
            raise MaintenanceError("Unaccepted canonical graph commitment")
        info = registry.stat()
        fingerprint = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        checked = (_execution(), str(registry), fingerprint, context["registry_sha256"])
        if context["registry"] != "data/indexes/resources.sqlite3":
            raise MaintenanceError("Canonical registry commitment changed")
        if not released and _canonical_registry_check.get() != checked:
            if digest(registry) != context["registry_sha256"]:
                raise MaintenanceError("Canonical registry commitment changed")
            after = registry.stat()
            if fingerprint != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise MaintenanceError("Canonical registry changed during verification")
            _canonical_registry_check.set(checked)
        import hashlib
        graph = json.dumps([context["plan_sha256"], context["registry_sha256"], indexes], sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(indexes) != 15 or hashlib.sha256(graph.encode()).hexdigest() != context["graph_sha256"]:
            raise MaintenanceError("Canonical graph commitment changed")
        return registry


@contextmanager
def session(project):
    """Hold a lifetime lease without authorizing new callbacks as nested work."""
    process = os.getpid()
    with _root(project) as (path, root):
        state = _state(path, root)
        if state is not None and state["state"] != "open":
            raise MaintenanceClosed(f"Project is in {state['state']}: {path}")
        try:
            fcntl.flock(root, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MaintenanceBusy("Exclusive project maintenance is active") from error
        try:
            _check_path(path, root)
            state = _state(path, root)
            if state is not None and state["state"] != "open":
                raise MaintenanceClosed(f"Project is in {state['state']}: {path}")
            yield path
        finally:
            if os.getpid() == process:
                fcntl.flock(root, fcntl.LOCK_UN)


def enroll(project, *, approved=False):
    """Explicitly enroll a project; ordinary legacy admission never creates files."""
    if approved is not True:
        raise MaintenanceError("Explicit enrollment approval required")
    with _root(project) as (path, root):
        existing = _state(path, root)
        if existing is not None:
            return existing
        token = uuid4().hex
        os.setxattr(root, ENROLLMENT_ATTRIBUTE, token.encode("ascii"), flags=os.XATTR_CREATE)
        os.fsync(root)
        os.mkdir(CONTROL, mode=0o700, dir_fd=root)
        os.fsync(root)
        with _control(root) as control:
            lock = os.open("owner.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                           0o600, dir_fd=control)
            try:
                os.fsync(lock)
                anchor = {"version": 1, **_identity(path, root), "token": token,
                          "control_inode": os.fstat(control).st_ino, "control_device": os.fstat(control).st_dev,
                          "lock_inode": os.fstat(lock).st_ino}
                _write(root, ANCHOR, anchor)
                _write(control, "state.json", {**anchor, "state": "open"})
            finally:
                os.close(lock)
        return _state(path, root)


def _acquire(descriptor, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as error:
            if time.monotonic() >= deadline:
                raise MaintenanceBusy("Maintenance owner/active operations did not drain") from error
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))


class _InspectionOwner:
    def __init__(self, project, operation_id):
        self.project = project
        self.operation_id = operation_id
        self.execution = _execution()
        self.active = True

    def inspect(self):
        if not self.active or self.execution != _execution():
            raise MaintenanceError("Maintenance inspection capability is no longer owned")
        result = status(self.project)
        if result.get("operation") != self.operation_id:
            raise MaintenanceError("Maintenance inspection operation changed")
        return result

    def require_resource_mutation(self):
        self.inspect()
        raise IncompleteCoverage("Full entry-point exclusion is not qualified; resource maintenance is unavailable")

    def require_fixture_renames(self):
        """Authorize only a live, freshly allocated rename-qualification project."""
        state = self.inspect()
        if (state["project"], state["device"], state["inode"]) not in _rename_fixtures:
            raise IncompleteCoverage("Rename execution is available only in allocated temporary fixtures")
        return state


@contextmanager
def fixture_graph_access(owner):
    """Allow canonical owner calls only inside a live allocated-fixture owner."""
    if not isinstance(owner, _InspectionOwner):
        raise MaintenanceError("Exclusive fixture owner required")
    owner.require_fixture_renames()
    token = _fixture_graph_owner.set(owner)
    try:
        yield
    finally:
        _fixture_graph_owner.reset(token)


@contextmanager
def temporary_rename_project():
    """Allocate an empty private fixture; forked qualification workers inherit it."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="crossing-rename-fixture-", dir="/tmp") as directory:
        project = Path(directory)
        info = project.stat()
        identity = (str(project), info.st_dev, info.st_ino)
        _rename_fixtures.add(identity)
        try:
            yield project
        finally:
            _rename_fixtures.discard(identity)


@contextmanager
def maintenance(project, operation_id, *, approved=False, timeout=0, interrupt=None):
    """Request/drain exclusive inspection ownership; retain persistent closure on exit.

    This does not certify unguarded clients or authorize resource mutation/reopening.
    """
    if approved is not True or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", operation_id):
        raise MaintenanceError("Explicit maintenance approval and valid operation ID required")
    if not isinstance(timeout, (int, float)) or not 0 <= timeout <= 3600:
        raise MaintenanceError("Bounded nonnegative maintenance timeout required")
    with _root(project) as (path, root):
        key = (_execution(), str(path), os.fstat(root).st_dev, os.fstat(root).st_ino)
        if _authorized(key):
            raise MaintenanceError("Normal operation cannot upgrade into maintenance")
        state = _state(path, root)
        if state is None:
            raise MaintenanceError("Project must be explicitly enrolled before maintenance")
        with _control(root) as control:
            owner_lock = _track(os.open("owner.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=control))
            process = os.getpid()
            owner = None
            try:
                _acquire(owner_lock, timeout)
                _check_path(path, root)
                state = _state(path, root)
                if os.fstat(owner_lock).st_ino != state["lock_inode"]:
                    raise MaintenanceError("Maintenance owner lock was substituted")
                if state.get("operation", operation_id) != operation_id:
                    raise MaintenanceError("Resume requires the existing maintenance operation ID")
                request = {key: value for key, value in state.items() if key not in ("state", "operation", "pid", "scope")}
                request.update(operation=operation_id)
                if not _exists(control, "request.json"):
                    placeholder = os.open("request.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                          0o600, dir_fd=control)
                    try:
                        os.fsync(placeholder)
                    finally:
                        os.close(placeholder)
                    os.fsync(control)
                    _write(control, "request.json", request)
                pending = {**request, "state": "maintenance-requested", "pid": os.getpid(), "scope": "inspection-only"}
                _write(control, "state.json", pending)
                if interrupt is not None:
                    interrupt("maintenance-requested")
                    interrupt("active-operation-draining")
                _acquire(root, timeout)
                _check_path(path, root)
                _state(path, root)
                _write(control, "state.json", {**pending, "state": "maintenance"})
                owner = _InspectionOwner(path, operation_id)
                if interrupt is not None:
                    interrupt("maintenance-owned")
                yield owner
            finally:
                if owner is not None:
                    owner.active = False
                if os.getpid() == process:
                    fcntl.flock(root, fcntl.LOCK_UN)
                    fcntl.flock(owner_lock, fcntl.LOCK_UN)
                    _close(owner_lock)


def release_maintenance(project):
    """Production reopening requires the future qualified graph-acceptance owner."""
    raise MaintenanceError("Production maintenance release is unavailable until Task 4 acceptance qualification")


def _release_fixture(owner, *, interrupt=None):
    from services.resource_integration import RECEIPT, _verified_acceptance, _save
    from services import resource_rename as renames
    from data.resource_storage import digest

    state = owner.require_fixture_renames()
    record = _verified_acceptance(owner)
    record = _save(owner, record, "released", "released", interrupt)
    _verified_acceptance(owner)
    with _root(owner.project) as (_path, root), _control(root) as control:
        checksum = digest(owner.project / CONTROL / RECEIPT)
        _write(control, "state.json", {**state, "state": "open", "release_evidence": checksum})
        renames._event(interrupt, "release-state-published")
        _write(control, "state.json", state)
        _verified_acceptance(owner)
        _write(control, "state.json", {**state, "state": "open", "release_evidence": checksum})
        renames._rename(control, "request.json", control, "request.released-" + state["operation"])
        os.fsync(control)
        renames._event(interrupt, "release-request-retired")
        _state(owner.project, root)
    return {"state": "released", "maintenance_released": True, "production_authorized": False}
