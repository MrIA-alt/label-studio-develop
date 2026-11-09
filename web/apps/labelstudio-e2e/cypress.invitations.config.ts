import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig } from "cypress";

export default defineConfig({
  video: false,
  viewportWidth: 1440,
  viewportHeight: 1000,
  defaultCommandTimeout: 30000,
  pageLoadTimeout: 60000,
  screenshotsFolder: resolve(__dirname, "../../../collab-test-results/invitations"),
  e2e: {
    baseUrl: process.env.COLLAB_HTTP_URL ?? "http://localhost:18081",
    specPattern: "src/e2e/invitations.cy.ts",
    supportFile: false,
    setupNodeEvents(on) {
      on("task", {
        invitationFixture() {
          if (!process.env.COLLAB_FIXTURE_PATH) throw new Error("Set the private synthetic invitation fixture path.");
          const fixture = JSON.parse(readFileSync(process.env.COLLAB_FIXTURE_PATH, "utf8"));
          const match = fixture.admin?.match(/^(invite-[a-f0-9]+)-admin@example\.test$/);
          if (!match || !Number.isInteger(fixture.project) || !Number.isInteger(fixture.task) ||
              !["alice", "bob"].every((name) => fixture.emails?.[name] === `${match[1]}-${name}@example.test` &&
                fixture.links?.[name]?.startsWith(`${process.env.COLLAB_HTTP_URL ?? "http://localhost:18081"}/user/invitation/#token=`))) {
            throw new Error("Only isolated synthetic invitation fixtures are allowed.");
          }
          return fixture;
        },
      });
    },
  },
});
