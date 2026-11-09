import { defineConfig } from "@playwright/test";

// Runs against an already-running ai-factory (see scripts/e2e.sh).
export default defineConfig({
  testDir: "e2e",
  timeout: 120_000,
  // One shared backend, and E-STOP is global: run the tests one at a time.
  workers: 1,
  use: {
    baseURL: process.env.AI_FACTORY_URL ?? "http://127.0.0.1:8700",
    screenshot: "only-on-failure",
    launchOptions: process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {},
  },
  outputDir: "e2e-results",
});
