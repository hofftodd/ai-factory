"""Executor adapters: what they write to the control dir and how they read output."""

from __future__ import annotations

import json

import pytest

from ai_factory.core.contextpack import budget_tokens
from ai_factory.core.models import Project, Task
from ai_factory.executors import EXECUTORS, ExecutorContext
from ai_factory.executors.pi import PiExecutor, PiOutput


def test_pi_prepare(tmp_path):
    inv = PiExecutor().prepare(
        Project(name="p", repo_url="x"),
        Task(project_id=1, title="t"),
        "http://ai-factory-gw:8701/g/tok/v1",
        "qwen3-coder",
        tmp_path,
        "# Task T1\n",
        ExecutorContext(context_window=32768),
    )
    assert (tmp_path / "prompt.md").read_text() == "# Task T1\n"
    cfg = json.loads((tmp_path / "pi" / "models.json").read_text())
    provider = cfg["providers"]["ai-factory"]
    assert provider["baseUrl"] == "http://ai-factory-gw:8701/g/tok/v1"
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "ai-factory"  # never a real key: the gateway adds it
    assert provider["models"] == [
        {"id": "qwen3-coder", "name": "qwen3-coder", "maxTokens": 8192, "contextWindow": 32768}
    ]
    script = inv.command[-1]
    for flag in ("--mode json", "--no-session", "--offline", "--no-context-files", "--no-approve"):
        assert flag in script
    # pi's own search tools on top of its defaults (they run rg and fd from the image).
    assert "--tools read,bash,edit,write,grep,find,ls" in script
    assert 'PI_CODING_AGENT_DIR="$HOME/.pi/agent"' in script
    assert inv.env == {"AI_FACTORY_MODEL": "qwen3-coder"}
    assert inv.output is not None


def _events(*evs: dict) -> list[str]:
    return [json.dumps(e) for e in evs]


def test_pi_output_renders_and_succeeds():
    out = PiOutput()
    lines = _events(
        {"type": "session", "id": "x"},
        {"type": "tool_execution_start", "toolName": "bash", "args": {"command": "ls"}},
        {
            "type": "tool_execution_end",
            "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "a\nb\n"}]},
            "isError": False,
        },
        {
            "type": "tool_execution_start",
            "toolName": "write",
            "args": {"path": "f", "content": "x"},
        },
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        },
        {"type": "agent_settled", "aborted": False},
    )
    shown = [r for r in (out.render(line) for line in lines) if r is not None]
    assert shown == ["$ ls", "a\nb", "write f (1 lines)", "Done."]
    assert out.failure() is None
    assert out.render("plain stderr line") == "plain stderr line"


def test_pi_output_reports_model_errors_despite_exit_0():
    out = PiOutput()
    for line in _events(
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                                            "errorMessage": "400: budget exceeded"}},
        {"type": "agent_settled", "aborted": False},
    ):  # fmt: skip
        out.render(line)
    assert out.failure() == "pi: 400: budget exceeded"
    assert PiOutput().failure() == "pi ended without finishing its run"


def test_budget_subtracts_executor_overhead():
    p = Project(name="p", repo_url="x", context_budget_pct=50)
    assert budget_tokens(p, 32000, "shell") == 16000
    from ai_factory.executors.pi import PiExecutor

    assert budget_tokens(p, 32000, "pi") == (32000 - PiExecutor.prompt_overhead_tokens) // 2
    assert budget_tokens(p, 32000, "opencode") == (32000 - 7500) // 2
    assert budget_tokens(p, 32000, "pi") > budget_tokens(p, 32000, "opencode")
    assert set(EXECUTORS) == {"pi", "opencode", "aider", "shell"}


@pytest.mark.parametrize(
    "window,expected",
    [(None, 8192), (16384, 4096), (32768, 8192), (131072, 32768), (1_000_000, 32768)],
)
def test_pi_output_budget_gives_thinking_models_room(window, expected):
    # 8k was too little for a thinking model on a 128k window: it reasoned through the
    # whole budget, was cut off, and pi ended the session with nothing written.
    from ai_factory.executors.pi import auto_max_output, models_config

    assert auto_max_output(window) == expected
    entry = models_config("http://gw/v1", "m", ExecutorContext(context_window=window))
    assert entry["providers"]["ai-factory"]["models"][0]["maxTokens"] == expected


def test_a_node_can_set_its_own_output_budget():
    from ai_factory.core.models import Node
    from ai_factory.executors.pi import models_config
    from ai_factory.orchestrator.runner import executor_context

    auto = executor_context(Node(name="n", base_url="x", context_window=131072))
    assert auto.max_output_tokens is None
    small = executor_context(
        Node(name="n", base_url="x", context_window=131072, max_output_tokens=4096)
    )
    cfg = models_config("http://gw/v1", "m", small)
    assert cfg["providers"]["ai-factory"]["models"][0]["maxTokens"] == 4096
    assert executor_context(None).context_window is None
