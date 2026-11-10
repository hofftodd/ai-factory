import pytest

from ai_factory import gitops
from tests.conftest import git


def test_clone_and_task_flow(origin):
    default = gitops.clone_project(1, str(origin), "ai-factory/m1")
    assert default == "main"
    repo = gitops.repo_dir(1)
    assert git("branch", "--show-current", cwd=repo).strip() == "ai-factory/m1"

    clone = gitops.prepare_task_clone(1, 7, "ai-factory/m1")
    # Standalone clone: real .git dir, no remote pointing back at the host repo.
    assert (clone / ".git").is_dir()
    assert git("remote", cwd=clone).strip() == ""
    assert git("branch", "--show-current", cwd=clone).strip() == "ai-factory/task-7"

    (clone / "src" / "app.py").write_text("VALUE = 2\n")
    (clone / "src" / "new.py").write_text("X = 1\n")
    assert gitops.changed_files(clone) == ["src/app.py", "src/new.py"]
    d = gitops.diff(clone)
    assert "+VALUE = 2" in d and "+X = 1" in d

    assert gitops.commit_all(clone, "task 7")
    assert not gitops.commit_all(clone, "nothing")
    assert gitops.changed_files(clone) == ["src/app.py", "src/new.py"]

    gitops.merge_task(1, 7, "change value")
    assert (repo / "src" / "app.py").read_text() == "VALUE = 2\n"
    assert "Merge task 7" in git("log", "-1", "--format=%s", cwd=repo)


def test_reuse_task_clone_keeps_work(origin):
    gitops.clone_project(1, str(origin), "ai-factory/m1")
    clone = gitops.prepare_task_clone(1, 1, "ai-factory/m1")
    (clone / "partial.txt").write_text("wip")
    assert gitops.prepare_task_clone(1, 1, "ai-factory/m1") == clone
    assert (clone / "partial.txt").exists()
    gitops.reset_task_clone(1, 1)
    assert not clone.exists()


def test_merge_conflict_is_reported_and_aborted(origin):
    gitops.clone_project(1, str(origin), "ai-factory/m1")
    a = gitops.prepare_task_clone(1, 1, "ai-factory/m1")
    b = gitops.prepare_task_clone(1, 2, "ai-factory/m1")
    (a / "src" / "app.py").write_text("VALUE = 'a'\n")
    (b / "src" / "app.py").write_text("VALUE = 'b'\n")
    gitops.commit_all(a, "a")
    gitops.commit_all(b, "b")
    gitops.merge_task(1, 1, "a")
    with pytest.raises(gitops.GitError, match="merge conflict"):
        gitops.merge_task(1, 2, "b")
    repo = gitops.repo_dir(1)
    assert git("status", "--porcelain", cwd=repo).strip() == ""


def test_out_of_scope():
    assert gitops.out_of_scope(["src/a.py", "docs/x.md"], ["src/*"]) == ["docs/x.md"]
    assert gitops.out_of_scope(["anything"], []) == []


def test_empty_repo_gets_root_commit(tmp_path):
    bare = tmp_path / "empty.git"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    gitops.clone_project(3, str(bare), "ai-factory/m1")
    assert gitops.has_commits(gitops.repo_dir(3))


def test_token_never_in_git_config(origin, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_GITHUB_TOKEN", "secret-token")
    args = gitops._auth_args("https://github.com/o/r.git")
    assert args and "secret-token" not in " ".join(args)  # base64, header only
    gitops.clone_project(1, str(origin), "ai-factory/m1")
    cfg = (gitops.repo_dir(1) / ".git" / "config").read_text()
    assert "secret" not in cfg and "extraheader" not in cfg


def _push_to_origin(origin, tmp_path, name, files):
    """Commit files to origin's main from a fresh clone, as someone using GitHub would."""
    work = tmp_path / name
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    for rel, content in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(content)
    git("add", "-A", cwd=work)
    git("-c", "user.name=u", "-c", "user.email=u@x", "commit", "-q", "-m", name, cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)


def test_sync_from_origin_merges_new_commits(origin, tmp_path):
    gitops.clone_project(1, str(origin), "ai-factory/m1")
    repo = gitops.repo_dir(1)
    assert gitops.sync_from_origin(1, str(origin), "main") == ("", "")
    gitops.write_and_commit(1, {"specs/001/requirements.md": "# R\n"}, "spec: draft")
    _push_to_origin(origin, tmp_path, "add-schema", {"schema/batchml.json": "{}\n"})
    done, problem = gitops.sync_from_origin(1, str(origin), "main")
    assert (done, problem) == ("merged 1 new commit from origin/main", "")
    assert git("branch", "--show-current", cwd=repo).strip() == "ai-factory/m1"
    assert (repo / "schema" / "batchml.json").exists()
    assert (repo / "specs" / "001" / "requirements.md").exists()
    assert gitops.sync_from_origin(1, str(origin), "main") == ("", "")


def test_sync_from_origin_backs_out_of_a_conflict(origin, tmp_path):
    gitops.clone_project(1, str(origin), "ai-factory/m1")
    repo = gitops.repo_dir(1)
    gitops.write_and_commit(1, {"README.md": "# ours\n"}, "spec: readme")
    before = gitops.head_commit(1)
    _push_to_origin(origin, tmp_path, "theirs", {"README.md": "# theirs\n"})
    done, problem = gitops.sync_from_origin(1, str(origin), "main")
    assert done == "" and "merging origin/main failed" in problem
    assert gitops.head_commit(1) == before
    assert git("status", "--porcelain", cwd=repo).strip() == ""
    assert (repo / "README.md").read_text() == "# ours\n"


def test_sync_from_an_origin_that_was_empty_at_clone(tmp_path):
    bare = tmp_path / "empty.git"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    gitops.clone_project(1, str(bare), "ai-factory/m1")  # ai-factory makes its own root
    assert gitops.sync_from_origin(1, str(bare), "main") == ("", "")  # nothing pushed yet
    _push_to_origin(bare, tmp_path, "first", {"schema/a.json": "{}\n"})
    done, _ = gitops.sync_from_origin(1, str(bare), "main")
    assert done == "merged 1 new commit from origin/main"
    assert (gitops.repo_dir(1) / "schema" / "a.json").exists()


@pytest.mark.parametrize(
    "path,generated",
    [
        ("backend/__pycache__/db.cpython-312.pyc", True),
        ("x.pyc", True),
        ("frontend/node_modules/react/index.js", True),
        (".pytest_cache/v/cache/lastfailed", True),
        (".coverage", True),
        ("backend/db.py", False),
        ("tests/backend/test_db.py", False),
        ("docs/pycache-notes.md", False),
    ],
)
def test_generated_files(path, generated):
    assert gitops.is_generated(path) is generated


def _with_tracked_bytecode(origin, tmp_path):
    """An origin whose main already tracks bytecode, as future-lab's did."""
    work = tmp_path / "seed2"
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    (work / "src" / "__pycache__").mkdir()
    (work / "src" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00old")
    git("add", "-A", cwd=work)
    git("-c", "user.name=u", "-c", "user.email=u@x", "commit", "-q", "-m", "pyc", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)


def test_task_commits_never_include_generated_files(origin, tmp_path):
    _with_tracked_bytecode(origin, tmp_path)
    gitops.clone_project(1, str(origin), "ai-factory/m1")
    clone = gitops.prepare_task_clone(1, 7, "ai-factory/m1")
    (clone / "src" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00new")
    (clone / "src" / "__pycache__" / "extra.cpython-312.pyc").write_bytes(b"\x00")
    (clone / "src" / "app.py").write_text("VALUE = 2\n")
    assert gitops.commit_all(clone, "task 7")
    tracked = git("ls-files", cwd=clone).split()
    assert "src/app.py" in tracked
    assert not [f for f in tracked if "__pycache__" in f]  # untracked, not deleted
    assert (clone / "src" / "__pycache__" / "app.cpython-312.pyc").exists()


def test_parallel_tasks_touching_bytecode_merge_cleanly(origin, tmp_path):
    _with_tracked_bytecode(origin, tmp_path)
    gitops.clone_project(1, str(origin), "ai-factory/m1")
    a = gitops.prepare_task_clone(1, 1, "ai-factory/m1")
    b = gitops.prepare_task_clone(1, 2, "ai-factory/m1")
    # Both tasks run the code; B is still on an older ai-factory clone that commits the
    # bytecode it changed (as future-lab's T27 did), A stops tracking it.
    (a / "src" / "a.py").write_text("A = 1\n")
    (a / "src" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00from-a")
    assert gitops.commit_all(a, "task 1")
    gitops.merge_task(1, 1, "A")
    (b / "src" / "b.py").write_text("B = 1\n")
    (b / "src" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00from-b")
    git("add", "-A", cwd=b)
    git("-c", "user.name=u", "-c", "user.email=u@x", "commit", "-q", "-m", "old-style", cwd=b)
    # Bringing A's work into B conflicts only in bytecode: resolved, not refused.
    assert gitops.sync_task_clone(1, 2, "ai-factory/m1")
    gitops.merge_task(1, 2, "B")
    repo = gitops.repo_dir(1)
    assert (repo / "src" / "a.py").exists() and (repo / "src" / "b.py").exists()
    assert not [f for f in git("ls-files", cwd=repo).split() if "__pycache__" in f]


def test_real_conflicts_are_still_refused(origin, tmp_path):
    gitops.clone_project(1, str(origin), "ai-factory/m1")
    a = gitops.prepare_task_clone(1, 1, "ai-factory/m1")
    b = gitops.prepare_task_clone(1, 2, "ai-factory/m1")
    (a / "src" / "app.py").write_text("VALUE = 'a'\n")
    gitops.commit_all(a, "a")
    gitops.merge_task(1, 1, "A")
    (b / "src" / "app.py").write_text("VALUE = 'b'\n")
    gitops.commit_all(b, "b")
    with pytest.raises(gitops.MergeConflict) as e:
        gitops.sync_task_clone(1, 2, "ai-factory/m1")
    assert e.value.files == ["src/app.py"]
