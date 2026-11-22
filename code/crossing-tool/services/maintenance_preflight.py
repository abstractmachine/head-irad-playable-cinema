"""Read-only known-client check for the single-operator offline window.

This snapshot is not resource-mutation authority or a general process inventory.
The operator must stop known jobs and automatic launchers before requesting it.
Unavailable OS inspection is diagnostic under the public single-operator check;
operator acknowledgement, exclusive ownership and resource checks remain gates.
"""

import os
from itertools import islice
from pathlib import Path
import time

from tool.project_access import MaintenanceBusy, MaintenanceError


_PROC_ROOT = Path("/proc")
_MAX_PROCESSES = 4096
_MAX_BYTES = 16384
_MAX_SECONDS = 5


def _read(path):
    with path.open("rb") as stream:
        value = stream.read(_MAX_BYTES + 1)
    if len(value) > _MAX_BYTES:
        raise MaintenanceError("Process inspection exceeded its byte limit")
    return value


def _user_manager_identity(directory):
    info = directory.stat()
    metadata = _read(directory / "stat").decode()
    fields = metadata.rpartition(")")[2].split()
    arguments = _read(directory / "cmdline")
    group = _read(directory / "cgroup")
    uid = os.getuid()
    expected_group = f"0::/user.slice/user-{uid}.slice/user@{uid}.service/init.scope"
    if (int(metadata.split("(", 1)[0]) != int(directory.name)
            or metadata.partition("(")[2].rpartition(")")[0] != "systemd"
            or fields[0] in ("Z", "X", "x") or int(fields[1]) != 1
            or info.st_uid != uid
            or arguments not in (b"/usr/lib/systemd/systemd\0--user\0", b"/lib/systemd/systemd\0--user\0")
            or group.decode().splitlines() != [expected_group]):
        raise MaintenanceError("Unverified user systemd parent identity")
    return (int(directory.name), info.st_uid, int(fields[1]), int(fields[19]), arguments, group)


def _process(directory):
    before = _read(directory / "stat")
    fields = before.decode().rpartition(")")[2].split()
    if fields[0] in ("Z", "X", "x"):
        return None
    parent, started = int(fields[1]), int(fields[19])
    arguments = tuple(part.decode(errors="replace") for part in _read(directory / "cmdline").split(b"\0") if part)
    manager = False
    helper = False
    parent_identity = None
    try:
        cwd = os.readlink(directory / "cwd")
    except PermissionError:
        uid = os.getuid()
        expected_group = f"0::/user.slice/user-{uid}.slice/user@{uid}.service/init.scope"
        comm = before.decode().partition("(")[2].rpartition(")")[0]
        if (directory.stat().st_uid != uid
                or _read(directory / "cgroup").decode().splitlines() != [expected_group]):
            raise
        if (comm == "systemd" and parent == 1
                and arguments in (("/usr/lib/systemd/systemd", "--user"), ("/lib/systemd/systemd", "--user"))):
            manager = True
        elif comm == "(sd-pam)" and arguments == ("(sd-pam)",) and parent > 1:
            parent_identity = _user_manager_identity(directory.parent / str(parent))
            if parent_identity[3] > started:
                raise MaintenanceError("User systemd parent started after its helper")
            helper = True
        else:
            raise
        if (_read(directory / "cmdline") != b"\0".join(argument.encode() for argument in arguments) + b"\0"
                or _read(directory / "cgroup").decode().splitlines() != [expected_group]):
            raise MaintenanceError("Process identity changed during inspection")
        cwd = None
    after_stat = _read(directory / "stat").decode()
    after = after_stat.rpartition(")")[2].split()
    if (not arguments or (int(after[1]), int(after[19])) != (parent, started)
            or (manager or helper) and (
                int(before.decode().split("(", 1)[0]) != int(directory.name)
                or
                int(after_stat.split("(", 1)[0]) != int(directory.name)
                or after_stat.partition("(")[2].rpartition(")")[0] != before.decode().partition("(")[2].rpartition(")")[0]
                or directory.stat().st_uid != os.getuid())):
        raise MaintenanceError("Process identity changed during inspection")
    if helper and _user_manager_identity(directory.parent / str(parent)) != parent_identity:
        raise MaintenanceError("User systemd parent identity changed during helper inspection")
    return {"pid": int(directory.name), "parent": parent, "started": started,
            "arguments": arguments, "cwd": cwd, "unrelated_user_manager": manager,
            "unrelated_sd_pam": helper, "verified_parent_identity": parent_identity}


def _diagnostic_process(directory):
    arguments = tuple(part.decode(errors="replace") for part in _read(directory / "cmdline").split(b"\0") if part)
    parent, started = 0, None
    try:
        metadata = _read(directory / "stat").decode().rpartition(")")[2].split()
        parent, started = int(metadata[1]), int(metadata[19])
    except (OSError, ValueError, IndexError, UnicodeError, MaintenanceError):
        pass
    return {"pid": int(directory.name), "parent": parent, "started": started,
            "arguments": arguments, "cwd": "", "partial_inspection": True}


def _snapshot(known_pids, *, diagnostics=None):
    processes = {}
    deadline = time.monotonic() + _MAX_SECONDS
    try:
        with os.scandir(_PROC_ROOT) as entries:
            count = 0
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                count += 1
                if count > _MAX_PROCESSES or time.monotonic() > deadline:
                    if diagnostics is not None:
                        diagnostics.append({"pid": None, "message": "Process inspection exceeded its bound"})
                        break
                    raise MaintenanceError("Process inspection exceeded its bound")
                directory = _PROC_ROOT / entry.name
                try:
                    if directory.stat().st_uid != os.getuid() and int(entry.name) not in known_pids:
                        continue
                    process = _process(directory)
                    if process is not None:
                        processes[process["pid"]] = process
                except (OSError, ValueError, IndexError, UnicodeError, MaintenanceError) as error:
                    if isinstance(error, FileNotFoundError) and not directory.exists():
                        continue
                    if diagnostics is None:
                        if isinstance(error, FileNotFoundError):
                            raise MaintenanceError(f"Process {entry.name} cannot be inspected") from error
                        raise
                    diagnostics.append({"pid": int(entry.name), "message": str(error)[:512]})
                    try:
                        process = _diagnostic_process(directory)
                    except (OSError, ValueError, IndexError, UnicodeError, MaintenanceError):
                        process = {"pid": int(entry.name), "parent": 0, "started": None,
                                   "arguments": (), "cwd": "", "partial_inspection": True}
                    processes[process["pid"]] = process
    except (OSError, ValueError, IndexError, UnicodeError, MaintenanceError) as error:
        if diagnostics is not None:
            diagnostics.append({"pid": None, "message": str(error)[:512]})
        elif isinstance(error, MaintenanceError):
            raise
        else:
            raise MaintenanceError("Known-client process inspection is unavailable") from error
    if time.monotonic() > deadline:
        if diagnostics is None:
            raise MaintenanceError("Process inspection exceeded its bound")
        diagnostics.append({"pid": None, "message": "Process inspection exceeded its bound"})
    if diagnostics:
        for pid in known_pids:
            if pid not in processes:
                try:
                    exited = _PROC_ROOT.is_dir() and not (_PROC_ROOT / str(pid)).exists()
                except OSError:
                    exited = False
                if exited:
                    continue
                processes[pid] = {"pid": pid, "parent": 0, "started": None,
                                  "arguments": (), "cwd": "", "partial_inspection": True}
    return processes


def _known_client(process, project):
    if process.get("unrelated_user_manager") or process.get("unrelated_sd_pam"):
        return False
    arguments = process["arguments"]
    if not arguments:
        return False
    names = {Path(argument).name for argument in arguments}
    if names & {"crossing", "crossing.py", "cli.py", "run_mcp.sh", "mcp_server.py"}:
        return True
    if any(name.endswith("_visualizer.py") for name in names):
        return True
    if "-m" in arguments:
        module = arguments[arguments.index("-m") + 1:][:1]
        if module and (module[0] in ("cli", "mcp_server.mcp_server") or module[0].startswith(("services.", "visualizers."))):
            return True
    roots = (Path(project), Path(__file__).resolve().parents[1])
    cwd = Path(process["cwd"].removesuffix(" (deleted)"))
    project_context = any(cwd.is_relative_to(root) for root in roots) or cwd.name == "crossing-tool"
    project_context = project_context or any(argument == str(root) or argument.startswith(str(root) + "/")
                                             for root in roots for argument in arguments)
    executable = Path(arguments[0]).name
    return project_context and (executable.startswith("python") or executable in {"ffmpeg", "ffplay", "mpv", "vlc"})


def _controller_launcher(processes):
    controller = processes.get(os.getpid())
    if not controller or controller.get("partial_inspection"):
        return None
    parent = processes.get(controller["parent"])
    if not parent or parent.get("partial_inspection"):
        return None
    arguments, launched = parent["arguments"], controller["arguments"]
    if (len(arguments) < 3 or len(launched) < 2
            or Path(arguments[0]).name != "uv" or arguments[1] != "run"
            or not Path(launched[0]).name.startswith("python")
            or parent["started"] > controller["started"]):
        return None
    if (Path(launched[1]).name == "crossing"
            and arguments[2] in ("crossing", launched[1])):
        forwarded = arguments[3:]
    elif (Path(launched[1]).name == "cli.py" and len(arguments) >= 4
            and arguments[2] == "python" and arguments[3] == launched[1]):
        forwarded = arguments[4:]
    else:
        return None
    if forwarded != launched[2:]:
        return None
    try:
        for process in (controller, parent):
            if _process(_PROC_ROOT / str(process["pid"])) != process:
                return None
    except (OSError, ValueError, IndexError, UnicodeError, MaintenanceError):
        return None
    return parent["pid"]


def check_known_clients(project, *, clients_stopped=False, known_pids=()):
    """Refuse known Crossing jobs; unrelated survey failures are diagnostic."""
    if clients_stopped is not True:
        raise MaintenanceError("Explicit operator acknowledgement of stopped clients is required")
    project = str(Path(project).resolve())
    known_pids = tuple(islice(known_pids, _MAX_PROCESSES + 1))
    if len(known_pids) > _MAX_PROCESSES or any(type(pid) is not int or pid <= 0 or pid == os.getpid() for pid in known_pids):
        raise MaintenanceError("Known client PIDs must be bounded positive non-controller identities")
    diagnostics = []
    processes = _snapshot(known_pids, diagnostics=diagnostics)
    launcher = _controller_launcher(processes)
    clients = {pid for pid, process in processes.items() if pid != os.getpid()
               and (pid in known_pids or pid != launcher and _known_client(process, project))}
    pending = list(clients)
    while pending:
        parent = pending.pop()
        for pid, process in processes.items():
            if pid != os.getpid() and pid not in clients and process["parent"] == parent:
                clients.add(pid)
                pending.append(pid)
    if clients:
        identities = ", ".join(str(pid) for pid in sorted(clients)[:16])
        raise MaintenanceBusy(f"Known Crossing clients or descendants remain: {identities}")
    return {"project": project, "checked_processes": len(processes), "scope": "single-operator-known-clients",
            "mutation_authorized": False, "survey_complete": not diagnostics,
            "diagnostics": diagnostics}


def check_quiescence(owner, *, clients_stopped=False, known_pids=()):
    """Recheck known clients under live exclusive ownership before physical work."""
    try:
        state = owner.inspect()
    except AttributeError as error:
        raise MaintenanceError("Live exclusive inspection ownership is required") from error
    if state.get("state") != "maintenance":
        raise MaintenanceError("Live exclusive inspection ownership is required")
    result = check_known_clients(state["project"], clients_stopped=clients_stopped, known_pids=known_pids)
    owner.inspect()
    return {**result, "operation": state["operation"]}