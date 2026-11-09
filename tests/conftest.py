from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_factory.db import reset_engine_cache


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("AI_FACTORY_DATA_DIR", str(data))
    monkeypatch.setenv("AI_FACTORY_RUNTIME", os.environ.get("TEST_AI_FACTORY_RUNTIME", "local"))
    monkeypatch.setenv("AI_FACTORY_POLL_INTERVAL_S", "0.1")
    monkeypatch.setenv("AI_FACTORY_STOP_GRACE_S", "2")
    monkeypatch.setenv("AI_FACTORY_GATEWAY_PORT", "0")  # ephemeral: tests run side by side
    monkeypatch.setenv("AI_FACTORY_NODE_CHECK_S", "0")  # tests call check_nodes() themselves
    monkeypatch.delenv("AI_FACTORY_TOKEN", raising=False)
    reset_engine_cache()
    from ai_factory import residency

    residency.reset()
    return data


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repo standing in for GitHub, with one commit on main."""
    bare = tmp_path / "origin.git"
    work = tmp_path / "seed"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(work), cwd=tmp_path)
    (work / "README.md").write_text("# demo\n")
    (work / "src").mkdir()
    (work / "src" / "app.py").write_text("VALUE = 1\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "init", cwd=work)
    git("remote", "add", "origin", str(bare), cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    shutil.rmtree(work)
    return bare


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from ai_factory.api.app import create_app

    with TestClient(create_app()) as c:
        yield c
