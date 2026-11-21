"""Read-only known-client check for the single-operator offline window.

This snapshot is not resource-mutation authority or a general process inventory.
The operator must stop known jobs and automatic launchers before requesting it.
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


def _process(directory):
    before = _read(directory / "stat")
    fields = before.decode().rpartition(")")[2].split()
    if fields[0] in ("Z", "X", "x"):
        return None
    parent, started = int(fields[1]), int(fields[19])
    arguments = tuple(part.decode(errors="replace") for part in _read(directory / "cmdline").split(b"\0") if part)
    cwd = os.readlink(directory / "cwd")
    after = _read(directory / "stat").decode().rpartition(")")[2].split()
    if not arguments or (int(after[1]), int(after[19])) != (parent, started):
        raise MaintenanceError("Process identity changed during inspection")
    return {"pid": int(directory.name), "parent": parent, "started": started,
            "arguments": arguments, "cwd": cwd}


def _snapshot(known_pids):
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
                    raise MaintenanceError("Process inspection exceeded its bound")
                directory = _PROC_ROOT / entry.name
                try:
                    if directory.stat().st_uid != os.getuid() and int(entry.name) not in known_pids:
                        continue
                    process = _process(directory)
                    if process is not None:
                        processes[process["pid"]] = process
                except FileNotFoundError:
                    if directory.exists():
                        raise MaintenanceError(f"Process {entry.name} cannot be inspected")
    except (OSError, ValueError, IndexError, UnicodeError) as error:
        raise MaintenanceError("Known-client process inspection is unavailable") from error
    if time.monotonic() > deadline:
        raise MaintenanceError("Process inspection exceeded its bound")
    return processes


def _known_client(process, project):
    arguments = process["arguments"]
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


def check_known_clients(project, *, clients_stopped=False, known_pids=()):
    """Bounded pre-request snapshot; it grants no ownership or mutation authority."""
    if clients_stopped is not True:
        raise MaintenanceError("Explicit operator acknowledgement of stopped clients is required")
    project = str(Path(project).resolve())
    known_pids = tuple(islice(known_pids, _MAX_PROCESSES + 1))
    if len(known_pids) > _MAX_PROCESSES or any(type(pid) is not int or pid <= 0 or pid == os.getpid() for pid in known_pids):
        raise MaintenanceError("Known client PIDs must be bounded positive non-controller identities")
    processes = _snapshot(known_pids)
    clients = {pid for pid, process in processes.items() if pid != os.getpid()
               and (pid in known_pids or _known_client(process, project))}
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
            "mutation_authorized": False}


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