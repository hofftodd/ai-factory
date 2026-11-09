"""The Lima VM ai-factory runs its containers in on macOS (`ai-factory vm ...`).

ai-factory's own processes run natively on the Mac; only executor, validation and
image-build containers run in the VM. The VM is created from Lima's
`docker-rootful` template with two changes:

- the only host directory it can see is AI_FACTORY_DATA_DIR, mounted writable at the
  same path (task clones are bind-mounted into containers by host path, so the paths
  must match; nothing else from your home directory is visible);
- `vz` + virtiofs for fast file access.

Rootful dockerd keeps file ownership simple: containers run as your uid, which is
the same uid inside the VM, so files written to /workspace belong to you on the Mac.

The Docker socket is forwarded to ~/.lima/ai-factory/sock/docker.sock, which
`ai_factory.engine` picks up automatically.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

from ai_factory.config import get_settings
from ai_factory.engine import LIMA_INSTANCE, lima_socket

TEMPLATE = "template://docker-rootful"


class VMError(RuntimeError):
    pass


def _limactl() -> str:
    exe = shutil.which("limactl")
    if not exe:
        raise VMError("limactl not found: install Lima first (`brew install lima`)")
    return exe


def _host_memory_gib() -> int:
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
        return int(out.stdout.strip()) // 2**30
    except (OSError, ValueError):
        return 8


def default_size() -> tuple[int, int]:
    """(cpus, memory GiB): the machine minus headroom for macOS and the model servers."""
    s = get_settings()
    cpus = s.vm_cpus or max(2, (os.cpu_count() or 4) - 2)
    mem = s.vm_memory_gib or max(4, min(_host_memory_gib() // 4, 32))
    return cpus, mem


def instance() -> dict | None:
    """Lima's description of the instance, or None if it doesn't exist."""
    proc = subprocess.run(
        [_limactl(), "list", "--json", LIMA_INSTANCE], capture_output=True, text=True
    )
    for line in proc.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("name") == LIMA_INSTANCE:
            return data
    return None


def mounted_paths(inst: dict) -> list[tuple[str, bool]]:
    mounts = (inst.get("config") or {}).get("mounts") or []
    return [(m.get("location", ""), bool(m.get("writable"))) for m in mounts]


def check_mount(inst: dict) -> str | None:
    """None if the data dir is mounted writable in the VM, else what's wrong."""
    data = str(get_settings().data_dir)
    for location, writable in mounted_paths(inst):
        loc = os.path.expanduser(location)
        if data == loc or data.startswith(loc.rstrip("/") + "/"):
            return None if writable else f"{loc} is mounted read-only in the VM"
    return (
        f"the VM does not mount {data}; it was created for another AI_FACTORY_DATA_DIR "
        "(run `ai-factory vm delete` then `ai-factory vm start`)"
    )


def _run(*args: str) -> None:
    proc = subprocess.run([_limactl(), *args])
    if proc.returncode != 0:
        raise VMError(f"limactl {args[0]} failed (exit {proc.returncode})")


def _wait_for_docker(timeout: float = 180) -> None:
    sock = lima_socket()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock.exists():
            proc = subprocess.run(
                ["docker", "-H", f"unix://{sock}", "info"], capture_output=True, text=True
            )
            if proc.returncode == 0:
                return
        time.sleep(2)
    raise VMError(f"Docker in the VM did not come up within {timeout:.0f}s")


def start() -> None:
    data = get_settings().data_dir
    data.mkdir(parents=True, exist_ok=True)
    inst = instance()
    if inst is None:
        cpus, mem = default_size()
        mounts = json.dumps([{"location": str(data), "writable": True}])
        print(f"creating Lima VM '{LIMA_INSTANCE}': {cpus} CPUs, {mem} GiB, mounts {data}")
        _run(
            "create",
            f"--name={LIMA_INSTANCE}",
            "--tty=false",
            "--vm-type=vz",
            "--mount-type=virtiofs",
            f"--cpus={cpus}",
            f"--memory={mem}",
            "--set",
            f".mounts = {mounts}",
            TEMPLATE,
        )
        inst = instance()
        if inst is None:
            raise VMError("the VM was not created")
    problem = check_mount(inst)
    if problem:
        raise VMError(problem)
    if inst.get("status") != "Running":
        _run("start", "--tty=false", LIMA_INSTANCE)
    _wait_for_docker()
    print(f"VM running; Docker at unix://{lima_socket()}")


def stop() -> None:
    if instance() is not None:
        _run("stop", LIMA_INSTANCE)


def delete() -> None:
    if instance() is not None:
        _run("delete", "--force", LIMA_INSTANCE)


def status() -> int:
    inst = instance()
    if inst is None:
        print(f"no VM '{LIMA_INSTANCE}' (create it with `ai-factory vm start`)")
        return 1
    print(f"VM '{LIMA_INSTANCE}': {inst.get('status')}")
    print(f"  cpus {inst.get('cpus')}, memory {int(inst.get('memory', 0)) // 2**30} GiB")
    for location, writable in mounted_paths(inst):
        print(f"  mount {location} ({'rw' if writable else 'ro'})")
    problem = check_mount(inst)
    if problem:
        print(f"  problem: {problem}")
    print(f"  DOCKER_HOST=unix://{lima_socket()}")
    return 0 if inst.get("status") == "Running" and not problem else 1


def shell() -> int:
    return subprocess.call([_limactl(), "shell", LIMA_INSTANCE])


def main(args: list[str]) -> None:
    actions = {"start": start, "stop": stop, "delete": delete, "status": status, "shell": shell}
    if len(args) != 1 or args[0] not in actions:
        sys.exit(f"usage: ai-factory vm {{{','.join(actions)}}}")
    try:
        code = actions[args[0]]()
    except VMError as e:
        sys.exit(f"ai-factory vm {args[0]}: {e}")
    sys.exit(code or 0)
