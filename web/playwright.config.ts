import { defineConfig, devices } from "@playwright/test";

// Smoke test of the built UI against a real `ais serve` on the seed tree (e2e/serve.sh).
// Port 8799, so it never fights the compose stack or the CI plugin smoke on :8000.
// E2E_BASE_URL points it at a server you started yourself instead (e.g. the compose stack
// with seed.yml imported and the e2e user created); then no server is started.
const port = Number(process.env.E2E_PORT ?? 8799);
const external = process.env.E2E_BASE_URL;

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  // No retries: the smoke asserts revisions 1→4 on a fresh database, so a second attempt
  // against the same server could only fail again, misleadingly.
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: external ?? `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: external
    ? undefined
    : {
        command: "bash e2e/serve.sh",
        url: `http://127.0.0.1:${port}/healthz`,
        reuseExistingServer: false,
        timeout: 60_000,
        env: { E2E_PORT: String(port) },
      },
});
