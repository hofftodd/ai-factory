from pathlib import Path

from foreman.core.contextpack import (
    build_pack,
    build_role_pack,
    extract_interface,
    requirement_slice,
    size_violations,
)
from foreman.core.models import Project, Task
from tests.conftest import git

SPEC = """# Spec

## 6. Functional requirements

- FR-1.1 Users can add a todo with a title.
  Titles are 1-200 characters.
- FR-1.2 Users can list todos.
- FR-1.10 Users can delete todos.

## 7. Non-functional
- NFR-1 Responses under 100 ms.
"""


def test_requirement_slice():
    text, missing = requirement_slice(SPEC, ["FR-1.1", "NFR-1", "FR-9"])
    assert "Titles are 1-200 characters." in text
    assert "FR-1.2" not in text  # stops at the next requirement
    assert "FR-1.10" not in text  # FR-1.1 must not match FR-1.10
    assert "NFR-1 Responses" in text
    assert missing == ["FR-9"]


def test_python_interface_has_signatures_not_bodies():
    src = '''
import os

LIMIT = 10


class Store:
    """Keeps todos."""

    def add(self, title: str) -> int:
        secret_body = 1
        return secret_body

    @property
    def size(self) -> int:
        return 0


async def fetch(url: str,
                timeout: float = 1.0) -> bytes:
    """Fetch a URL."""
    return b""
'''
    iface = extract_interface("store.py", src)
    assert "class Store:" in iface and "def add(self, title: str) -> int:" in iface
    assert "@property" in iface and "LIMIT = 10" in iface
    assert "timeout: float = 1.0) -> bytes:" in iface
    assert '"""Fetch a URL."""' in iface
    assert "secret_body" not in iface


def test_other_languages_use_signature_lines():
    src = "import x from 'y'\nexport function add(a, b) {\n  return a + b\n}\n"
    assert extract_interface("m.ts", src) == "export function add(a, b) {"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    git("init", "-q", str(repo), cwd=tmp_path)
    (repo / "src" / "store.py").write_text("def add(title: str) -> int:\n    return 1\n")
    (repo / "src" / "api.py").write_text("ROUTES = []\n")
    (repo / "specs" / "001-initial").mkdir(parents=True)
    (repo / "specs" / "001-initial" / "spec.md").write_text(SPEC)
    git("add", "-A", cwd=repo)
    return repo


def test_pack_contents_and_budget(tmp_path):
    repo = _repo(tmp_path)
    project = Project(name="p", repo_url="x")
    task = Task(
        project_id=1,
        key="T1",
        title="Add endpoint",
        req_refs=["FR-1.1"],
        todos=["route POST /todos"],
        context_files=["src/store.py"],
        allowed_globs=["src/api.py"],
        validation_cmd="pytest -q tests/test_api.py",
    )
    pack = build_pack(project, task, repo, context_window=32768)
    assert "# Task T1: Add endpoint" in pack.text
    assert "Titles are 1-200 characters." in pack.text
    assert "def add(title: str) -> int:" in pack.text and "return 1" not in pack.text
    assert "Current `src/api.py`" in pack.text and "ROUTES = []" in pack.text
    assert pack.files == ["src/api.py"]
    # The executor's own prompt comes off the window first (pi is the default).
    assert pack.budget == (32768 - 1500) * 35 // 100 and not pack.over_budget
    assert size_violations(project, task, pack, ["src/api.py"]) == []


def test_size_violations(tmp_path):
    repo = _repo(tmp_path)
    project = Project(name="p", repo_url="x", max_task_files=2)
    task = Task(
        project_id=1,
        title="Too big",
        allowed_globs=["src/*.py", "src/new.py"],
        req_refs=["FR-404"],
        validation_cmd="",
    )
    (repo / "src" / "big.py").write_text(("x = " + "1" * 200 + "\n") * 1000)
    git("add", "-A", cwd=repo)
    pack = build_pack(project, task, repo, context_window=8192)
    files = ["src/api.py", "src/big.py", "src/store.py"]
    v = size_violations(project, task, pack, files)
    assert "no validation command" in v
    assert "touches 4 files (limit 2)" in v
    assert any("exceeds budget" in x for x in v)
    assert "unknown requirement ids: FR-404" in v


def test_setup_pack_has_the_whole_requirements(tmp_path):
    repo = _repo(tmp_path)
    project = Project(name="p", repo_url="x")
    task = Task(project_id=1, key="SETUP", title="Project setup", kind="setup", req_refs=[])
    for role in ("tester", "builder"):
        pack = build_role_pack(
            project,
            task,
            repo,
            role,
            docs={"requirements": "specs/001-initial/spec.md"},
            test_paths=["tests"],
        )
        assert "## Requirements (the whole project)" in pack.text
        assert "NFR-1 Responses under 100 ms." in pack.text
        assert "## Toolchain image" not in pack.text  # no environment prepared yet


def test_packs_show_the_toolchain_image(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".foreman").mkdir()
    (repo / ".foreman" / "check.sh").write_text("python3.12 --version\npytest --version\n")
    project = Project(name="p", repo_url="x")
    task = Task(project_id=1, key="T1", title="Add", req_refs=[])
    for role in ("tester", "builder"):
        pack = build_role_pack(project, task, repo, role, docs={}, test_paths=["tests"])
        assert "## Toolchain image" in pack.text and "pytest --version" in pack.text
        assert "`.foreman/` is read-only to you" in pack.text


def test_testers_see_how_the_project_runs_its_tests(tmp_path):
    repo = _repo(tmp_path)
    project = Project(
        name="p", repo_url="x", test_note="make test (pytest); CI runs pytest -n auto"
    )
    setup = Task(project_id=1, key="SETUP", title="Project setup", kind="setup", req_refs=[])
    feature = Task(project_id=1, key="T1", title="Add", req_refs=[])
    pack = build_role_pack(project, setup, repo, "tester", docs={}, test_paths=["tests"])
    assert "## How tests run in this project (from the user)" in pack.text
    assert "CI runs pytest -n auto" in pack.text and "thin wrapper" in pack.text
    pack = build_role_pack(project, feature, repo, "tester", docs={}, test_paths=["tests"])
    assert "CI runs pytest -n auto" in pack.text and "thin wrapper" not in pack.text
    pack = build_role_pack(project, feature, repo, "builder", docs={}, test_paths=["tests"])
    assert "from the user" not in pack.text  # the Builder runs tests/run.sh, nothing else
    plain = Project(name="p", repo_url="x")
    pack = build_role_pack(plain, setup, repo, "tester", docs={}, test_paths=["tests"])
    assert "from the user" not in pack.text
