import { defineConfig, devices } from "@playwright/test";

// Browser tests of the whole system: the real API (with a stand-in model, backend/tests/e2e_server.py) and
// this website. Needs a Postgres with pgvector; its database is emptied, so use one named *_e2e:
//   docker compose exec postgres createdb -U consult consult_e2e     (once)
//   npx playwright install chromium                                  (once)
//   E2E_DATABASE_URL=postgresql://consult:consult@localhost:5432/consult_e2e npm run test:e2e
// Locally, servers already running on :8000 and :3000 are reused (start the API with tests.e2e_server).
const CI = Boolean(process.env.CI);
const python = process.env.E2E_PYTHON || "python";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,          // the tests share one database
  workers: 1,
  retries: CI ? 1 : 0,
  reporter: CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: { baseURL: "http://localhost:3000", trace: "retain-on-failure" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] }, testIgnore: /phone\.spec\.ts/ },
    { name: "phone", use: { ...devices["Pixel 7"] }, testMatch: /phone\.spec\.ts/ },
  ],
  webServer: [
    { command: `${python} -m tests.e2e_server`, cwd: "../backend", url: "http://127.0.0.1:8000/health",
      reuseExistingServer: !CI, timeout: 120_000 },
    { command: CI ? "npm run start" : "npm run dev", url: "http://localhost:3000", reuseExistingServer: !CI,
      timeout: 120_000 },
  ],
});
