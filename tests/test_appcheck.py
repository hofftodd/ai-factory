"""The app check: starting the project's app with scripts/start.sh and looking at it.

The scripts run for real here (bash + node, local runtime). Without Playwright on the
machine the pages are fetched over HTTP, and the browser part is reported as skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_factory import appcheck

needs_node = pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("python3") is None,
    reason="needs node and python3",
)

SERVE = '#!/bin/sh\nexec python3 -m http.server "$APP_PORT" --bind 127.0.0.1 --directory www\n'


def make_app(root: Path, start: str | None = SERVE, page: str = "<h1>Recipes</h1>") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if start is not None:
        (root / "scripts").mkdir()
        (root / "scripts" / "start.sh").write_text(start)
    (root / "www").mkdir()
    (root / "www" / "index.html").write_text(f"<!doctype html><html><body>{page}</body></html>")
    return root


def run_check(root: Path, tmp: Path) -> tuple[int, appcheck.AppResult, Path]:
    ctl, results = tmp / "ctl", tmp / "results"
    results.mkdir()
    appcheck.install(ctl)
    env = {
        **os.environ,
        **appcheck.env("local"),
        "APP_START_TIMEOUT": "20",
        "AI_FACTORY_CTL": str(ctl),
        "AI_FACTORY_RESULTS": str(results),
    }
    proc = subprocess.run(
        ["bash", "-c", appcheck.shell_command()],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, appcheck.read_result(results), results


def test_only_projects_with_a_start_script_have_an_app(tmp_path):
    assert not appcheck.applies(tmp_path)
    make_app(tmp_path)
    assert appcheck.applies(tmp_path)


def test_docker_uses_a_fixed_port_and_the_local_runtime_a_free_one():
    assert appcheck.env("docker")["APP_PORT"] == appcheck.APP_PORT
    assert int(appcheck.env("local")["APP_PORT"]) > 0


def test_a_missing_or_broken_result_is_a_problem(tmp_path):
    result = appcheck.read_result(tmp_path)
    assert not result.ok and "no usable result" in result.problems[0]
    (tmp_path / "app.json").write_text("[1, 2]")
    assert not appcheck.read_result(tmp_path).ok


def test_the_result_summary_lists_the_problems(tmp_path):
    (tmp_path / "app.json").write_text(
        json.dumps(
            {
                "started": True,
                "browser": True,
                "problems": ["/: uncaught error: boom", "/: the page shows no text"],
                "pages": [{"path": "/"}],
            }
        )
    )
    result = appcheck.read_result(tmp_path)
    assert not result.ok
    assert result.summary() == "- /: uncaught error: boom\n- /: the page shows no text"


@needs_node
def test_a_working_app_passes_and_gets_a_report(tmp_path):
    app = make_app(tmp_path / "app")
    code, result, results = run_check(app, tmp_path)
    assert code == 0, result.problems
    assert result.ok and result.started
    assert (results / "report" / "app" / "index.html").is_file()
    junit = (results / "junit.xml").read_text()
    assert 'name="page / works"' in junit


@needs_node
def test_the_pages_listed_by_the_tester_are_checked_too(tmp_path):
    app = make_app(tmp_path / "app")
    (app / "tests").mkdir()
    (app / "tests" / "app-pages.txt").write_text("# screens\n/missing.html\n")
    code, result, _ = run_check(app, tmp_path)
    assert code != 0
    assert [p["path"] for p in result.pages] == ["/", "/missing.html"]
    assert any(p.startswith("/missing.html:") for p in result.problems)


@needs_node
def test_an_app_that_exits_fails_with_its_output(tmp_path):
    app = make_app(tmp_path / "app", start="#!/bin/sh\necho 'no such module: app'\nexit 3\n")
    code, result, results = run_check(app, tmp_path)
    assert code != 0 and not result.started
    assert "exited before the app answered" in result.problems[0]
    assert "no such module" in (results / "junit.xml").read_text()


def test_no_start_script_is_reported(tmp_path):
    app = make_app(tmp_path / "app", start=None)
    code, result, _ = run_check(app, tmp_path)
    assert code != 0 and result.problems == ["scripts/start.sh is missing"]


# With a browser: the checks a person would notice. Runs where Playwright is installed
# for the dashboard's own tests (web/node_modules) and a browser can start.
PLAYWRIGHT = Path(__file__).resolve().parent.parent / "web" / "node_modules"


def browser_check(app: Path, tmp: Path) -> appcheck.AppResult:
    (app / "node_modules").symlink_to(PLAYWRIGHT)
    _, result, _ = run_check(app, tmp)
    if not result.browser or any("browser could not run" in p for p in result.problems):
        pytest.skip("no browser Playwright can start here")
    return result


needs_browser = pytest.mark.skipif(
    not (PLAYWRIGHT / "playwright").is_dir() or shutil.which("node") is None,
    reason="needs Playwright (web/node_modules) and node",
)


@needs_browser
def test_a_page_that_renders_blank_is_a_problem(tmp_path):
    page = '<div id="root"></div><script>document.title = "x";</script>'
    result = browser_check(make_app(tmp_path / "app", page=page), tmp_path)
    assert any("renders blank" in p for p in result.problems)


@needs_browser
def test_uncaught_errors_are_a_problem(tmp_path):
    page = '<h1>Hi</h1><script>throw new Error("boom")</script>'
    result = browser_check(make_app(tmp_path / "app", page=page), tmp_path)
    assert result.problems == ["/: uncaught error: boom"]


@needs_browser
def test_a_page_that_scrolls_sideways_is_a_problem(tmp_path):
    page = '<nav style="white-space: nowrap">' + "SomeEntityType " * 400 + "</nav>"
    result = browser_check(make_app(tmp_path / "app", page=page), tmp_path)
    assert any("scrolls sideways" in p for p in result.problems)


@needs_browser
def test_a_working_page_passes_in_a_browser(tmp_path):
    result = browser_check(make_app(tmp_path / "app"), tmp_path)
    assert result.ok, result.problems
    assert result.pages[0]["screenshot"] == "index.png"
