// ai-factory's app check, browser half: open each page of the running app in headless
// Chromium (Playwright), record what a person would notice (console errors, uncaught
// errors, failed requests, a page with nothing on it) and take a screenshot.
//
//   APP_URL=http://127.0.0.1:8799 node appcheck.cjs / /recipes ...
//
// Without Playwright in the toolchain image it falls back to plain HTTP: each page must
// answer with a 2xx/3xx and a body, and the browser part is reported as skipped.
// CommonJS on purpose: NODE_PATH (set by appcheck.sh to wherever npm put Playwright)
// only applies to require().
"use strict";

const fs = require("fs");
const path = require("path");

const base = (process.env.APP_URL || "http://127.0.0.1:8799").replace(/\/$/, "");
const results = process.env.AI_FACTORY_RESULTS || path.join(process.cwd(), "results");
const reportDir = path.join(results, "report", "app");
// Each page once (tests/app-pages.txt often lists / too), / first.
const pages = [...new Set(["/", ...process.argv.slice(2)].map((p) => (p.startsWith("/") ? p : "/" + p)))];
const PAGE_TIMEOUT_MS = 30000;
const SETTLE_MS = 750;
const VIEWPORT = { width: 1440, height: 900 };
// A page this much wider than the window scrolls sideways: a layout problem, not a
// wide table.
const MAX_WIDTH = Math.round(VIEWPORT.width * 1.5);
// Screenshots stop here, so a runaway page still gives a viewable image.
const MAX_SHOT_HEIGHT = 8000;

fs.mkdirSync(reportDir, { recursive: true });

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function shotName(p) {
  const slug = p.replace(/^\/+|\/+$/g, "").replace(/[^A-Za-z0-9._-]+/g, "_") || "index";
  return `${slug}.png`;
}

function loadPlaywright() {
  for (const name of ["playwright", "playwright-core", "@playwright/test"]) {
    try {
      const pw = require(name);
      if (pw && pw.chromium) return pw;
    } catch (_) {
      // try the next one
    }
  }
  return null;
}

async function launch(pw) {
  try {
    return await pw.chromium.launch();
  } catch (e) {
    // Playwright without its own Chromium (the local runtime on a Mac, say): an
    // installed Google Chrome does the same job.
    try {
      return await pw.chromium.launch({ channel: "chrome" });
    } catch (_) {
      throw e;
    }
  }
}

async function checkWithBrowser(pw) {
  const browser = await launch(pw);
  const out = [];
  try {
    for (const p of pages) {
      const url = base + (p.startsWith("/") ? p : "/" + p);
      const page = await browser.newPage({ viewport: VIEWPORT });
      const problems = [];
      const origin = new URL(base).origin;
      page.on("console", (m) => {
        // "Failed to load resource" repeats a failed response, which is reported below with
        // its URL (and a missing favicon isn't a problem).
        if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) {
          problems.push(`console error: ${m.text()}`);
        }
      });
      page.on("pageerror", (e) => problems.push(`uncaught error: ${e.message}`));
      page.on("requestfailed", (r) => {
        if (r.url().startsWith(origin)) {
          problems.push(`request failed: ${r.method()} ${r.url()} (${r.failure()?.errorText})`);
        }
      });
      page.on("response", (r) => {
        const u = r.url();
        if (u.startsWith(origin) && r.status() >= 400 && !u.endsWith("/favicon.ico")) {
          problems.push(`HTTP ${r.status()}: ${r.request().method()} ${u}`);
        }
      });
      let status = 0;
      try {
        const resp = await page.goto(url, { waitUntil: "networkidle", timeout: PAGE_TIMEOUT_MS });
        status = resp ? resp.status() : 0;
        await page.waitForTimeout(SETTLE_MS);
        const text = (await page.evaluate(() => document.body?.innerText || "")).trim();
        if (!text) problems.push("the page shows no text: it renders blank");
        const width = await page.evaluate(() => document.documentElement.scrollWidth);
        if (width > MAX_WIDTH) {
          problems.push(
            `the page is ${width}px wide in a ${VIEWPORT.width}px window: it scrolls sideways`,
          );
        }
      } catch (e) {
        problems.push(`the page did not load: ${e.message.split("\n")[0]}`);
      }
      const shot = shotName(p);
      try {
        const height = await page.evaluate(() => document.documentElement.scrollHeight);
        await page.screenshot({
          path: path.join(reportDir, shot),
          fullPage: true,
          clip: { x: 0, y: 0, width: VIEWPORT.width, height: Math.min(height, MAX_SHOT_HEIGHT) },
        });
      } catch (_) {
        // a page that never loaded may not screenshot; the problem is already recorded
      }
      await page.close();
      out.push({ path: p, url, status, problems: [...new Set(problems)], screenshot: shot });
    }
  } finally {
    await browser.close();
  }
  return out;
}

async function checkWithHttp() {
  const out = [];
  for (const p of pages) {
    const url = base + (p.startsWith("/") ? p : "/" + p);
    const problems = [];
    let status = 0;
    try {
      const resp = await fetch(url, { redirect: "follow" });
      status = resp.status;
      const body = (await resp.text()).trim();
      if (status >= 400) problems.push(`HTTP ${status}`);
      else if (!body) problems.push("the page is empty");
    } catch (e) {
      problems.push(`the page did not load: ${e.message}`);
    }
    out.push({ path: p, url, status, problems, screenshot: "" });
  }
  return out;
}

function writeJunit(rows, browser) {
  const cases = rows.map((r) => {
    const name = `page ${r.path} works`;
    if (!r.problems.length) return `  <testcase classname="app" name="${esc(name)}"/>`;
    const msg = r.problems[0];
    return (
      `  <testcase classname="app" name="${esc(name)}">\n` +
      `    <failure message="${esc(msg)}">${esc(r.problems.join("\n"))}</failure>\n` +
      `  </testcase>`
    );
  });
  if (!browser) {
    cases.push(
      `  <testcase classname="app" name="pages render in a browser">\n` +
        `    <skipped message="Playwright is not installed in the toolchain image"/>\n` +
        `  </testcase>`,
    );
  }
  const failures = rows.filter((r) => r.problems.length).length;
  fs.writeFileSync(
    path.join(results, "junit.xml"),
    `<?xml version="1.0" encoding="UTF-8"?>\n` +
      `<testsuite name="app" tests="${cases.length}" failures="${failures}">\n` +
      cases.join("\n") +
      `\n</testsuite>\n`,
  );
}

function writeReport(rows, browser) {
  const items = rows
    .map((r) => {
      const status = r.problems.length
        ? `<ul class="bad">${r.problems.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
        : `<p class="ok">No problems found.</p>`;
      const img = r.screenshot
        ? `<a href="${esc(r.screenshot)}"><img src="${esc(r.screenshot)}" alt="${esc(r.path)}"></a>`
        : "";
      return `<section><h2><code>${esc(r.path)}</code></h2>${status}${img}</section>`;
    })
    .join("\n");
  const note = browser
    ? ""
    : `<p class="bad">Playwright isn't installed in the toolchain image, so the pages were only fetched, not rendered. Rebuild the environment to add it.</p>`;
  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>App check</title>
<style>
  body { font: 14px/1.5 system-ui, sans-serif; margin: 24px; color: #1d1d1f; background: #fff; }
  h1 { font-size: 20px; } h2 { font-size: 15px; margin: 24px 0 8px; }
  img { max-width: 100%; border: 1px solid #ccc; border-radius: 4px; }
  .ok { color: #1a7f37; } .bad { color: #b42318; }
  @media (prefers-color-scheme: dark) { body { background: #111; color: #eee; } img { border-color: #444; } }
</style></head>
<body><h1>App check</h1><p>${esc(base)}, ${rows.length} page(s)</p>${note}
${items}
</body></html>
`;
  fs.writeFileSync(path.join(reportDir, "index.html"), html);
}

(async () => {
  const pw = loadPlaywright();
  let pageResults;
  try {
    pageResults = pw ? await checkWithBrowser(pw) : await checkWithHttp();
  } catch (e) {
    // Playwright is there but Chromium won't launch: say so plainly.
    pageResults = pages.map((p) => ({
      path: p,
      url: base + p,
      status: 0,
      problems: [`the browser could not run: ${String(e.message || e).split("\n")[0]}`],
      screenshot: "",
    }));
  }
  const browser = Boolean(pw);
  const problems = pageResults.flatMap((r) => r.problems.map((x) => `${r.path}: ${x}`));
  fs.writeFileSync(
    path.join(results, "app.json"),
    JSON.stringify({ started: true, browser, problems, pages: pageResults }, null, 1) + "\n",
  );
  writeJunit(pageResults, browser);
  writeReport(pageResults, browser);
  for (const r of pageResults) {
    console.log(`app check: ${r.path} ${r.problems.length ? "FAILED" : "ok"}`);
    for (const x of r.problems) console.log(`  - ${x}`);
  }
  if (!browser) console.log("app check: Playwright not installed; pages fetched, not rendered");
  process.exit(problems.length ? 1 : 0);
})();
