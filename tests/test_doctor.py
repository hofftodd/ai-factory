"""`ai-factory doctor`: Docker as the service would reach it."""

from __future__ import annotations

import os
import pwd

import pytest

from ai_factory import doctor

ME = pwd.getpwuid(os.getuid()).pw_name
pytestmark = pytest.mark.skipif(os.getuid() == 0, reason="root can open any socket")


def test_env_file_is_read(tmp_path):
    f = tmp_path / "env"
    f.write_text("# comment\nAI_FACTORY_DOCKER_HOST='unix:///x.sock'\n\nexport DOCKER_HOST=y\n")
    env = doctor.read_env_file(f)
    assert env == {"AI_FACTORY_DOCKER_HOST": "unix:///x.sock", "DOCKER_HOST": "y"}
    assert doctor.service_docker_host(env) == "unix:///x.sock"
    assert doctor.read_env_file(tmp_path / "missing") == {}


def test_socket_permissions_against_the_users_groups(tmp_path):
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    sock.chmod(0o600)
    assert doctor.socket_access(sock, ME)[0]  # the owner
    os.chown(sock, -1, os.getgid())
    sock.chmod(0o060)
    assert doctor.socket_access(sock, ME)[0]  # via the group
    sock.chmod(0o000)
    ok, detail = doctor.socket_access(sock, ME)
    assert not ok and "mode 0" in detail
    assert not doctor.socket_access(tmp_path / "gone.sock", ME)[0]


def test_check_uses_the_service_env_and_hints_the_fix(tmp_path, capsys):
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    sock.chmod(0o000)
    env = tmp_path / "env"
    env.write_text(f"AI_FACTORY_DOCKER_HOST=unix://{sock}\n")
    r = doctor.Report()
    doctor.check_service_docker(r, env, ME)
    out = capsys.readouterr().out
    assert r.failed and "cannot use the Docker socket" in out
    assert "usermod -aG" in out and ME in out and "restart the ai-factory service" in out

    sock.chmod(0o660)
    r = doctor.Report()
    doctor.check_service_docker(r, env, ME)
    assert not r.failed and str(env) in capsys.readouterr().out

    env.write_text("AI_FACTORY_DOCKER_HOST=ssh://builder\n")
    r = doctor.Report()
    doctor.check_service_docker(r, env, ME)
    assert not r.failed  # not a local socket: nothing to check here
