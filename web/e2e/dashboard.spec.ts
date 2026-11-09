import { expect, test, type APIRequestContext } from "@playwright/test";

// E2E against a live stack started by scripts/e2e.sh, which also creates the
// ORIGIN_REPO bare repository standing in for GitHub.
const ORIGIN = process.env.ORIGIN_REPO!;
const SHOTS = process.env.SHOTS_DIR ?? "e2e-results";

async function createProject(request: APIRequestContext, name: string) {
  const r = await request.post("/api/projects", {
    data: { name, repo_url: ORIGIN, executor_kind: "shell", test_cmd: "test -f README.md", skip_spec: true },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  return (await r.json()) as { id: number };
}

async function createTask(request: APIRequestContext, projectId: number, data: Record<string, unknown>) {
  const r = await request.post(`/api/projects/${projectId}/tasks`, { data });
  expect(r.ok(), await r.text()).toBeTruthy();
  return (await r.json()) as { id: number };
}

test("task runs to done and shows log + diff", async ({ page, request }) => {
  const p = await createProject(request, "e2e-happy");
  const t = await createTask(request, p.id, {
    title: "Write greeting",
    executor_script: "for i in 1 2 3; do echo step $i; sleep 1; done\necho 'GREETING = \"hi\"' > src/greet.py",
    validation_cmd: "grep -q hi src/greet.py",
    allowed_globs: ["src/*"],
  });

  await page.goto("/");
  await expect(page.locator("main").getByText("e2e-happy")).toBeVisible();
  await page.locator("main").getByText("e2e-happy").click();
  await expect(page.locator("main").getByRole("heading", { name: /e2e-happy/ })).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/project-running.png`, fullPage: true });

  await page.goto(`/tasks/${t.id}`);
  await expect(page.locator("main .event-log")).toContainText("step 3", { timeout: 60_000 });
  await expect(page.locator(".badge").first()).toHaveText(/done/i, { timeout: 60_000 });
  await page.getByRole("button", { name: "Diff" }).click();
  await expect(page.locator(".diff")).toContainText('GREETING = "hi"');
  await page.screenshot({ path: `${SHOTS}/task-done-diff.png`, fullPage: true });
});

test("big red stop button kills a runaway task", async ({ page, request }) => {
  const p = await createProject(request, "e2e-runaway");
  const t = await createTask(request, p.id, {
    title: "Runaway agent",
    executor_script: "echo 'partial work' > wip.txt\nwhile true; do echo 'still going...'; sleep 1; done",
    validation_cmd: "true",
  });

  await page.goto(`/tasks/${t.id}`);
  await expect(page.locator("main .event-log")).toContainText("still going", { timeout: 60_000 });
  await page.screenshot({ path: `${SHOTS}/task-runaway.png`, fullPage: true });

  const started = Date.now();
  await page.getByRole("button", { name: "STOP", exact: true }).click();
  await expect(page.locator("h1 .badge")).toHaveText(/stopped by user/i, { timeout: 10_000 });
  expect(Date.now() - started).toBeLessThan(10_000);
  await expect(page.locator("main .event-log")).toContainText("run killed");
  await page.getByRole("button", { name: "Diff" }).click();
  await expect(page.locator(".diff")).toContainText("partial work"); // work preserved
  await page.screenshot({ path: `${SHOTS}/task-stopped.png`, fullPage: true });
});

test("E-STOP ALL halts everything until cleared", async ({ page, request }) => {
  const p = await createProject(request, "e2e-estop");
  await createTask(request, p.id, {
    title: "Long job",
    executor_script: "while true; do sleep 1; done",
    validation_cmd: "true",
  });
  await page.goto(`/projects/${p.id}`);
  await expect(page.locator(".column").nth(1)).toContainText("Long job", { timeout: 60_000 });

  await page.getByRole("button", { name: "E-STOP ALL" }).click();
  await expect(page.locator(".estop-banner")).toBeVisible();
  await expect(page.locator(".column").nth(2)).toContainText("Long job", { timeout: 10_000 });
  await page.screenshot({ path: `${SHOTS}/estop.png`, fullPage: true });

  await page.getByRole("button", { name: "Clear E-stop" }).click();
  await expect(page.locator(".estop-banner")).toBeHidden();
});
