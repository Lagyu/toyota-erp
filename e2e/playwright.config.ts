// End-to-end suite: runs the mandatory journeys (J-01..J-10) and the role surfaces
// (internal personas, supplier and dealer portals) against a reference site loaded with
// the synthetic dataset. Run through scripts/ci/e2e.sh (pinned Playwright image).
import { defineConfig, devices } from "@playwright/test";

const reportDir = process.env.E2E_REPORT_DIR ?? "../.local/e2e";

export default defineConfig({
	testDir: "./tests",
	timeout: 180_000,
	expect: { timeout: 20_000 },
	fullyParallel: false, // the journeys share one dataset and one simulated clock
	workers: 1,
	// no retries: a journey changes the shared dataset, so a second attempt would not start from
	// the state the first one found (and would fail for a different reason)
	retries: 0,
	reporter: [
		["list"],
		["html", { outputFolder: `${reportDir}/report`, open: "never" }],
		["junit", { outputFile: `${reportDir}/junit.xml` }],
	],
	globalSetup: "./support/global-setup.ts",
	use: {
		baseURL: process.env.E2E_BASE_URL ?? "http://small.localhost:18000",
		actionTimeout: 20_000, // a click that cannot happen fails fast instead of eating the test timeout
		navigationTimeout: 60_000,
		trace: "retain-on-failure",
		screenshot: "only-on-failure",
		video: "retain-on-failure",
		locale: "en-US",
		timezoneId: "Asia/Tokyo",
	},
	projects: [
		{ name: "desktop", use: { ...devices["Desktop Chrome"] } },
		// plant-floor and partner screens are used on tablets
		{ name: "tablet", use: { ...devices["iPad Pro 11 landscape"] }, testMatch: /(shop-floor|portal).*\.spec\.ts/ },
	],
});
