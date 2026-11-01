import { defineConfig, devices } from "@playwright/test";

// Smoke test of the built UI against a real `ais serve` on the seed tree (e2e/serve.sh).
// Port 8799, so it never fights the compose stack or the CI plugin smoke on :8000.
const port = Number(process.env.E2E_PORT ?? 8799);

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "bash e2e/serve.sh",
    url: `http://127.0.0.1:${port}/healthz`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: { E2E_PORT: String(port) },
  },
});
