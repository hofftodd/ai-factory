"""The app check: start the project's app the way a person would and look at it.

A project with a server or a UI has `scripts/start.sh` (written by the Builder at
setup): it starts the app in the foreground and serves it on http://127.0.0.1:$APP_PORT.
After a task's suite passes, and before the acceptance tests at the milestone,
ai-factory runs its own check (appcheck.sh + appcheck.cjs, copied into the step's
control directory, so no agent writes or changes it):

- the app must answer within APP_START_TIMEOUT seconds;
- `/` and any page listed in `tests/app-pages.txt` are opened in headless Chromium
  (Playwright, when the toolchain image has it; plain HTTP otherwise): console errors,
  uncaught errors, failed requests and a page with no text are problems;
- each page is screenshotted into an HTML report (`report/app/index.html`), shown on
  the task's Tests tab like any test report.

The results are JUnit XML too (one case per page), stored as a test run with step
"app".
"""

from __future__ import annotations

import json
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path

START_SCRIPT = "scripts/start.sh"
PAGES_FILE = "tests/app-pages.txt"
STEP = "app"
APP_PORT = "8799"
# How long the app may take to answer, and the whole check (start + every page).
START_TIMEOUT_S = 90
CHECK_TIMEOUT_S = 600

_HERE = Path(__file__).parent
_FILES = ("appcheck.sh", "appcheck.cjs")


def applies(workspace: Path) -> bool:
    """Whether the project has an app to check."""
    return (workspace / START_SCRIPT).is_file()


def install(ctl_dir: Path) -> None:
    """Copy the check into a step's control directory (mounted at $AI_FACTORY_CTL)."""
    ctl_dir.mkdir(parents=True, exist_ok=True)
    for name in _FILES:
        shutil.copyfile(_HERE / name, ctl_dir / name)


def shell_command() -> str:
    """The check as one shell command line, for `bash -lc`."""
    return 'bash "$AI_FACTORY_CTL/appcheck.sh"'


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def env(runtime: str = "docker") -> dict[str, str]:
    """The check's environment. In Docker each step has its own network, so the port is
    always the same; the local runtime shares the host's, so it gets a free one."""
    port = APP_PORT if runtime == "docker" else str(free_port())
    return {"APP_PORT": port, "APP_START_TIMEOUT": str(START_TIMEOUT_S)}


@dataclass
class AppResult:
    started: bool = False
    # Whether the pages were rendered in a browser (False: only fetched over HTTP).
    browser: bool = False
    problems: list[str] = field(default_factory=list)
    pages: list[dict[str, object]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.started and not self.problems

    def summary(self, limit: int = 12) -> str:
        """What went wrong, for the Builder (previous_failure) and the event log."""
        if self.ok:
            pages = len(self.pages)
            how = "rendered in a browser" if self.browser else "fetched (no browser installed)"
            return f"the app started; {pages} page(s) {how}, no problems"
        lines = self.problems[:limit]
        if len(self.problems) > limit:
            lines.append(f"... and {len(self.problems) - limit} more")
        return "\n".join(f"- {p}" for p in lines)


def read_result(results_dir: Path) -> AppResult:
    """The check's app.json. A missing or unreadable file means the check itself broke."""
    path = results_dir / "app.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        return AppResult(problems=[f"the app check wrote no usable result ({e})"])
    if not isinstance(data, dict):
        return AppResult(problems=["the app check wrote no usable result"])
    return AppResult(
        started=bool(data.get("started")),
        browser=bool(data.get("browser")),
        problems=[str(p) for p in data.get("problems") or []],
        pages=[p for p in data.get("pages") or [] if isinstance(p, dict)],
    )
