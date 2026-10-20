"""Streaming chat calls for the planner/reviewer roles.

Two providers:

- ``openai``: any OpenAI-compatible ``/chat/completions`` endpoint. This is how
  local servers (llama.cpp, LM Studio, Ollama, MLX, vLLM) are reached.
- ``anthropic``: Claude through the official Anthropic SDK.

Thinking models served by llama.cpp, vLLM or Ollama stream their reasoning in a
separate delta field; it goes to ``on_reasoning`` and never into the reply text.

Calls stream, and ``should_abort`` is polled while tokens arrive: when it
returns true the stream is closed (which stops generation on the server) and
``Aborted`` is raised. This is how Stop / E-stop cancel an in-flight planner
call (FR-8.2).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx

from foreman.core.contextpack import estimate_tokens
from foreman.core.models import Node

# Claude models that accept server-side refusal fallbacks ("default" routing).
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

ABORT_POLL_S = 0.5

# Output budget for Claude. Local nodes get whatever their context window leaves after
# the prompt: a thinking model can reason for tens of thousands of tokens before it
# writes a word, and a fixed cap cuts it off with nothing to show.
ANTHROPIC_MAX_TOKENS = 16000
MIN_OUTPUT_TOKENS = 4096
# The prompt estimate is ~4 chars/token; real tokenizers differ, so keep headroom.
PROMPT_HEADROOM = 1.15
# Delta fields that carry reasoning: llama.cpp/DeepSeek, then vLLM/Ollama/OpenRouter.
REASONING_FIELDS = ("reasoning_content", "reasoning")


class LLMError(RuntimeError):
    pass


class Aborted(Exception):
    """The caller asked to stop while the model was generating."""


@dataclass
class ChatResult:
    text: str
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    max_tokens: int = 0
    reasoning_chars: int = 0

    @property
    def truncated(self) -> bool:
        """Generation hit max_tokens (or the end of the context window)."""
        return self.stop_reason in ("length", "max_tokens")


TextCallback = Callable[[str], None]
AbortCheck = Callable[[], bool]


class _AbortGate:
    """Throttles should_abort (it reads a database) to once per ABORT_POLL_S."""

    def __init__(self, check: AbortCheck | None):
        self.check = check
        self.last = 0.0

    def __call__(self, force: bool = False) -> None:
        if self.check is None:
            return
        now = time.monotonic()
        if force or now - self.last >= ABORT_POLL_S:
            self.last = now
            if self.check():
                raise Aborted()


async def stream_chat(
    node: Node,
    model: str,
    system: str,
    messages: list[dict[str, str]],
    *,
    max_tokens: int | None = None,
    on_text: TextCallback | None = None,
    on_reasoning: TextCallback | None = None,
    should_abort: AbortCheck | None = None,
) -> ChatResult:
    """Run one chat completion, streaming text to ``on_text``.

    ``messages`` alternate user/assistant with plain-text content. ``max_tokens``
    defaults to `output_budget`.
    """
    gate = _AbortGate(should_abort)
    gate(force=True)
    if max_tokens is None:
        max_tokens = output_budget(node, system, messages)
    if node.provider == "anthropic":
        result = await _anthropic(node, model, system, messages, max_tokens, on_text, gate)
    elif node.provider == "openai":
        result = await _openai(
            node, model, system, messages, max_tokens, on_text, on_reasoning, gate
        )
    else:
        raise LLMError(f"unknown provider {node.provider!r}")
    result.max_tokens = max_tokens
    return result


def output_budget(node: Node, system: str, messages: list[dict[str, str]]) -> int:
    """Output tokens for one call: the rest of a local node's context window."""
    if node.provider == "anthropic":
        return ANTHROPIC_MAX_TOKENS
    prompt = estimate_tokens(system) + sum(estimate_tokens(m["content"]) for m in messages)
    return max(MIN_OUTPUT_TOKENS, node.context_window - int(prompt * PROMPT_HEADROOM))


async def _openai(
    node: Node,
    model: str,
    system: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    on_text: TextCallback | None,
    on_reasoning: TextCallback | None,
    gate: _AbortGate,
) -> ChatResult:
    headers = {"Authorization": f"Bearer {node.api_key}"} if node.api_key else {}
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *messages],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    parts: list[str] = []
    result = ChatResult(text="")
    # Local models can take minutes to read a long prompt before the first token.
    timeout = httpx.Timeout(connect=10.0, read=900.0, write=60.0, pool=10.0)
    async with (
        httpx.AsyncClient(timeout=timeout) as client,
        client.stream(
            "POST", f"{node.base_url.rstrip('/')}/chat/completions", json=body, headers=headers
        ) as resp,
    ):
        if resp.status_code >= 400:
            detail = (await resp.aread()).decode(errors="replace")[:500]
            raise LLMError(f"{node.name}: HTTP {resp.status_code}: {detail}")
        async for line in resp.aiter_lines():
            gate()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("usage"):
                result.input_tokens = chunk["usage"].get("prompt_tokens", 0) or 0
                result.output_tokens = chunk["usage"].get("completion_tokens", 0) or 0
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                for name in REASONING_FIELDS:
                    thought = delta.get(name)
                    if thought:
                        result.reasoning_chars += len(thought)
                        if on_reasoning:
                            on_reasoning(thought)
                        break
                text = delta.get("content")
                if text:
                    parts.append(text)
                    if on_text:
                        on_text(text)
                if choice.get("finish_reason"):
                    result.stop_reason = choice["finish_reason"]
    result.text = "".join(parts)
    return result


async def _anthropic(
    node: Node,
    model: str,
    system: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    on_text: TextCallback | None,
    gate: _AbortGate,
) -> ChatResult:
    client = anthropic.AsyncAnthropic(
        api_key=node.api_key or None,
        base_url=node.base_url or None,
        max_retries=2,
    )
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "thinking": {"type": "adaptive"},
    }
    if model in _FALLBACK_MODELS:
        # On a safety decline, re-run on Anthropic's recommended fallback model.
        params["fallbacks"] = "default"
        params["betas"] = [_FALLBACK_BETA]
    try:
        async with client.beta.messages.stream(**params) as stream:
            async for text in stream.text_stream:
                gate()
                if on_text:
                    on_text(text)
            message = await stream.get_final_message()
    except anthropic.APIConnectionError as e:
        raise LLMError(f"{node.name}: cannot reach the Claude API: {e}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"{node.name}: Claude API error {e.status_code}: {e.message}") from e
    finally:
        await client.close()
    if message.stop_reason == "refusal":
        raise LLMError(f"{model} declined the request")
    text = "".join(b.text for b in message.content if b.type == "text")
    return ChatResult(
        text=text,
        stop_reason=message.stop_reason or "",
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
    )


async def list_models(node: Node) -> list[str]:
    """Model ids the node serves (health check)."""
    if node.provider == "anthropic":
        claude = anthropic.AsyncAnthropic(
            api_key=node.api_key or None,
            base_url=node.base_url or None,
            max_retries=0,
            timeout=10.0,
        )
        try:
            page = await claude.models.list(limit=100)
            return [m.id for m in page.data]
        finally:
            await claude.close()
    headers = {"Authorization": f"Bearer {node.api_key}"} if node.api_key else {}
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{node.base_url.rstrip('/')}/models", headers=headers)
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", []) if m.get("id")]
