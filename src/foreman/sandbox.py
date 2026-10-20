"""Where executor and validation commands run.

`DockerRuntime` is the real thing: one container per step, the task clone
mounted at /workspace, the run's control directory (prompt, configs) mounted
read-only at /foreman, nothing else from the host.

`LocalRuntime` runs the same commands as a host process group. It provides no
isolation and exists for tests and development only.

Every process started here is addressable by a *handle* string stored on the
TaskRun, so the supervisor can kill it without talking to the orchestrator:
    docker:<container name>   |   local:<process group id>
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from foreman.config import get_settings

WORKSPACE = "/workspace"
CTL = "/foreman"
CONTAINER_UID = 1000


@dataclass
class StepSpec:
    name: str  # unique, used as the container name
    workspace: Path  # host path of the task clone
    ctl_dir: Path  # host path of the run control dir
    command: list[str]  # may reference $FOREMAN_WORKSPACE / $FOREMAN_CTL
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    image: str = ""  # the project's toolchain image; "" = FOREMAN_EXECUTOR_IMAGE


class Proc:
    def __init__(self, handle: str, proc: asyncio.subprocess.Process):
        self.handle = handle
        self._proc = proc

    async def lines(self) -> AsyncIterator[str]:
        assert self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return
            yield line.decode(errors="replace").rstrip("\n")

    async def wait(self) -> int:
        return await self._proc.wait()


class LocalRuntime:
    name = "local"

    async def start(self, spec: StepSpec) -> Proc:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(spec.ctl_dir / "home"),
            "FOREMAN_WORKSPACE": str(spec.workspace),
            "FOREMAN_CTL": str(spec.ctl_dir),
            **spec.env,
        }
        (spec.ctl_dir / "home").mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *spec.command,
            cwd=spec.workspace,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group, so Stop kills the whole tree
        )
        return Proc(f"local:{proc.pid}", proc)


class DockerRuntime:
    name = "docker"

    def __init__(self) -> None:
        self.settings = get_settings()

    def _user(self, spec: StepSpec) -> str:
        if os.getuid() == 0:
            # Never run agents as root: hand the mounts to the image's `agent` user.
            for p in (spec.workspace, spec.ctl_dir):
                subprocess.run(
                    ["chown", "-R", f"{CONTAINER_UID}:{CONTAINER_UID}", str(p)], check=True
                )
            return f"{CONTAINER_UID}:{CONTAINER_UID}"
        return f"{os.getuid()}:{os.getgid()}"

    def docker_args(self, spec: StepSpec) -> list[str]:
        s = self.settings
        env = {
            "HOME": "/tmp/home",
            "FOREMAN_WORKSPACE": WORKSPACE,
            "FOREMAN_CTL": CTL,
            **spec.env,
        }
        args = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--name",
            spec.name,
            "--user",
            self._user(spec),
            "--cpus",
            s.container_cpus,
            "--memory",
            s.container_memory,
            "--pids-limit",
            "1024",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--add-host",
            "host.docker.internal:host-gateway",
            "-v",
            f"{spec.workspace}:{WORKSPACE}",
            "-v",
            f"{spec.ctl_dir}:{CTL}:ro",
            "-w",
            WORKSPACE,
            "--label",
            "foreman=1",
            "--label",
            f"foreman.instance={s.instance_id}",
        ]
        for k, v in spec.labels.items():
            args += ["--label", f"{k}={v}"]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        return [*args, spec.image or s.executor_image, *spec.command]

    async def start(self, spec: StepSpec) -> Proc:
        proc = await asyncio.create_subprocess_exec(
            *self.docker_args(spec),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")},
        )
        return Proc(f"docker:{spec.name}", proc)


Runtime = LocalRuntime | DockerRuntime


def get_runtime() -> Runtime:
    return LocalRuntime() if get_settings().runtime == "local" else DockerRuntime()


def kill_handle(handle: str, grace_s: float | None = None) -> str:
    """Terminate whatever `handle` names: SIGTERM, then SIGKILL after `grace_s`.

    Synchronous and self-contained so the supervisor can call it without any
    orchestrator state. Returns a short description of what happened.
    """
    grace = get_settings().stop_grace_s if grace_s is None else grace_s
    kind, _, ident = handle.partition(":")
    if kind == "docker" and ident:
        if not shutil.which("docker"):
            return "docker not available"
        proc = subprocess.run(
            ["docker", "stop", "-t", str(int(grace)), ident], capture_output=True, text=True
        )
        if proc.returncode != 0 and "No such container" not in proc.stderr:
            subprocess.run(["docker", "kill", ident], capture_output=True)
        return f"stopped container {ident}"
    if kind == "local" and ident:
        pgid = int(ident)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return "already exited"
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not group_alive(pgid):
                return f"terminated pgid {pgid}"
            time.sleep(0.1)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        return f"killed pgid {pgid}"
    return "no handle"


def group_alive(pgid: int) -> bool:
    """True if any non-zombie process is in the group. Zombies wait for their parent to
    reap them and count as dead here."""
    proc = Path("/proc")
    if not proc.is_dir():
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            fields = (d / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[2]) == pgid and fields[0] != "Z":
            return True
    return False


def running_containers() -> dict[str, str]:
    """This instance's running containers: name -> run id label."""
    if not shutil.which("docker"):
        return {}
    instance = get_settings().instance_id
    proc = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=foreman.instance={instance}",
            "--format",
            '{{.Names}}\t{{.Label "foreman.run"}}',
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return {}
    out = {}
    for line in proc.stdout.splitlines():
        name, _, run = line.partition("\t")
        out[name] = run
    return out
