import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig } from "cypress";

export default defineConfig({
  video: false,
  viewportWidth: 1440,
  viewportHeight: 1000,
  defaultCommandTimeout: 30000,
  requestTimeout: 45000,
  responseTimeout: 45000,
  pageLoadTimeout: 60000,
  taskTimeout: 45000,
  numTestsKeptInMemory: 0,
  experimentalMemoryManagement: true,
  screenshotsFolder: resolve(__dirname, "../../../collab-test-results/browser"),
  e2e: {
    baseUrl: process.env.COLLAB_HTTP_URL ?? "http://localhost:18080",
    specPattern: "src/e2e/collaboration.cy.ts",
    supportFile: false,
    setupNodeEvents(on) {
      on("task", {
        campaignFixture() {
          const filename = process.env.COLLAB_FIXTURE_PATH;
          if (!filename) throw new Error("Set COLLAB_FIXTURE_PATH to the private acceptance fixture file.");
          const fixture = JSON.parse(readFileSync(filename, "utf8"));
          const adminEmail = fixture.users?.admin?.email;
          const match = typeof adminEmail === "string" && adminEmail.match(/^(collab-[a-f0-9]+)-admin@example\.test$/);
          if (!match || !Number.isInteger(fixture.project) || fixture.project < 1 ||
              !["admin", "qa", "alice", "bob", "charlie"].every((name) =>
                fixture.users?.[name]?.email === `${match[1]}-${name}@example.test`)) {
            throw new Error("Use only a synthetic acceptance campaign fixture; personal demonstration accounts are excluded.");
          }
          return fixture;
        },
      });
    },
  },
});
