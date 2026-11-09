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
