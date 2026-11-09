"""Read-only repository tools for the planners, and the tool loop that runs them."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_factory import llm as llm_mod
from ai_factory import repotools
from ai_factory.core.models import Node, Project
from ai_factory.llm import stream_chat, tools_offered
from ai_factory.planner import chat_system_prompt, decompose_system_prompt
from tests.fakellm import FakeLLM


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "schema").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / ".ai-factory").mkdir()
    (root / ".ai-factory" / "Dockerfile").write_text("FROM ai-factory/executor-base\n")
    (root / "README.md").write_text("# demo\nA BatchML tool.\n")
    lines = [f'"Type{i}": {{"properties": {{}}}},' for i in range(3000)]
    (root / "schema" / "big.json").write_text("{\n" + "\n".join(lines) + "\n}\n")
    (root / "logo.png").write_bytes(b"\x89PNG\0\0binary")
    (tmp_path / "secret.txt").write_text("outside\n")
    os.symlink(tmp_path / "secret.txt", root / "escape.txt")
    return root


def test_list_files_shows_folders_and_sizes_but_not_git(repo):
    out = repotools.list_files(repo, "")
    assert "schema/" in out and ".ai-factory/" in out and "README.md (" in out
    assert ".git" not in out.replace(".ai-factory", "")
    assert repotools.list_files(repo, "./schema").startswith("schema/big.json")


def test_read_file_pages_through_a_large_file(repo):
    first = repotools.read_file(repo, "schema/big.json", 1, 50)
    assert first.startswith("schema/big.json, lines 1-50 of 3002")
    assert '2: "Type0"' in first and "start_line=51" in first
    later = repotools.read_file(repo, "schema/big.json", 2990, 400)
    assert "lines 2990-3002 of 3002" in later and "more:" not in later
    # Never more than MAX_LINES at once, however many are asked for.
    capped = repotools.read_file(repo, "schema/big.json", 1, 100_000)
    assert len(capped.splitlines()) <= repotools.MAX_LINES + 2
    assert len(capped) <= repotools.MAX_CHARS + 500
    dockerfile = repotools.read_file(repo, ".ai-factory/Dockerfile")
    assert dockerfile.endswith("FROM ai-factory/executor-base")


def test_search_repo_finds_lines(repo):
    out = repotools.search_repo(repo, "type2999")
    assert out == 'schema/big.json:3001: "Type2999": {"properties": {}},'
    assert repotools.search_repo(repo, "batchml", "README.md") == "README.md:2: A BatchML tool."
    many = repotools.search_repo(repo, "properties")
    assert "more than 100 matches" in many
    assert repotools.search_repo(repo, "nowhere-to-be-found") == "no matches"


@pytest.mark.parametrize(
    "name,args",
    [
        ("read_file", {"path": "../secret.txt"}),
        ("read_file", {"path": "/etc/passwd"}),
        ("read_file", {"path": "escape.txt"}),
        ("read_file", {"path": ".git/config"}),
        ("list_files", {"path": ".git"}),
        ("search_repo", {"text": "outside", "path": ".."}),
        ("read_file", {"path": "logo.png"}),
        ("read_file", {"path": "missing.md"}),
        ("read_file", {"path": "schema"}),
    ],
)
def test_tools_refuse_what_they_must_not_read(repo, name, args):
    out = repotools.run_tool(repo, name, args)
    assert out.startswith(f"{name} failed:"), out
    assert "outside\n" not in out and "[core]" not in out


def test_search_skips_symlinks_out_of_the_repo(repo):
    assert repotools.search_repo(repo, "outside") == "no matches"


@pytest.fixture
def llm():
    server = FakeLLM(host="127.0.0.1", planner=lambda r: "The schema has 3000 types.")
    server.planner_after_tools = True
    server.start()
    yield server
    server.stop()


async def test_local_planner_reads_the_repository(llm, repo, monkeypatch):
    monkeypatch.delenv("AI_FACTORY_SEARCH_URL", raising=False)
    llm.tool_script = [
        ("search_repo", {"text": "Type2999"}),
        ("read_file", {"path": "schema/big.json", "start_line": 3000, "max_lines": 2}),
    ]
    node = Node(name="local", base_url=llm.url, context_window=32768)
    seen: list[str] = []
    result = await stream_chat(
        node, "m", "S", [{"role": "user", "content": "x"}], repo=repo, on_research=seen.append
    )
    assert result.text == "The schema has 3000 types."
    assert seen == [
        "searched repo: Type2999",
        "read file: schema/big.json (from line 3000)",
    ]
    first, last = llm.requests[0], llm.requests[-1]
    # No search service: only the repository tools are offered.
    assert {t["function"]["name"] for t in first["tools"]} == set(repotools.NAMES)
    tool_msgs = [m for m in last["messages"] if m["role"] == "tool"]
    assert "schema/big.json:3001" in tool_msgs[0]["content"]
    assert tool_msgs[1]["content"].startswith("schema/big.json, lines 3000-3001")


async def test_tool_rounds_end_with_an_answer_without_tools(llm, repo, monkeypatch):
    monkeypatch.setattr(llm_mod, "MAX_TOOL_ROUNDS", 3)
    llm.tool_script = [("list_files", {})] * 5  # a model that never stops looking
    node = Node(name="local", base_url=llm.url, context_window=32768)
    result = await stream_chat(node, "m", "S", [{"role": "user", "content": "x"}], repo=repo)
    assert len(llm.requests) == 3
    assert "tools" not in llm.requests[-1]
    assert llm.requests[-1]["messages"][-1] == {"role": "user", "content": llm_mod.ANSWER_NOW}
    assert result.text == "The schema has 3000 types."


async def test_full_context_window_stops_the_tools(llm, repo):
    # Each read adds ~6k tokens; a 20k window has room for one before it must answer.
    llm.tool_script = [("read_file", {"path": "schema/big.json", "max_lines": 1000})] * 4
    node = Node(name="local", base_url=llm.url, context_window=20000)
    await stream_chat(node, "m", "S", [{"role": "user", "content": "x"}], repo=repo)
    assert "tools" not in llm.requests[-1]
    assert len(llm.requests) < 4
    for req in llm.requests:  # every round fits what the window has left
        assert req["max_tokens"] <= node.context_window


async def test_claude_planner_reads_the_repository_with_client_tools(llm, repo):
    llm.tool_script = [("read_file", {"path": "README.md"})]
    node = Node(
        name="claude", provider="anthropic", base_url=llm.url.removesuffix("/v1"), api_key="k"
    )
    seen: list[str] = []
    result = await stream_chat(
        node,
        "claude-opus-5-5",
        "S",
        [{"role": "user", "content": "x"}],
        research=True,
        repo=repo,
        on_research=seen.append,
    )
    first, second = llm.requests[0], llm.requests[1]
    names = [t.get("name") for t in first["tools"]]
    assert names == ["web_search", "web_fetch", *repotools.NAMES]
    assert first["tools"][2]["input_schema"]["required"] == []
    assert seen == ["read file: README.md (from line 1)"]
    reply = second["messages"][-1]
    assert reply["role"] == "user" and reply["content"][0]["type"] == "tool_result"
    assert "A BatchML tool." in reply["content"][0]["content"]
    assert result.text == "The schema has 3000 types." and result.stop_reason == "end_turn"


P = Project(name="p", repo_url="x")


def test_prompts_say_which_tools_there_are(monkeypatch):
    monkeypatch.delenv("AI_FACTORY_SEARCH_URL", raising=False)
    local = Node(name="local", base_url="http://x/v1")
    offered = tools_offered(local, research=True, repo=True)
    assert offered == {"web": False, "repo": True}
    prompt = chat_system_prompt(P, {}, ["schema/big.json"], offered)
    assert "You have no web access. Do not write tool calls" in prompt
    assert "list_files, read_file and search_repo" in prompt
    assert "read any of it with read_file" in prompt
    claude = Node(name="c", provider="anthropic", base_url="", api_key="k")
    prompt = chat_system_prompt(P, {}, [], tools_offered(claude, research=True, repo=True))
    assert "search and read the web" in prompt and "no web access" not in prompt
    breakdown = decompose_system_prompt(P, {}, None, tools_offered(local, False, True))
    assert "list_files, read_file and search_repo" in breakdown
    assert "no web access" in breakdown
