"""Parsing test results and coverage; changed-line coverage; role bindings."""

from __future__ import annotations

import subprocess

from ai_factory import testresults as tr
from ai_factory.core import roles
from ai_factory.core.models import Project

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" tests="4">
  <testcase classname="tests.test_greet" name="test_ok" time="0.01" file="tests/test_greet.py"/>
  <testcase classname="tests.test_greet" name="test_bad" time="0.2">
    <failure message="assert 1 == 2">long traceback</failure></testcase>
  <testcase classname="tests.test_other" name="test_err"><error message="boom"/></testcase>
  <testcase classname="tests.test_other" name="test_skip"><skipped/></testcase>
</testsuite></testsuites>"""

COBERTURA = """<?xml version="1.0"?>
<coverage><sources><source>/workspace/src</source></sources><packages><package><classes>
  <class filename="greet.py"><lines>
    <line number="1" hits="1"/><line number="2" hits="0"/><line number="3" hits="4"/>
  </lines></class>
</classes></package></packages></coverage>"""

LCOV = """SF:/workspace/src/greet.py
DA:1,1
DA:2,0
end_of_record
SF:src/cli.py
DA:5,2
end_of_record
"""


def test_parse_junit():
    cases = tr.parse_junit(JUNIT)
    assert [c.status for c in cases] == ["passed", "failed", "error", "skipped"]
    assert cases[0].file == "tests/test_greet.py" and cases[1].message == "assert 1 == 2"
    assert cases[1].duration_s == 0.2


def test_parse_cobertura_resolves_sources(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "greet.py").write_text("x\n")
    lines = tr.parse_cobertura(COBERTURA, str(tmp_path))
    assert lines == {"src/greet.py": {1: 1, 2: 0, 3: 4}}


def test_parse_lcov():
    lines = tr.parse_lcov(LCOV)
    assert lines == {"src/greet.py": {1: 1, 2: 0}, "src/cli.py": {5: 2}}


def test_collect_reports_missing_results(tmp_path):
    res = tr.collect(tmp_path, tmp_path)
    assert "no junit.xml" in res.problem
    (tmp_path / "junit.xml").write_text(JUNIT)
    (tmp_path / "lcov.info").write_text(LCOV)
    res = tr.collect(tmp_path, tmp_path)
    assert res.problem == "" and res.count("passed") == 1
    assert res.coverage == 200 / 3
    assert res.per_file()["src/greet.py"] == {"covered": 1, "total": 2}


JACOCO = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><!DOCTYPE report PUBLIC \
"-//JACOCO//DTD Report 1.1//EN" "report.dtd"><report name="backend">
<package name="com/app"><class name="com/app/Units" sourcefilename="Units.kt"/>
  <sourcefile name="Units.kt">
    <line nr="3" mi="0" ci="4" mb="0" cb="0"/><line nr="4" mi="2" ci="0" mb="0" cb="0"/>
  </sourcefile>
  <sourcefile name="Gone.kt"><line nr="1" mi="0" ci="1"/></sourcefile>
</package></report>"""


def _repo(tmp_path, *files):
    for f in files:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)


def test_parse_jacoco_finds_each_source_file_in_the_repository(tmp_path):
    _repo(
        tmp_path,
        "backend/src/main/kotlin/com/app/Units.kt",
        "backend/src/test/kotlin/com/app/Units.kt",
    )
    lines = tr.parse_jacoco(JACOCO, str(tmp_path))
    assert lines == {
        "backend/src/main/kotlin/com/app/Units.kt": {3: 4, 4: 0},
        "com/app/Gone.kt": {1: 1},  # not in the repository: left as JaCoCo names it
    }
    assert tr.parse_coverage_xml(JACOCO, str(tmp_path)) == lines
    assert tr.parse_coverage_xml(COBERTURA) == tr.parse_cobertura(COBERTURA)


def test_collect_merges_every_coverage_file(tmp_path):
    """A back end's JaCoCo report and a front end's Cobertura and lcov reports count
    together; a line two reports share counts its most hits."""
    _repo(tmp_path, "backend/src/main/kotlin/com/app/Units.kt", "src/greet.py")
    results = tmp_path / "results"
    results.mkdir()
    (results / "junit.xml").write_text(JUNIT)
    (results / "coverage-backend.xml").write_text(JACOCO)
    (results / "coverage.xml").write_text(COBERTURA)
    (results / "lcov-frontend.info").write_text(LCOV)
    res = tr.collect(results, tmp_path)
    assert res.problem == ""
    assert res.lines["backend/src/main/kotlin/com/app/Units.kt"] == {3: 4, 4: 0}
    assert res.lines["src/greet.py"] == {1: 1, 2: 0, 3: 4}  # Cobertura and lcov merged
    assert res.lines["src/cli.py"] == {5: 2}
    # A module's tool may name files relative to the module.
    _repo(tmp_path, "frontend/src/app/app.ts")
    (results / "lcov-frontend.info").write_text("SF:src/app/app.ts\nDA:1,1\nend_of_record\n")
    (results / "coverage-frontend.xml").write_text(
        '<coverage><packages><package><classes><class filename="frontend/src/app/app.ts">'
        '<lines><line number="1" hits="0"/><line number="2" hits="0"/></lines>'
        "</class></classes></package></packages></coverage>"
    )
    res = tr.collect(results, tmp_path)
    assert res.lines["frontend/src/app/app.ts"] == {1: 1, 2: 0}
    assert "src/app/app.ts" not in res.lines
    (results / "coverage-broken.xml").write_text("<report")
    assert "coverage-broken.xml is not valid XML" in tr.collect(results, tmp_path).problem


def test_task_cases_are_found_by_file_or_class():
    a = tr.Case(name="test_ok", classname="tests.test_greet", file="tests/test_greet.py")
    b = tr.Case(name="test_x", classname="tests.test_greet")
    c = tr.Case(name="test_y", classname="tests.test_other")
    files = ["tests/test_greet.py"]
    assert tr.is_task_case(a, files) and tr.is_task_case(b, files)
    assert not tr.is_task_case(c, files)


def test_task_cases_from_tap_scripts_and_junit_classes():
    # larder's T3: tap2junit named the cases after the script and test number, no file.
    tap = [tr.Case(name=f"case {i}", classname=f"units.{i}") for i in (1, 2)]
    assert all(tr.is_task_case(c, ["tests/smoke/units.sh"]) for c in tap)
    assert not tr.is_task_case(tap[0], ["tests/smoke/pantry.sh"])
    assert not tr.is_task_case(tr.Case(name="x", classname="units.extra"), ["tests/smoke/units.sh"])
    junit = tr.Case(name="converts kg to g", classname="com.larder.app.UnitsTest")
    kt = "backend/src/test/kotlin/com/larder/app/UnitsTest.kt"
    assert tr.is_task_case(junit, [kt])


# Vitest's JUnit report, as future-lab's T17 produced it: the class name is the file's
# path, and a file that fails to load is one failed case named after the file.
VITEST_JUNIT = """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="3" failures="1" errors="0" time="0.02">
  <testsuite name="tests/frontend/registry.test.ts"
             tests="1" failures="1" errors="0" skipped="0" time="0">
    <testcase classname="tests/frontend/registry.test.ts"
              name="tests/frontend/registry.test.ts" time="0">
      <failure message="Failed to resolve import ../../frontend/src/registry" type="Error">
Error: Failed to resolve import
      </failure>
    </testcase>
  </testsuite>
  <testsuite name="tests/frontend/smoke.test.tsx"
             tests="2" failures="0" errors="0" skipped="0" time="0.01">
    <testcase classname="tests/frontend/smoke.test.tsx"
              name="front-end skeleton &gt; renders" time="0.01">
    </testcase>
    <testcase classname="./tests/frontend/smoke.test.tsx"
              name="front-end skeleton &gt; mounts" time="0.01">
    </testcase>
  </testsuite>
</testsuites>
"""


def test_vitest_cases_are_found_by_their_path_class_names():
    cases = tr.parse_junit(VITEST_JUNIT)
    registry = ["tests/frontend/registry.test.ts"]
    mine = [c for c in cases if tr.is_task_case(c, registry)]
    assert [c.status for c in mine] == ["failed"]  # a file that fails to load counts
    smoke = ["tests/frontend/smoke.test.tsx"]
    assert sum(tr.is_task_case(c, smoke) for c in cases) == 2  # "./" prefix too
    assert not any(tr.is_task_case(c, ["tests/frontend/other.test.ts"]) for c in cases)


def test_cases_named_from_inside_a_module_are_found():
    # larder-2's T12.2.1: vitest ran in frontend/ and named the case's class
    # `src/app/app.routes.spec.ts`, so its three failing tests didn't count as the task's
    # and it was rejected as no_red six times.
    spec = "frontend/src/app/app.routes.spec.ts"
    case = tr.Case(name="routes > four paths", classname="src/app/app.routes.spec.ts")
    assert tr.is_task_case(case, [spec])
    assert not tr.is_task_case(case, ["frontend/src/app/app.component.spec.ts"])
    # A bare file name is not a path: it doesn't match by suffix.
    assert not tr.is_task_case(tr.Case(name="x", file="spec.ts"), ["frontend/app/spec.ts"])
    # Nor does a path that is only a suffix of a directory name.
    assert not tr.is_task_case(tr.Case(name="x", file="app/x.spec.ts"), ["webapp/x.spec.ts"])


def test_changed_line_coverage(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "greet.py").write_text("a\nb\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    (tmp_path / "greet.py").write_text("a\nB\nc\nd\n")  # lines 2-4 changed/added
    git("commit", "-qam", "change")
    added = tr.added_lines(tmp_path, base, ["greet.py"])
    assert added == {"greet.py": {2, 3, 4}}
    # Line 4 isn't instrumented (e.g. a comment), so it doesn't count.
    pct, counted = tr.changed_coverage({"greet.py": {1: 1, 2: 1, 3: 0}}, added)
    assert counted == 2 and pct == 50.0
    assert tr.changed_coverage({}, added) == (None, 0)


def test_role_bindings_fall_back_for_older_projects():
    p = Project(
        name="p",
        repo_url="x",
        role_bindings={
            "planner": {"node_id": 1, "model": "big"},
            "executor": {"node_id": 2, "model": "small"},
            "tester": {"node_id": 3, "model": "mid"},
        },
    )
    assert roles.binding(p, roles.PROJECT_PLANNER)["model"] == "big"
    assert roles.binding(p, roles.TASK_PLANNER)["model"] == "big"
    assert roles.binding(p, roles.TESTER)["model"] == "mid"  # its own binding wins
    assert roles.binding(p, roles.BUILDER)["model"] == "small"
    assert roles.binding(p, roles.ESCALATION) == {}


def test_test_paths_and_fences():
    p = Project(name="p", repo_url="x", test_paths=["tests", "spec/"])
    assert roles.is_test_path(p, "tests/test_a.py") and roles.is_test_path(p, "spec/x_spec.rb")
    assert roles.is_test_path(p, "src/foo_test.go")  # test files next to code count too
    assert not roles.is_test_path(p, "src/app.py")
    # Kotlin and Maven/Gradle test sources, fixtures included, outside tests/.
    assert roles.is_test_path(p, "backend/src/test/kotlin/com/app/UnitsTest.kt")
    assert roles.is_test_path(p, "backend/src/test/resources/application-test.properties")
    assert roles.is_test_path(p, "src/test/java/AppIT.java")
    assert not roles.is_test_path(p, "backend/src/main/kotlin/com/app/Units.kt")
    changed = ["src/app.py", "tests/test_a.py"]
    assert roles.fence(p, roles.TESTER, changed) == ["src/app.py"]
    assert roles.fence(p, roles.BUILDER, changed) == ["tests/test_a.py"]
    # larder: karma.conf.js is outside tests/, and the Builder may not change it after
    # setup, so it is the Tester's; shared files like package.json are not.
    config = ["frontend/karma.conf.js", "frontend/package.json", "frontend/src/app/a.spec.ts"]
    assert roles.fence(p, roles.TESTER, config) == ["frontend/package.json"]
    assert roles.fence(p, roles.TESTER, [".ai-factory/Dockerfile"]) == [".ai-factory/Dockerfile"]


def test_test_configuration_changes():
    changed = roles.test_config_changed
    assert changed("conftest.py", None, "collect_ignore = ['tests/test_old.py']\n")
    assert changed("src/pytest.ini", "[pytest]\n", "[pytest]\naddopts = -k 'not old'\n")
    assert not changed("conftest.py", "x = 1\n", "x = 1\n")
    assert not changed("src/app.py", None, "print(1)\n")
    # Shared files: only their test sections count.
    toml = '[project]\nname = "app"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'
    deps = toml.replace('name = "app"', 'name = "app"\ndependencies = ["rich"]')
    assert not changed("pyproject.toml", toml, deps)
    deselect = toml.replace('"-q"', '"-q --deselect tests/test_old.py"')
    assert changed("pyproject.toml", toml, deselect)
    assert changed("pyproject.toml", toml, toml + '\n[tool.coverage.run]\nomit = ["app/*"]\n')
    cfg = "[metadata]\nname = a\n"
    assert not changed("setup.cfg", cfg, cfg.replace("= a", "= b"))
    assert changed("setup.cfg", cfg, cfg + "[tool:pytest]\naddopts = -x\n")
    pkg = '{"scripts": {"test": "jest", "build": "tsc"}}'
    assert not changed("package.json", pkg, pkg.replace('"tsc"', '"tsc -b"'))
    ignore = pkg.replace('"jest"', '"jest --testPathIgnorePatterns old"')
    assert changed("package.json", pkg, ignore)


def test_html_report_pages_are_found_and_saved(tmp_path):
    from ai_factory.testresults import report_pages, save_report

    results = tmp_path / "results"
    report = results / "report"
    (report / "frontend" / "assets").mkdir(parents=True)
    (report / "backend.html").write_text("<h1>backend</h1>")
    (report / "frontend" / "index.html").write_text("<script src=assets/a.js></script>")
    (report / "frontend" / "assets" / "a.js").write_text("1")
    (report / "frontend" / "assets" / "index.html").write_text("deeper, still a page")
    assert report_pages(report) == [
        "backend.html",
        "frontend/assets/index.html",
        "frontend/index.html",
    ]
    dest = tmp_path / "saved"
    assert save_report(results, dest)
    assert (dest / "frontend" / "assets" / "a.js").read_text() == "1"
    assert not save_report(tmp_path / "nothing", tmp_path / "saved2")
    assert not (tmp_path / "saved2").exists()


def test_strip_ansi():
    from ai_factory.testresults import strip_ansi

    assert strip_ansi("\x1b[2m37:19\x1b[22m \x1b[31mFAIL\x1b[0m") == "37:19 FAIL"
