import { defineConfig } from "@playwright/test";
import path from "node:path";

const webRoot = process.cwd();
const repoRoot = path.resolve(webRoot, "..");
const port = process.env.BIZ_BROWSER_PORT ?? "3100";
const statePath = path.resolve(webRoot, "test-results/browser-state.json");

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  outputDir: "test-results/artifacts",
  reporter: "list",
  use: {
    baseURL: `http://localhost:${port}`,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
    },
  },
  webServer: {
    command: ".venv/bin/python deploy/browser_demo.py",
    cwd: repoRoot,
    url: `http://localhost:${port}/v1/health`,
    timeout: 120_000,
    reuseExistingServer: false,
    gracefulShutdown: { signal: "SIGTERM", timeout: 20_000 },
    env: { BIZ_BROWSER_STATE: statePath, BIZ_BROWSER_PORT: port },
  },
});
