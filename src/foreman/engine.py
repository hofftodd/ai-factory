"""The container engine executors run on, and how containers reach this host.

Foreman only drives the `docker` CLI, so any Docker-compatible engine works:

- Linux: dockerd on this machine.
- macOS: a Linux VM running dockerd, preferably the Lima VM Foreman manages itself
  (`foreman vm start`, see `foreman.vm`), or Docker Desktop, OrbStack, colima.

Two things differ between them and are resolved here:

- the Docker socket (`DOCKER_HOST`): `FOREMAN_DOCKER_HOST`, else the caller's
  `DOCKER_HOST`, else the managed Lima VM's socket when it exists, else the docker
  CLI's default context;
- the host as seen from a container. Executors always reach the model gateway as
  `host.docker.internal`; Foreman maps that name with `--add-host` to `host-gateway`
  (Linux, Docker Desktop, OrbStack) or to Lima's host address, since on Lima
  `host-gateway` is the VM, not the Mac. The gateway then only needs to listen where
  that address arrives: the docker bridge on Linux, loopback on macOS.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from foreman.config import get_settings

log = logging.getLogger("foreman.engine")

HOST_ALIAS = "host.docker.internal"
# Lima's user-mode network: the gateway address forwards to the host's loopback.
LIMA_HOST_IP = "192.168.5.2"
LIMA_INSTANCE = "foreman"


def lima_dir() -> Path:
    return Path(os.environ.get("LIMA_HOME", Path.home() / ".lima")) / LIMA_INSTANCE


def lima_socket() -> Path:
    return lima_dir() / "sock" / "docker.sock"


def docker_host() -> str:
    """The DOCKER_HOST every docker command should use; "" = the CLI's default."""
    configured = get_settings().docker_host or os.environ.get("DOCKER_HOST", "")
    if configured:
        return configured
    sock = lima_socket()
    return f"unix://{sock}" if sock.exists() else ""


def docker_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a docker subprocess: `base` (default: this process's
    environment) with DOCKER_HOST resolved."""
    env = dict(os.environ if base is None else base)
    host = docker_host()
    if host:
        env["DOCKER_HOST"] = host
    return env


def docker(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run a docker CLI command against the resolved engine and capture its output."""
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, env=docker_env(), timeout=timeout
    )


@dataclass(frozen=True)
class Engine:
    kind: str  # linux | lima | colima | docker-desktop | orbstack | other | unavailable
    docker_host: str
    host_ip: str  # --add-host target for host.docker.internal
    bind_host: str  # where the model gateway listens
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.kind != "unavailable"


def _classify(name: str, os_name: str, host: str) -> str:
    low = f"{name} {os_name}".lower()
    if "docker desktop" in low:
        return "docker-desktop"
    if "orbstack" in low:
        return "orbstack"
    if name.startswith("colima") or "colima" in host:
        return "colima"
    if name.startswith("lima-") or "/.lima/" in host:
        return "lima"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def _bridge_gateway() -> str:
    proc = docker(
        "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}", timeout=10
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


@functools.cache
def _detect(host: str) -> Engine:
    if not shutil.which("docker"):
        return Engine("unavailable", host, "", "127.0.0.1", "docker CLI not found")
    try:
        proc = docker("info", "--format", "{{.Name}}|{{.OperatingSystem}}", timeout=15)
    except subprocess.TimeoutExpired:
        return Engine("unavailable", host, "", "127.0.0.1", "docker info timed out")
    if proc.returncode != 0:
        return Engine("unavailable", host, "", "127.0.0.1", proc.stderr.strip()[:300])
    name, _, os_name = proc.stdout.strip().partition("|")
    kind = _classify(name, os_name, host)
    if kind in ("lima", "colima"):
        return Engine(kind, host, LIMA_HOST_IP, "127.0.0.1", os_name)
    if kind == "linux":
        # Containers arrive from the docker bridge; listen there, not on the LAN.
        bridge = _bridge_gateway()
        if not bridge:
            log.warning("could not find the docker bridge address; gateway binds 0.0.0.0")
        return Engine(kind, host, "host-gateway", bridge or "0.0.0.0", os_name)
    # Docker Desktop / OrbStack forward host.docker.internal to the host's loopback.
    return Engine(kind, host, "host-gateway", "127.0.0.1", os_name)


def detect() -> Engine:
    """The engine behind the resolved DOCKER_HOST, with settings overrides applied."""
    s = get_settings()
    engine = _detect(docker_host())
    host_ip = s.container_host_ip if s.container_host_ip != "auto" else engine.host_ip
    bind = s.gateway_host if s.gateway_host != "auto" else engine.bind_host
    return Engine(engine.kind, engine.docker_host, host_ip, bind, engine.detail)


def reset_cache() -> None:
    _detect.cache_clear()
