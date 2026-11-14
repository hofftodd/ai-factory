# Post-mortem: the larder runs (November 2026)

The second test run of ai-factory, on a medium project: larder, a meal planner with a
Kotlin / Spring Boot + PostgreSQL back end and an Angular + Material front end. It
checked the PRs that came out of the [future-lab evaluation](../2026-11-future-lab/)
(#30–#35) on a stack other than Python and React. It ran twice, and the second run was
cancelled.

| | larder (project 3) | larder-2 (project 4) |
|---|---|---|
| Models | copied from future-lab (mixed) | deepseek-v4-flash on every role |
| Ran | 11-12 12:28 to 11-13 07:59 (19h 31m) | 11-13 07:59 to 11-14, cancelled (about 25h) |
| Planned tasks | 21 | 26 |
| Planned tasks done | 2 (setup and acceptance), no feature | 13: setup, acceptance and all 11 back-end features |
| Front-end features done | 0 | 0 of 11 (only the route table, from a split task) |
| Tasks added by splitting | 16 | 13 |
| Agent runs / failed | 68 / 45 | 88 / 35 |
| Tokens in | 153M | 57M |
| ai-factory PRs merged during the run | 4 (#36–#39) | 7 (#40–#46) |

Sources: both projects' run reports (`GET /api/projects/{id}/report`), the run logs and
results on the factory host, and the notes kept while monitoring.

## Summary

**The back end worked and the front end never got started.** With deepseek-v4-flash on
every role, larder-2 built the whole back end by 19:00 on the first day, about ten hours
after setup started. Eight of its 11
features passed on the first attempt, and the Reviewer caught one real design problem
(T8). Then all the remaining time went on the front end's *test setup*, not on any
front-end feature. Six ai-factory bugs surfaced one after another, each found only when
the one before it was fixed.

The single cause behind most of it: **nothing proved the test setup worked before
feature work began.** Setup was accepted with a front-end suite that only ran when an
agent happened to install the packages by hand. Its tests were named in a way the
factory couldn't link to tasks, and its coverage report counted nothing. None of that
showed until the first front-end task, eight hours in. From then on, every failure came
back as "the Tester's tests don't fail first" (`no_red`) or "the Tester added no tests"
(`no_tests`). The factory blamed the agent, and escalation split the task into ever
smaller tasks about the test runner itself.

## Timeline of larder-2

| When | What happened | What it exposed | Fix |
|---|---|---|---|
| 11-13 08:07 | plan approved; the toolchain image failed to build twice | a file block left open swallowed `check.sh`; no `xz` in the base image; npm peer conflicts | #40 |
| 08:46–13:00 | setup, acceptance tests, T1–T6 | the back end's coverage counted as 0%: JaCoCo wasn't read | #41 |
| 12:57–14:20 | T7 and T8 failed with "tests fail: 0 of 91 failed" | the image has no project `node_modules`, and `tests/run.sh` never installed them; the message hid the cause | #42 |
| 13:03–15:52 | T12 (app shell) rejected as `no_red` 3 times, then split | (seen later) the same path-matching bug as T12.2.1 | #45 |
| 15:52–16:53 | T12.1 "test runner installs packages" rejected as `no_red`: it changes only `tests/run.sh`, so no test can fail first | runner-only tasks had no path to merge | #43 |
| 16:53–18:38 | T12.1.1 failed 6 times | the Tester waited for a Builder that may not edit `tests/run.sh`; setup had gitignored `package-lock.json`, so `npm ci` couldn't work | #44, plus a note by hand |
| 19:00–20:07 | T12.1.2 and T12.1.3 tested the test runner; T12.1.3 hit a merge conflict and was discarded; the runner now reinstalled `node_modules` on every run | splitting turns a harness problem into tasks about the harness | #125, added by hand |
| 20:25–05:23 | T12.2, then T12.2.1, rejected 3× `no_red` and 4× `no_tests`, idle overnight at a checkpoint | vitest names cases `src/app/x.spec.ts` (relative to `frontend/`), so a task's failing tests didn't count as its own; a retried Tester's committed tests weren't counted | #45, #46 |
| 11-14 07:46 | T12.2.1 built, then stopped at the coverage checkpoint: 0% | Angular's test runner with coverage set in `vitest.config.ts` maps nothing back to the source | approved by hand; #129 added |
| 08:09–08:20 | #129 fixed coverage, but was rejected as `no_red` 3× and split | #43's rule misses a helper script under `tests/` (`check-coverage.js`) | not fixed: run cancelled |

## What went wrong

### 1. Setup never proved the tests run

Setup passed on its own suite run. Every later problem was already there at that point:
- the front end's packages weren't installed in a fresh checkout;
- the lockfile was gitignored;
- vitest named cases by a path the factory couldn't match;
- the front end's coverage counted one line per file at 0%.

Each showed up only when a later task depended on it, and each cost hours to find under
another name. Setup is the one place where the factory, not an agent, should check the
test setup end to end.

### 2. Factory problems were reported as agent failures

Of larder-2's 35 failed runs, 22 were `no_red` or `no_tests`. Most of them came from
#43, #45 or #46-type bugs, not from the Tester. The run report puts all of them under
"agent: the agent's work", and every message told the Tester it had written the wrong
tests. The Testers then did odd things:
- wrote checks that fail until "someone else" installs packages;
- changed nothing at all.

A plain signal was ignored: the suite's own JUnit output showed failing tests, while the
factory said none of the task's tests failed.

### 3. Escalation by splitting made harness problems worse

13 tasks came from splits, most of them about the test runner: "Runner handles install
failure and repeated runs", "Full runner still produces back-end results". Splitting
assumes the task was too big. When the same `no_red` or `no_tests` signature repeats, the
task isn't too big: the factory or the test setup is wrong, and smaller tasks inherit the
problem. They also start from the milestone branch, losing work the parent's branch
already had (#129 fixed coverage; its split children started again without that fix).

### 4. Every fix needed a person, and the run waited

- **Stalls:** larder-2 spent 11h 24m in stalls of 20 minutes or more, mostly waiting at
  checkpoints, including 8h 22m overnight.
- **Deploys:** every factory fix needed a pause and a drain (up to 20 minutes) before
  the deploy.
- **Hand edits to the plan:** two tasks added, two discarded, and notes on 15 tasks.

### 5. Mistakes in the monitoring

- **The `npm ci` note:** the note added to every queued task suggested `npm ci` without
  checking for a lockfile, and there wasn't one. The planner also turned the note into a
  task of its own (T12.1), which started the run of test-runner tasks.
- **No factory-bug marks:** no checkpoint was marked as a factory bug when it was
  resolved, so the run report's causes are wrong.
- **One wrong report:** I once said #40 was deployed when it was only merged.

## What worked

- **The back end:** T1–T6, T9 and T10 each passed on the first attempt, in 25–48
  minutes. T7, T8 and T11 needed 3–4 attempts, mostly because of the front-end test
  problems above. The back-end tests are real Spring Boot / JUnit tests, with JaCoCo
  coverage since #41.
- **The Reviewer:** it found a real problem in T8 (the shopping-list computation lived
  in a controller, against the design). Sent back with a note, the Builder fixed it.
- **The stronger model:** switching every role to deepseek-v4-flash turned larder (4
  tasks done in 19.5h) into larder-2 (17 in about 25h) on the same documents.
- **The app check:** it ran 17 times with no problems. It didn't help much here, since
  there were no front-end pages.
- **The fixes themselves:** #39–#46 each came with a test that reproduces the larder
  failure. The full suite went from 425 to 429 tests.

## Recommendations

In priority order:

1. **Check the test setup at setup, in ai-factory's own code.** After the setup task's
   suite passes, run the suite again in a fresh clone in the toolchain image, then
   require for every module:
   - at least one JUnit case;
   - the case files map to the module's test files with `is_task_case`;
   - every coverage file maps to the repository's source, with more than one counted
     line per file and some lines covered.

   Fail setup with the specific problem. This would have caught #42, #45, the lockfile
   and the front end's coverage in the first hour instead of over the following 24.
2. **Diagnose `no_red` before blaming the Tester.** When the JUnit output has failing
   cases but none match the task's files, say so and mark the run `internal`. When
   `no_red` or `no_tests` repeats with the same signature, open a checkpoint with the
   evidence instead of retrying or splitting.
3. **Don't split on repeated `no_red`, `no_tests` or test-setup failures.** When a split
   does happen, keep the parent's branch work available to the children.
4. **Widen #43's rule** to helper scripts under `tests/` that contain none of the task's
   test cases (the open #129 case).
5. **Show discarded tasks to the plan review**, so their IDs don't read as missing tasks
   (checkpoint 21 proposed a document change because of it).
6. **Rehearse the stack before a long run.** `scripts/rehearsal.py` covers Python and
   React only. A Kotlin + Angular rehearsal would have surfaced recommendations 1–4 in a
   few hours rather than two days.
7. **Mark factory bugs when resolving checkpoints**, so the run report's causes are
   usable.

## Where things are

- **larder-2:** project 4 is paused, with nothing deleted. Its work is on the
  `ai-factory/m1` branch of hofftodd/larder-2: the back end, the front end's route
  table, and a `tests/run.sh` that installs the packages.
- **larder:** project 3 has been paused since 11-13.
- **ai-factory:** the factory host is on `726661f` (#46). Recommendation 4 and the
  front end's coverage settings are still open.
