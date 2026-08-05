import puppeteer from "puppeteer";
import fs from "fs";

const appUrl = process.argv[2];
const artifactDir = process.argv[3] || "/home/hayden/Desktop/Projects/OpenBox/specs/verifications/artifacts";
if (!appUrl) {
  console.error("Usage: node e03s03-browser-sweep.mjs <app-url> [artifact-dir]");
  process.exit(1);
}
fs.mkdirSync(artifactDir, { recursive: true });

const report = {
  journeys: {},
  consoleErrors: [],
  failedRequests: [],
  pageErrors: [],
};

const browser = await puppeteer.launch({
  headless: true,
  executablePath: "/usr/bin/google-chrome",
  defaultViewport: { width: 1920, height: 1080 },
  args: ["--hide-scrollbars", "--force-device-scale-factor=1", "--no-sandbox", "--disable-setuid-sandbox"],
});
const page = await browser.newPage();
page.on("console", (msg) => {
  if (msg.type() === "error") report.consoleErrors.push(msg.text().slice(0, 300));
});
page.on("pageerror", (err) => report.pageErrors.push(String(err).slice(0, 300)));
page.on("requestfailed", (req) => {
  const url = req.url();
  if (!url.includes("favicon")) report.failedRequests.push(`${req.failure()?.errorText || "failed"} ${url.slice(0, 200)}`);
});
page.on("response", (resp) => {
  if (resp.status() >= 400) {
    report.failedRequests.push(`HTTP ${resp.status()} ${resp.url().slice(0, 200)}`);
  }
});

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function textOf(selector) {
  return page.evaluate((sel) => (document.querySelector(sel)?.textContent || "").trim(), selector);
}

async function clickRobust(selector) {
  await page.evaluate((sel) => document.querySelector(sel)?.click(), selector);
  await sleep(150);
}

// Journey 1: initial load and library render.
report.journeys.load = {};
try {
  await page.goto(appUrl, { waitUntil: "networkidle0", timeout: 120000 });
  await page.waitForSelector("#grid .card", { timeout: 60000 });
  await sleep(500);
  const cards = await page.$$("#grid .card");
  const gridText = await textOf("#grid");
  report.journeys.load.cardCount = cards.length;
  report.journeys.load.namesVisible = ["Echo Canary", "True Fixture"].every((name) => gridText.includes(name));
  await page.screenshot({ path: `${artifactDir}/e03s03-library.png`, type: "png" });
} catch (error) {
  report.journeys.load.error = String(error);
}

// Journey 2: settings round-trip through the real form.
report.journeys.settings = {};
try {
  await clickRobust("#settingsButton");
  await page.waitForSelector("#settingsDialog[open]", { timeout: 30000 });
  await page.waitForFunction(
    () => {
      const input = document.querySelector('input[name="screensaver_seconds"], #screensaverSeconds');
      return input && input.value !== "";
    },
    { timeout: 30000 },
  );
  const secondsInput = await page.evaluate(() => {
    const input = document.querySelector('input[name="screensaver_seconds"], #screensaverSeconds');
    return input;
  });
  // Type into whichever control backs the screensaver delay.
  await page.evaluate((input) => {
    const el = document.querySelector('input[name="screensaver_seconds"], #screensaverSeconds');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(el, "60");
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }, secondsInput);
  await sleep(150);
  const saveBtn = await page.$("#settingsDialog button[type='submit'], #settingsDialog .save, #settingsSave");
  if (saveBtn) await saveBtn.click();
  else await page.evaluate(() => document.querySelector("#settingsDialog form")?.requestSubmit());
  await sleep(800);
  const settings = await page.evaluate(async (token) => {
    const resp = await fetch("/api/settings", { headers: { "X-OpenBox-Token": token } });
    return resp.json();
  }, appUrl.split("token=")[1].split("&")[0]);
  report.journeys.settings.screensaver_seconds = settings.screensaver_seconds;
  report.journeys.settings.storefront_preserved = JSON.stringify(settings.storefront_auto_import) ===
    JSON.stringify({ steam: false, heroic: false, lutris: false, gameyfin: false });
  report.journeys.settings.password_hidden = !("gameyfin_password" in settings) && settings.gameyfin_password_set === true;
  await page.screenshot({ path: `${artifactDir}/e03s03-settings.png`, type: "png" });
  await page.evaluate(() => document.querySelector("#settingsDialog")?.close());
} catch (error) {
  report.journeys.settings.error = String(error);
}

// Journey 3: storefront partial save must not clobber general settings.
report.journeys.storefront = {};
try {
  await clickRobust("#storefrontButton");
  await page.waitForSelector("#storefrontDialog[open]", { timeout: 30000 });
  await page.evaluate(() => {
    const target = document.querySelector("#storefrontAutoImportSteam");
    if (target && !target.checked) target.click();
  });
  await sleep(150);
  await page.click("#saveStorefront");
  await sleep(800);
  const settings2 = await page.evaluate(async (token) => {
    const resp = await fetch("/api/settings", { headers: { "X-OpenBox-Token": token } });
    return resp.json();
  }, appUrl.split("token=")[1].split("&")[0]);
  report.journeys.storefront.steam_enabled = settings2.storefront_auto_import?.steam === true;
  report.journeys.storefront.screensaver_preserved = settings2.screensaver_seconds === 60;
  await page.screenshot({ path: `${artifactDir}/e03s03-storefront.png`, type: "png" });
  await page.evaluate(() => document.querySelector("#storefrontDialog")?.close());
} catch (error) {
  report.journeys.storefront.error = String(error);
}

// Journey 3b: favicon routes must answer 200 (I12).
report.journeys.favicon = {};
try {
  for (const path of ["/favicon.svg", "/favicon.ico"]) {
    const status = await page.evaluate(async (p) => (await fetch(p)).status, path);
    report.journeys.favicon[path] = status;
  }
} catch (error) {
  report.journeys.favicon.error = String(error);
}

// Journey 4: launch a SAFE_MODE game through the real play control.
report.journeys.launch = {};
try {
  const cardId = await page.evaluate(() => document.querySelector("#grid .card-main")?.dataset.game || "");
  report.journeys.launch.cardId = cardId;
  await page.click(`#grid .card-main[data-game="${cardId}"]`);
  await page.waitForSelector("#details .play", { timeout: 30000 });
  await sleep(300);
  await page.click("#details .play");
  await sleep(1200);
  report.journeys.launch.clicked = true;
  await page.screenshot({ path: `${artifactDir}/e03s03-launch.png`, type: "png" });
  // Stop the session through the real control to keep the fixture clean.
  const stopBtn = await page.$("#details .stop, #nowPlaying .stop, #sessionStop");
  if (stopBtn) {
    await stopBtn.click();
    await sleep(800);
  }
} catch (error) {
  report.journeys.launch.error = String(error);
}

// Journey 5: update check inside the settings dialog opens without exception.
report.journeys.updates = {};
try {
  // Reload to clear any modal state left by the launch journey.
  await page.goto(appUrl, { waitUntil: "networkidle0", timeout: 120000 });
  await page.waitForSelector("#grid .card", { timeout: 60000 });
  await sleep(400);
  await clickRobust("#settingsButton");
  await page.waitForSelector("#settingsDialog[open]", { timeout: 30000 });
  const hasCheck = await page.$("#checkUpdate");
  report.journeys.updates.check_button_present = Boolean(hasCheck);
  if (hasCheck) {
    await page.click("#checkUpdate");
    await sleep(2000);
    report.journeys.updates.status = await textOf("#updateStatus");
  }
  await page.screenshot({ path: `${artifactDir}/e03s03-updates.png`, type: "png" });
  await page.evaluate(() => document.querySelector("#settingsDialog")?.close());
} catch (error) {
  report.journeys.updates.error = String(error);
}

// Final library integrity check.
report.journeys.final = {};
try {
  await page.evaluate(async (token) => {
    await fetch("/api/library", { headers: { "X-OpenBox-Token": token } });
  }, appUrl.split("token=")[1].split("&")[0]);
  await sleep(600);
  const cards = await page.$$("#grid .card");
  report.journeys.final.cardCount = cards.length;
  await page.screenshot({ path: `${artifactDir}/e03s03-final.png`, type: "png" });
} catch (error) {
  report.journeys.final.error = String(error);
}

await browser.close();
report.summary = {
  journeys: Object.keys(report.journeys),
  consoleErrors: report.consoleErrors.length,
  pageErrors: report.pageErrors.length,
  failedRequests: report.failedRequests,
};
console.log(JSON.stringify(report, null, 2));
