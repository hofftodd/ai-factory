"""OpenCode (https://opencode.ai) in non-interactive `run` mode.

The run's model gateway URL is registered as an OpenAI-compatible provider
named "foreman" in a generated config file; the prompt is passed as the run
message.
"""

from __future__ import annotations

import json
from pathlib import Path

from foreman.core.models import Project, Task
from foreman.executors.base import ExecutorContext, ExecutorInvocation

PROVIDER = "foreman"


def opencode_config(endpoint: str, model: str) -> dict:
    # No API key: the gateway adds the node's key on the host side.
    options: dict[str, str] = {"baseURL": endpoint, "apiKey": "foreman"}
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "foreman gateway",
                "options": options,
                "models": {model: {"name": model}},
            }
        },
        "model": f"{PROVIDER}/{model}",
        "autoupdate": False,
        "share": "disabled",
        # Unattended run inside a sandbox: allow edits and shell, nothing from the web.
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "deny"},
    }


class OpenCodeExecutor:
    kind = "opencode"
    prompt_overhead_tokens = 7500  # measured: ~7.4k (system prompt + 9 tool schemas)
    needs_node = True

    def prepare(
        self,
        project: Project,
        task: Task,
        endpoint: str,
        model: str,
        ctl_dir: Path,
        prompt: str,
        context: ExecutorContext | None = None,
    ) -> ExecutorInvocation:
        if not endpoint or not model:
            raise ValueError("opencode executor needs a model endpoint and a model")
        (ctl_dir / "prompt.md").write_text(prompt)
        (ctl_dir / "opencode.json").write_text(
            json.dumps(opencode_config(endpoint, model), indent=2)
        )
        return ExecutorInvocation(
            command=[
                "bash",
                "-c",
                'export OPENCODE_CONFIG="$FOREMAN_CTL/opencode.json"; '
                'exec opencode run --model "$FOREMAN_MODEL" "$(cat "$FOREMAN_CTL/prompt.md")"',
            ],
            env={"FOREMAN_MODEL": f"{PROVIDER}/{model}"},
        )
