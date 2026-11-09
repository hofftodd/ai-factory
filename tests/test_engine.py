"""Container engine detection (ai_factory.engine) and the Lima VM helpers (ai_factory.vm)."""

from __future__ import annotations

import subprocess

import pytest

from ai_factory import engine, vm


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("AI_FACTORY_DOCKER_HOST", raising=False)
    monkeypatch.setenv("LIMA_HOME", str(tmp_path / "lima"))
    engine.reset_cache()
    yield
    engine.reset_cache()


def _fake_docker(monkeypatch, info: str, bridge: str = "172.17.0.1"):
    def run(*args, timeout=None):
        if args[0] == "info":
            return subprocess.CompletedProcess(args, 0, info, "")
        if args[:2] == ("network", "inspect"):
            return subprocess.CompletedProcess(args, 0, bridge + "\n", "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected")

    monkeypatch.setattr(engine, "docker", run)
    monkeypatch.setattr(engine.shutil, "which", lambda _: "/usr/bin/docker")


@pytest.mark.parametrize(
    ("info", "platform", "kind", "host_ip", "bind"),
    [
        ("lima-ai-factory|Ubuntu 24.04 LTS", "darwin", "lima", engine.LIMA_HOST_IP, "127.0.0.1"),
        ("colima|Ubuntu 24.04 LTS", "darwin", "colima", engine.LIMA_HOST_IP, "127.0.0.1"),
        ("docker-desktop|Docker Desktop", "darwin", "docker-desktop", "host-gateway", "127.0.0.1"),
        ("orbstack|OrbStack", "darwin", "orbstack", "host-gateway", "127.0.0.1"),
        ("minipc|Ubuntu 24.04 LTS", "linux", "linux", "host-gateway", "172.17.0.1"),
    ],
)
def test_detect(monkeypatch, info, platform, kind, host_ip, bind):
    _fake_docker(monkeypatch, info)
    monkeypatch.setattr(engine.sys, "platform", platform)
    eng = engine.detect()
    assert (eng.kind, eng.host_ip, eng.bind_host) == (kind, host_ip, bind)


def test_overrides(monkeypatch):
    _fake_docker(monkeypatch, "minipc|Ubuntu", bridge="172.17.0.1")
    monkeypatch.setattr(engine.sys, "platform", "linux")
    monkeypatch.setenv("AI_FACTORY_GATEWAY_HOST", "10.0.0.5")
    monkeypatch.setenv("AI_FACTORY_CONTAINER_HOST_IP", "10.0.0.5")
    eng = engine.detect()
    assert (eng.host_ip, eng.bind_host) == ("10.0.0.5", "10.0.0.5")


def test_unavailable_without_docker(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", lambda _: None)
    assert not engine.detect().available


def test_docker_host_resolution(monkeypatch, tmp_path):
    assert engine.docker_host() == ""
    sock = engine.lima_socket()
    sock.parent.mkdir(parents=True)
    sock.touch()
    assert engine.docker_host() == f"unix://{sock}"  # managed VM picked up
    monkeypatch.setenv("DOCKER_HOST", "unix:///other.sock")
    assert engine.docker_host() == "unix:///other.sock"
    monkeypatch.setenv("AI_FACTORY_DOCKER_HOST", "tcp://h:2375")
    assert engine.docker_host() == "tcp://h:2375"
    assert engine.docker_env({"PATH": "/bin"}) == {"PATH": "/bin", "DOCKER_HOST": "tcp://h:2375"}


def test_vm_mount_check(monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setenv("AI_FACTORY_DATA_DIR", str(data))
    ok = {"config": {"mounts": [{"location": str(data), "writable": True}]}}
    parent = {"config": {"mounts": [{"location": str(tmp_path), "writable": True}]}}
    ro = {"config": {"mounts": [{"location": str(data), "writable": False}]}}
    other = {"config": {"mounts": [{"location": "/elsewhere", "writable": True}]}}
    assert vm.check_mount(ok) is None
    assert vm.check_mount(parent) is None
    assert "read-only" in (vm.check_mount(ro) or "")
    assert "does not mount" in (vm.check_mount(other) or "")
