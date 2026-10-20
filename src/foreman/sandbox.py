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
RESULTS = "/foreman-results"  # where the test runner writes junit.xml and coverage
CONTAINER_UID = 1000


@dataclass
class StepSpec:
    name: str  # unique, used as the container name
    workspace: Path  # host path of the task clone
    ctl_dir: Path  # host path of the run control dir
    command: list[str]  # may reference $FOREMAN_WORKSPACE / $FOREMAN_CTL
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    # "gateway": internal network, only the model gateway reachable (default).
    # "open": normal bridge network with internet access.
    network: str = "gateway"
    # Image override (per-project toolchain image); empty = the executor base image.
    image: str = ""
    # Role sandboxing (Docker only; the local runtime has no isolation):
    # the workspace mounted read-only (Tester), and workspace-relative directories
    # mounted over it read-only (Builder: the tests) or writable (Tester: the tests).
    workspace_readonly: bool = False
    readonly_dirs: list[str] = field(default_factory=list)
    writable_dirs: list[str] = field(default_factory=list)
    # Host directory for test results, mounted writable at $FOREMAN_RESULTS.
    results_dir: Path | None = None


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
            **({"FOREMAN_RESULTS": str(spec.results_dir)} if spec.results_dir else {}),
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
        for rel in [*spec.readonly_dirs, *spec.writable_dirs]:
            (spec.workspace / rel).mkdir(parents=True, exist_ok=True)  # mount points
        if os.getuid() == 0:
            # Never run agents as root: hand the mounts to the image's `agent` user.
            paths = [spec.workspace, spec.ctl_dir]
            if spec.results_dir:
                paths.append(spec.results_dir)
            for p in paths:
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
            **({"FOREMAN_RESULTS": RESULTS} if spec.results_dir else {}),
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
            *self._network_args(spec),
            *self._mounts(spec),
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

    def _mounts(self, spec: StepSpec) -> list[str]:
        ws = f"{spec.workspace}:{WORKSPACE}" + (":ro" if spec.workspace_readonly else "")
        out = ["-v", ws, "-v", f"{spec.ctl_dir}:{CTL}:ro"]
        for rel, mode in [*((d, "ro") for d in spec.readonly_dirs),
                          *((d, "rw") for d in spec.writable_dirs)]:
            out += ["-v", f"{spec.workspace / rel}:{WORKSPACE}/{rel}:{mode}"]
        if spec.results_dir:
            out += ["-v", f"{spec.results_dir}:{RESULTS}"]
        return out

    def _network_args(self, spec: StepSpec) -> list[str]:
        if spec.network == "open":
            return ["--add-host", "host.docker.internal:host-gateway"]
        # No route out: only the relay (alias GATEWAY_ALIAS) is reachable.
        return ["--network", network_name()]

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


# -- egress lockdown ----------------------------------------------------------
#
# Executors run on an internal Docker network with no route anywhere. A small
# relay container joins both that network and the default bridge and forwards one
# port to the model gateway on the host. So an agent can reach its model, and
# nothing else: no internet, no LAN, no host services.

GATEWAY_ALIAS = "foreman-gateway"
RELAY_PORT = 8701


def network_name() -> str:
    return f"foreman-net-{get_settings().instance_id}"


def relay_name() -> str:
    return f"foreman-relay-{get_settings().instance_id}"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def ensure_egress_lockdown(gateway_port: int) -> str:
    """Create the internal network and the gateway relay. Returns the gateway URL
    executors on that network should use."""
    settings = get_settings()
    net, relay = network_name(), relay_name()
    if _docker("network", "inspect", net).returncode != 0:
        r = _docker("network", "create", "--internal", "--label", "foreman=1",
                    "--label", f"foreman.instance={settings.instance_id}", net)
        if r.returncode != 0 and "already exists" not in r.stderr:
            raise RuntimeError(f"cannot create network {net}: {r.stderr.strip()}")
    inspect = _docker("inspect", "-f",
                      '{{.State.Running}} {{index .Config.Labels "foreman.gateway_port"}}', relay)
    if inspect.returncode == 0 and inspect.stdout.split() != ["true", str(gateway_port)]:
        _docker("rm", "-f", relay)  # stale: wrong port or stopped
        inspect = _docker("inspect", relay)
    if inspect.returncode != 0:
        r = _docker(
            "run", "-d", "--name", relay, "--restart", "unless-stopped",
            "--user", f"{CONTAINER_UID}:{CONTAINER_UID}", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--memory", "64m", "--pids-limit", "64",
            "--add-host", "host.docker.internal:host-gateway",
            "--label", "foreman=1", "--label", f"foreman.instance={settings.instance_id}",
            "--label", "foreman.relay=1", "--label", f"foreman.gateway_port={gateway_port}",
            settings.executor_image,
            "socat", f"TCP-LISTEN:{RELAY_PORT},fork,reuseaddr",
            f"TCP:host.docker.internal:{gateway_port}",
        )
        if r.returncode != 0:
            raise RuntimeError(f"cannot start the gateway relay: {r.stderr.strip()}")
        r = _docker("network", "connect", "--alias", GATEWAY_ALIAS, net, relay)
        if r.returncode != 0 and "already exists" not in r.stderr:
            raise RuntimeError(f"cannot attach the relay to {net}: {r.stderr.strip()}")
    return f"http://{GATEWAY_ALIAS}:{RELAY_PORT}"


def remove_egress_lockdown() -> None:
    """Remove this installation's relay and network (uninstall / tests)."""
    _docker("rm", "-f", relay_name())
    _docker("network", "rm", network_name())


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
        if name != relay_name():  # the relay is infrastructure, not a run
            out[name] = run
    return out
