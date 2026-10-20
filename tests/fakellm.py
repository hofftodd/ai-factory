"""Scripted model server for tests and demos.

Speaks two APIs:
- OpenAI-compatible: POST /v1/chat/completions (streaming and not), GET /v1/models
- Anthropic Messages: POST /v1/messages (streaming and not)

Behaviour per chat request:
- if the conversation already contains a tool result -> reply "Done." and stop;
- else if a `write` tool is offered (OpenCode) -> call it once to create FILE_PATH;
- else (planner calls, title generation...) -> reply with `planner(request)` if a
  planner function was given, otherwise a short fixed text.

Streamed text is sent in small chunks, `chunk_delay` seconds apart, and
`disconnected` is set if the client hangs up mid-stream.

    python -m tests.fakellm 8999      # run standalone
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

MODEL = "fake-coder"
FILE_PATH = "hello.txt"
FILE_CONTENT = "hello from the fake model\n"
CHUNK = 24

Planner = Callable[[dict[str, Any]], str]


def last_user_text(req: dict[str, Any]) -> str:
    for m in reversed(req.get("messages", [])):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                return "".join(b.get("text", "") for b in c if isinstance(b, dict))
            return str(c)
    return ""


def system_text(req: dict[str, Any]) -> str:
    if "system" in req:  # Anthropic
        s = req["system"]
        return s if isinstance(s, str) else "".join(b.get("text", "") for b in s)
    for m in req.get("messages", []):
        if m.get("role") == "system":
            return str(m.get("content"))
    return ""


class FakeLLM:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 0,
        planner: Planner | None = None,
        chunk_delay: float = 0.0,
    ):
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.planner = planner
        self.chunk_delay = chunk_delay
        # Streamed as `reasoning_content` before the reply, like llama.cpp does for a
        # thinking model; `finish` overrides the finish reason (e.g. "length").
        self.reasoning = ""
        self.finish: str | None = None
        self.disconnected = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _json(self, code: int, obj: Any) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _sse_start(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            def _send(self, payload: bytes) -> bool:
                try:
                    self.wfile.write(payload)
                    self.wfile.flush()
                    return True
                except (BrokenPipeError, ConnectionResetError):
                    outer.disconnected.set()
                    return False

            def do_GET(self) -> None:  # noqa: N802
                outer.headers.append(dict(self.headers.items()))
                if self.path.split("?", 1)[0].rstrip("/").endswith("/models"):
                    self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(req)
                outer.headers.append(dict(self.headers.items()))
                path = self.path.split("?", 1)[0].rstrip("/")
                if path.endswith("/chat/completions"):
                    self._openai(req)
                elif path.endswith("/messages"):
                    self._anthropic(req)
                else:
                    self._json(404, {"error": "not found"})

            # -- OpenAI-compatible ------------------------------------------

            def _openai(self, req: dict[str, Any]) -> None:
                message, finish = outer.decide(req)
                usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
                if not req.get("stream"):
                    self._json(
                        200,
                        {
                            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": req.get("model", MODEL),
                            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                            "usage": usage,
                        },
                    )
                    return
                self._sse_start()
                cid = f"chatcmpl-{uuid.uuid4().hex[:8]}"

                def chunk(
                    delta: dict[str, Any],
                    finish_reason: str | None = None,
                    with_usage: bool = False,
                ) -> bool:
                    obj: dict[str, Any] = {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": req.get("model", MODEL),
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                    }
                    if with_usage:
                        obj["usage"] = usage
                    return self._send(f"data: {json.dumps(obj)}\n\n".encode())

                if not chunk({"role": "assistant", "content": ""}):
                    return
                for i in range(0, len(outer.reasoning), CHUNK):
                    if not chunk({"reasoning_content": outer.reasoning[i : i + CHUNK]}):
                        return
                if outer.finish:
                    finish = outer.finish
                if message.get("tool_calls"):
                    for i, tc in enumerate(message["tool_calls"]):
                        chunk({"tool_calls": [{"index": i, **tc}]})
                else:
                    text = message["content"] or ""
                    for i in range(0, len(text), CHUNK):
                        if not chunk({"content": text[i : i + CHUNK]}):
                            return
                        time.sleep(outer.chunk_delay)
                chunk({}, finish, with_usage=True)
                self._send(b"data: [DONE]\n\n")

            # -- Anthropic Messages -----------------------------------------

            def _anthropic(self, req: dict[str, Any]) -> None:
                text = outer.planner(req) if outer.planner else "Fake reply"
                mid = f"msg_{uuid.uuid4().hex[:12]}"
                model = req.get("model", MODEL)
                if not req.get("stream"):
                    self._json(
                        200,
                        {
                            "id": mid,
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [{"type": "text", "text": text}],
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": 5},
                        },
                    )
                    return
                self._sse_start()

                def event(name: str, data: dict[str, Any]) -> bool:
                    return self._send(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())

                event(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": mid,
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": 1},
                        },
                    },
                )
                event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
                for i in range(0, len(text), CHUNK):
                    if not event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": text[i : i + CHUNK]},
                        },
                    ):
                        return
                    time.sleep(outer.chunk_delay)
                event("content_block_stop", {"type": "content_block_stop", "index": 0})
                event(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 5},
                    },
                )
                event("message_stop", {"type": "message_stop"})

        self.server = http.server.ThreadingHTTPServer((host, port), Handler)
        self.port = self.server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def decide(self, req: dict[str, Any]) -> tuple[dict[str, Any], str]:
        messages = req.get("messages", [])
        tools = {t.get("function", {}).get("name") for t in req.get("tools") or []}
        if any(m.get("role") == "tool" for m in messages):
            return {"role": "assistant", "content": "Done."}, "stop"
        if "write" in tools:
            args = json.dumps({"filePath": FILE_PATH, "content": FILE_CONTENT})
            call = {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": "write", "arguments": args},
            }
            return {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
        text = self.planner(req) if self.planner else "Fake task"
        return {"role": "assistant", "content": text}, "stop"

    def start(self) -> FakeLLM:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()


DEMO_SPEC = """# Spec: greeter

A tiny command-line greeter.

## Functional requirements
- FR-1.1 `greet(name)` returns `hello, <name>`.
- FR-1.2 Running `greeter <name>` prints the greeting.

## Non-functional
- NFR-1 No dependencies beyond the standard library.
"""

DEMO_PLAN = """# Plan

- `greeter/core.py`: `greet(name) -> str` (FR-1.1)
- `greeter/cli.py`: argument parsing, prints `greet(name)` (FR-1.2)

Milestone 1: both, with unit tests next to each module.
"""

DEMO_VALIDATION = """# Validation

- V1 (FR-1.1): `greet("bob") == "hello, bob"`
- V2 (FR-1.2): `greeter bob` prints `hello, bob`
- Acceptance: `sh tests/acceptance/run.sh` exits 0.
"""

DEMO_TASKS = [
    {
        "key": "T1",
        "title": "greet() function",
        "req_refs": ["FR-1.1"],
        "description": "Implement greet(name) in greeter/core.py with a unit test.",
        "todos": ["Create greeter/core.py", "Add tests/test_core.py"],
        "allowed_files": ["greeter/core.py", "tests/test_core.py"],
        "context_files": [],
        "validation_cmd": "test -f README.md",
        "deps": [],
    },
    {
        "key": "T2",
        "title": "Command-line entry point",
        "req_refs": ["FR-1.2"],
        "description": "Parse the name argument and print greet(name).",
        "todos": ["Create greeter/cli.py", "Add tests/test_cli.py"],
        "allowed_files": ["greeter/cli.py", "tests/test_cli.py"],
        "context_files": ["greeter/core.py"],
        "validation_cmd": "test -f README.md",
        "deps": ["T1"],
    },
]


def demo_planner(req: dict[str, Any]) -> str:
    """A scripted planner for demos and browser tests (greeter project)."""
    if "Break the approved plan" in system_text(req):
        return "```json\n" + json.dumps(DEMO_TASKS, indent=2) + "\n```"
    turns = sum(1 for m in req.get("messages", []) if m.get("role") == "user")
    if turns == 1:
        return (
            "Happy to help. A first draft of the spec is below.\n\n"
            f"<<<FILE specs/001-initial/spec.md\n{DEMO_SPEC}>>>\n\n"
            "Should the greeting be lowercase (`hello, bob`) or capitalised?"
        )
    return (
        "Lowercase it is. I wrote the plan, the validation criteria and an acceptance test.\n\n"
        f"<<<FILE specs/001-initial/plan.md\n{DEMO_PLAN}>>>\n"
        f"<<<FILE specs/001-initial/validation.md\n{DEMO_VALIDATION}>>>\n"
        "<<<FILE tests/acceptance/run.sh\ntest -f README.md\n>>>\n"
        "<<<ACCEPTANCE_CMD\nsh tests/acceptance/run.sh\n>>>\n"
        "If this looks right, approve the spec and I'll break it into tasks."
    )


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = int(args[0]) if args else 8999
    demo = "--demo" in sys.argv
    print(f"fake LLM on :{port}, model {MODEL}{' (demo planner)' if demo else ''}", flush=True)
    FakeLLM(
        port=port, planner=demo_planner if demo else None, chunk_delay=0.02 if demo else 0.0
    ).server.serve_forever()
