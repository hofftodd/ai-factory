"""`ai-factory doctor`: check that this host can run executors.

Runs the same paths a task uses: the Docker engine, the executor image, a container
writing to a task clone under AI_FACTORY_DATA_DIR, and a container reaching the model
gateway's address. Prints what is wrong and how to fix it.
"""

from __future__ import annotations

import http.server
import os
import secrets
import shutil
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from ai_factory.config import get_settings
from ai_factory.engine import HOST_ALIAS, Engine, detect, docker

OK, FAIL, SKIP = "ok", "FAIL", "skip"


class Report:
    def __init__(self) -> None:
        self.failed = False

    def line(self, status: str, what: str, hint: str = "") -> None:
        if status == FAIL:
            self.failed = True
        print(f"[{status:>4}] {what}")
        if hint:
            print(f"       {hint}")


def _user() -> str:
    return "1000:1000" if os.getuid() == 0 else f"{os.getuid()}:{os.getgid()}"


def check_engine(r: Report) -> Engine:
    eng = detect()
    if not eng.available:
        hint = (
            "start the ai-factory VM: `ai-factory vm start` (install Lima with `brew install lima`)"
            if sys.platform == "darwin"
            else "install Docker and make sure your user can reach the daemon"
        )
        r.line(FAIL, f"docker engine: {eng.detail}", hint)
        return eng
    where = eng.docker_host or "docker CLI default context"
    r.line(OK, f"docker engine: {eng.kind} ({eng.detail}) via {where}")
    r.line(OK, f"containers reach this host as {HOST_ALIAS} -> {eng.host_ip}")
    if eng.bind_host == "0.0.0.0":
        r.line(FAIL, "model gateway would listen on all interfaces", "set AI_FACTORY_GATEWAY_HOST")
    else:
        r.line(OK, f"model gateway listens on {eng.bind_host}")
    if eng.kind == "lima":
        from ai_factory import vm

        inst = vm.instance() if shutil.which("limactl") else None
        problem = vm.check_mount(inst) if inst else None
        if problem:
            r.line(FAIL, "VM mounts", problem)
        elif inst:
            r.line(OK, f"VM mounts {get_settings().data_dir} writable")
    return eng


def check_image(r: Report) -> bool:
    image = get_settings().executor_image
    if docker("image", "inspect", image).returncode == 0:
        r.line(OK, f"executor image {image}")
        return True
    r.line(FAIL, f"executor image {image} not found", "build it: `ai-factory build-image`")
    return False


def check_mount_write(r: Report) -> None:
    s = get_settings()
    probe_dir = s.data_dir / "doctor"
    probe_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(8)
    proc = docker(
        "run", "--rm", "--user", _user(), "-v", f"{probe_dir}:/w", s.executor_image,
        "sh", "-c", f"echo {token} > /w/probe",
        timeout=120,
    )  # fmt: skip
    probe = probe_dir / "probe"
    got = probe.read_text().strip() if probe.exists() else ""
    probe.unlink(missing_ok=True)
    if proc.returncode == 0 and got == token:
        r.line(OK, "a container can write to a task clone under AI_FACTORY_DATA_DIR")
    else:
        r.line(
            FAIL,
            "a container cannot write to AI_FACTORY_DATA_DIR",
            (proc.stderr.strip() or "file did not appear on the host")[:300]
            + " - on macOS the data dir must be mounted writable into the VM",
        )


def _serve(bind: str) -> tuple[http.server.HTTPServer, int]:
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ai-factory-doctor")

        def log_message(self, *a: object) -> None:
            pass

    server = http.server.HTTPServer((bind, 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def check_gateway_reach(r: Report, eng: Engine) -> None:
    try:
        server, port = _serve(eng.bind_host)
    except OSError as e:
        r.line(FAIL, f"cannot listen on {eng.bind_host}: {e}", "set AI_FACTORY_GATEWAY_HOST")
        return
    try:
        proc = docker(
            "run", "--rm", "--add-host", f"{HOST_ALIAS}:{eng.host_ip}",
            get_settings().executor_image,
            "curl", "-fsS", "--max-time", "10", f"http://{HOST_ALIAS}:{port}/",
            timeout=120,
        )  # fmt: skip
    finally:
        server.shutdown()
    if proc.returncode == 0 and "ai-factory-doctor" in proc.stdout:
        r.line(OK, f"a container reaches the gateway address ({eng.bind_host})")
    else:
        r.line(
            FAIL,
            f"a container cannot reach {eng.bind_host}:{port} as {HOST_ALIAS}",
            (proc.stderr.strip() or proc.stdout.strip())[:300]
            + " - override with AI_FACTORY_GATEWAY_HOST / AI_FACTORY_CONTAINER_HOST_IP",
        )


def check_isolation(r: Report, eng: Engine) -> None:
    """The isolated executor network reaches the gateway through the relay and
    nothing else (sandbox.py)."""
    from ai_factory import sandbox

    try:
        server, port = _serve(eng.bind_host)
    except OSError as e:
        r.line(FAIL, f"cannot listen on {eng.bind_host}: {e}")
        return
    image = get_settings().executor_image
    net = sandbox.exec_network()
    try:
        sandbox.ensure_relay(port)

        def curl(url: str) -> bool:
            proc = docker(
                "run", "--rm", "--network", net, image,
                "curl", "-fsS", "--max-time", "5", url,
                timeout=60,
            )  # fmt: skip
            return proc.returncode == 0

        relay_ok = curl(f"{sandbox.RELAY_URL}/")
        leaks = [u for u in ("http://1.1.1.1/", "https://example.com/") if curl(u)]
    except sandbox.IsolationError as e:
        r.line(FAIL, "isolated executor network", str(e))
        return
    finally:
        server.shutdown()
        sandbox.teardown_isolation()
    if relay_ok:
        r.line(OK, "isolated network: the gateway is reachable through the relay")
    else:
        r.line(FAIL, "isolated network: the relay does not reach the gateway")
    if leaks:
        r.line(FAIL, f"isolated network can reach {', '.join(leaks)}")
    else:
        r.line(OK, "isolated network: the internet is not reachable")


def run(extra: list[Callable[[Report, Engine], None]] | None = None) -> int:
    r = Report()
    s = get_settings()
    print(f"ai-factory doctor - data dir {s.data_dir}, runtime {s.runtime}")
    if s.runtime != "docker":
        r.line(SKIP, "AI_FACTORY_RUNTIME is not docker: executors run without isolation")
        return 0
    eng = check_engine(r)
    if eng.available and check_image(r):
        check_mount_write(r)
        check_gateway_reach(r, eng)
        check_isolation(r, eng)
        for check in extra or []:
            check(r, eng)
    print("all checks passed" if not r.failed else "some checks failed")
    return 1 if r.failed else 0


def main(args: list[str]) -> None:
    del args
    Path(get_settings().data_dir).mkdir(parents=True, exist_ok=True)
    sys.exit(run())
