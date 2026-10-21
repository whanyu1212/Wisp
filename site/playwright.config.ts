import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  outputDir: "./test-results",
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 2,
  use: {
    baseURL: "http://127.0.0.1:4322/Wisp/",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "tablet", use: { viewport: { width: 1024, height: 768 } } },
    {
      name: "mobile",
      use: { ...devices["iPhone 13"], browserName: "chromium" },
    },
  ],
  webServer: {
    command: "npm run preview -- --port 4322 --ignore-lock",
    url: "http://127.0.0.1:4322/Wisp/",
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
