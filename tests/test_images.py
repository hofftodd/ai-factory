"""Per-project toolchain images, without Docker (the build itself is in test_docker)."""

from __future__ import annotations

from pathlib import Path

from foreman import images
from foreman.core.models import TaskState
from foreman.orchestrator.main import Orchestrator
from foreman.sandbox import DockerRuntime, StepSpec
from tests.conftest import git
from tests.helpers import TERMINAL, drive, make_project, make_task, task


def add_to_origin(origin: Path, tmp_path: Path, files: dict[str, str]) -> None:
    work = tmp_path / "push"
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    for rel, content in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(content)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "toolchain", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)


def test_tag_follows_dockerfile_and_base_image():
    tag = images.image_tag(1, "FROM foreman/executor-base\n", "sha256:a")
    assert tag.startswith("foreman/project-") and tag == images.image_tag(
        1, "FROM foreman/executor-base\n", "sha256:a"
    )
    assert tag != images.image_tag(1, "FROM foreman/executor-base\nRUN x\n", "sha256:a")
    assert tag != images.image_tag(1, "FROM foreman/executor-base\n", "sha256:b")
    assert tag != images.image_tag(2, "FROM foreman/executor-base\n", "sha256:a")


def test_project_dockerfile(tmp_path):
    assert images.project_dockerfile(tmp_path) is None
    (tmp_path / ".foreman").mkdir()
    (tmp_path / ".foreman" / "Dockerfile").write_text("FROM x\n")
    assert images.project_dockerfile(tmp_path) == "FROM x\n"


def test_step_image_overrides_the_base_image(tmp_path):
    spec = StepSpec(name="n", workspace=tmp_path, ctl_dir=tmp_path, command=["true"])
    base = DockerRuntime().docker_args(spec)
    assert base[-2] == "foreman/executor-base:latest"
    spec.image = "foreman/project-x-1:abc"
    assert DockerRuntime().docker_args(spec)[-2] == "foreman/project-x-1:abc"


async def test_local_runtime_ignores_the_dockerfile(client, origin, tmp_path):
    add_to_origin(origin, tmp_path, {".foreman/Dockerfile": "FROM nothing-to-build\n"})
    p = make_project(client, origin)
    t = make_task(client, p["id"], "echo hi > out.txt", validation="test -f out.txt")
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason
