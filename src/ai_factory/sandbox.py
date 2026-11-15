"""Where executor and validation commands run.

`DockerRuntime` is the real thing: one container per step, the task clone
mounted at /workspace, the run's control directory (prompt, configs) mounted
read-only at /ai-factory, nothing else from the host.

Containers run on one of two networks (`StepSpec.network`, from the project):

- "isolated" (default): an internal Docker network with no route anywhere. The only
  thing on it besides executors is a relay container (`ai-factory-gw`) that forwards
  one port to the model gateway, so an agent can reach its model only through the
  gateway's budgets and loop detection - not the LAN, model servers or the internet.
- "open": the default bridge plus `host.docker.internal`, for projects whose tests
  need the network.

The Docker arguments, the network, the relay and the output reader are
`ai_common.sandbox`'s; this module supplies ai-factory's names, labels, mounts and limits.

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
from dataclasses import dataclass, field
from pathlib import Path

from ai_common import sandbox as common
from ai_common.sandbox import CONTAINER_UID, RELAY_PORT, WORKSPACE, Proc, RunSpec

from ai_factory.config import get_settings
from ai_factory.engine import detect, docker, docker_env

__all__ = ["CONTAINER_UID", "RELAY_PORT", "WORKSPACE", "Proc", "local_proc"]

CTL = "/ai-factory"
RESULTS = "/ai-factory-results"  # where the test runner writes junit.xml and coverage
NETWORKS = ("isolated", "open")
RELAY_ALIAS = "ai-factory-gw"
RELAY_URL = f"http://{RELAY_ALIAS}:{RELAY_PORT}"
# Output lines past this are cut with a note (a coding agent's JSON events can carry whole
# files on one line; ai_common.sandbox.Proc reads any length, this bounds memory).
MAX_LINE_BYTES = common.MAX_LINE_BYTES
CUT_LABEL = "ai-factory"


@dataclass
class StepSpec:
    name: str  # unique, used as the container name
    workspace: Path  # host path of the task clone
    ctl_dir: Path  # host path of the run control dir
    command: list[str]  # may reference $AI_FACTORY_WORKSPACE / $AI_FACTORY_CTL
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    image: str = ""  # the project's toolchain image; "" = AI_FACTORY_EXECUTOR_IMAGE
    network: str = "isolated"  # see module docstring
    # Role sandboxing (Docker only; the local runtime has no isolation):
    # the workspace mounted read-only (Tester), and workspace-relative directories
    # mounted over it read-only (Builder: the tests) or writable (Tester: the tests).
    workspace_readonly: bool = False
    readonly_dirs: list[str] = field(default_factory=list)
    writable_dirs: list[str] = field(default_factory=list)
    # Host directory for test results, mounted writable at $AI_FACTORY_RESULTS.
    results_dir: Path | None = None


def local_proc(proc: asyncio.subprocess.Process) -> Proc:
    """A host process started here, addressable as ``local:<pgid>``."""
    return Proc(proc, f"local:{proc.pid}", max_line_bytes=MAX_LINE_BYTES, cut_label=CUT_LABEL)


def base_labels() -> dict[str, str]:
    return {"ai-factory": "1", "ai-factory.instance": get_settings().instance_id}


class LocalRuntime:
    name = "local"

    async def start(self, spec: StepSpec) -> Proc:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(spec.ctl_dir / "home"),
            "AI_FACTORY_WORKSPACE": str(spec.workspace),
            "AI_FACTORY_CTL": str(spec.ctl_dir),
            **({"AI_FACTORY_RESULTS": str(spec.results_dir)} if spec.results_dir else {}),
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
        return local_proc(proc)


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

    def run_spec(self, spec: StepSpec) -> RunSpec:
        s = self.settings
        isolated = spec.network != "open"
        return RunSpec(
            name=spec.name,
            workspace=spec.workspace,
            ctl_dir=spec.ctl_dir,
            ctl_mount=CTL,
            image=spec.image or s.executor_image,
            command=spec.command,
            env={
                "AI_FACTORY_WORKSPACE": WORKSPACE,
                "AI_FACTORY_CTL": CTL,
                **({"AI_FACTORY_RESULTS": RESULTS} if spec.results_dir else {}),
                **spec.env,
            },
            labels={**base_labels(), **spec.labels},
            user=self._user(spec),
            cpus=s.container_cpus,
            memory=s.container_memory,
            network=exec_network() if isolated else "",
            add_host="" if isolated else (detect().host_ip or "host-gateway"),
            workspace_readonly=spec.workspace_readonly,
            readonly_dirs=spec.readonly_dirs,
            writable_dirs=spec.writable_dirs,
            results_dir=spec.results_dir,
            results_mount=RESULTS if spec.results_dir else "",
        )

    def docker_args(self, spec: StepSpec) -> list[str]:
        return common.docker_args(self.run_spec(spec))

    async def start(self, spec: StepSpec) -> Proc:
        env = docker_env(
            {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
        )
        # Stop goes through kill_handle (docker stop with a grace period), not Proc.kill.
        return await common.start(
            self.run_spec(spec), env=env, max_line_bytes=MAX_LINE_BYTES, cut_label=CUT_LABEL
        )


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
            ["docker", "stop", "-t", str(int(grace)), ident],
            capture_output=True,
            text=True,
            env=docker_env(),
        )
        if proc.returncode != 0 and "No such container" not in proc.stderr:
            subprocess.run(["docker", "kill", ident], capture_output=True, env=docker_env())
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
        # Darwin answers EPERM for a group that holds only zombies.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        return f"killed pgid {pgid}"
    return "no handle"


def alive_in_group(pgid: int) -> list[int]:
    """Pids of the non-zombie processes in a process group. Zombies wait for their
    parent to reap them and count as dead here.

    Linux reads /proc; other platforms (macOS) ask `ps`. If neither works, falls back
    to probing the group with signal 0, which cannot tell zombies apart."""
    proc = Path("/proc")
    if proc.is_dir():
        alive = []
        for d in proc.iterdir():
            if not d.name.isdigit():
                continue
            try:
                fields = (d / "stat").read_text().rsplit(")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if int(fields[2]) == pgid and fields[0] != "Z":
                alive.append(int(d.name))
        return alive
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            # Darwin: EPERM when the group holds only zombies.
            return []
        return [pgid]
    alive = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        if int(parts[1]) == pgid and not parts[2].startswith("Z"):
            alive.append(int(parts[0]))
    return alive


def group_alive(pgid: int) -> bool:
    """True if any non-zombie process is in the group."""
    return bool(alive_in_group(pgid))


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
            f"label=ai-factory.instance={instance}",
            "--format",
            '{{.Names}}\t{{.Label "ai-factory.run"}}',
        ],
        capture_output=True,
        text=True,
        env=docker_env(),
    )
    if proc.returncode != 0:
        return {}
    out = {}
    for line in proc.stdout.splitlines():
        name, _, run = line.partition("\t")
        out[name] = run
    return out


# -- isolated executor network ----------------------------------------------

IsolationError = common.SandboxError


def exec_network() -> str:
    return f"ai-factory-exec-{get_settings().instance_id}"


def relay_name() -> str:
    return f"ai-factory-gw-{get_settings().instance_id}"


def ensure_exec_network() -> None:
    """Create the internal executor network if it doesn't exist."""
    common.ensure_network(exec_network(), base_labels(), run=docker)


def ensure_relay(gateway_port: int) -> None:
    """Make sure the gateway relay runs and forwards to `gateway_port`.

    The relay sits on the default bridge (so it can reach the host) and on the
    executor network as `ai-factory-gw`. It carries no `ai-factory.run` label, so the
    supervisor's orphan sweep and Stop leave it alone; it holds no state."""
    ensure_exec_network()  # with the network's own labels, before the relay's
    relay = common.Relay(
        name=relay_name(),
        alias=RELAY_ALIAS,
        network=exec_network(),
        image=get_settings().executor_image,
        labels={**base_labels(), "ai-factory.role": "gateway-relay"},
        user=f"{CONTAINER_UID}:{CONTAINER_UID}",
        host_ip=detect().host_ip or "host-gateway",
        port_label="ai-factory.gateway_port",
        timeout=60,
    )
    common.ensure_relay(relay, gateway_port, run=docker)


def teardown_isolation() -> None:
    """Remove the relay and the executor network (best effort; on orchestrator exit).
    The network stays if executor containers are still attached to it."""
    common.teardown(relay_name(), exec_network(), run=docker)
