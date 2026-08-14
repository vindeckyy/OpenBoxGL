// Capture additional OpenBox UI views for GitHub screenshots.
// Usage: node scripts/capture_extra_views.mjs <app-url> <outdir>
import puppeteer from "puppeteer";
import { mkdirSync } from "node:fs";
import path from "node:path";

const [appUrl, outdir] = process.argv.slice(2);
if (!appUrl || !outdir) {
  console.error("Usage: node scripts/capture_extra_views.mjs <app-url> <outdir>");
  process.exit(1);
}
mkdirSync(outdir, { recursive: true });

const browser = await puppeteer.launch({
  headless: true,
  defaultViewport: { width: 1920, height: 1080 },
  args: ["--hide-scrollbars", "--force-device-scale-factor=1", "--no-sandbox", "--disable-setuid-sandbox"],
});
const page = await browser.newPage();
await page.goto(appUrl, { waitUntil: "domcontentloaded", timeout: 120000 });
await page.waitForFunction(() => document.querySelectorAll("#grid [data-game]").length >= 12, { timeout: 60000 });
await page.waitForFunction(() => {
  const images = [...document.querySelectorAll("#grid img")];
  return images.length >= 12 && images.every((img) => img.complete && img.naturalWidth > 32);
}, { timeout: 120000 });

const shot = async (name) => {
  await new Promise((r) => setTimeout(r, 800));
  const out = path.join(outdir, name);
  await page.screenshot({ path: out, type: "png" });
  console.log("saved", out);
};

// 1. Library grid (default)
await shot("openbox-library-grid.png");

// 2. List view
await page.evaluate(() => {
  const btn = document.querySelector("#viewToggleButton");
  if (btn) btn.click();
});
await new Promise((r) => setTimeout(r, 800));
await page.waitForFunction(() => document.querySelectorAll("#grid .list-row").length >= 12, { timeout: 30000 });
await shot("openbox-library-list.png");
// back to grid
await page.evaluate(() => document.querySelector("#viewToggleButton")?.click());
await new Promise((r) => setTimeout(r, 800));

// 3. Filter menu open (sidebar categories expanded / search focused)
await page.evaluate(() => {
  const search = document.querySelector("#sidebarSearch");
  if (search) search.focus();
});
await new Promise((r) => setTimeout(r, 400));
await shot("openbox-library-search.png");

// 4. Tools menu open
await page.evaluate(() => {
  const tools = document.querySelector(".topbar-tools summary");
  if (tools) tools.click();
});
await new Promise((r) => setTimeout(r, 600));
await shot("openbox-tools-menu.png");
await page.evaluate(() => {
  const tools = document.querySelector(".topbar-tools summary");
  if (tools && document.querySelector(".topbar-tools[open]")) tools.click();
});

// 5. Settings dialog
await page.evaluate(() => {
  const btn = document.querySelector("[data-open-settings], #settingsButton, button[aria-label*='Settings' i]");
  if (btn) btn.click();
});
await new Promise((r) => setTimeout(r, 800));
const settingsOpen = await page.evaluate(() => {
  const dlg = document.querySelector("dialog[open]");
  return dlg ? dlg.id || dlg.className || "dialog" : null;
});
if (settingsOpen) {
  await shot("openbox-settings.png");
}

// 6. A game's detail view with hero art
await page.evaluate(() => {
  const card = document.querySelector("#grid [data-game]");
  if (card) card.click();
});
await page.waitForSelector("#details .play", { timeout: 30000 });
await new Promise((r) => setTimeout(r, 1200));
await shot("openbox-detail.png");

// 7. Big Box
await page.goto(`${appUrl}&deeplink=bigbox`, { waitUntil: "domcontentloaded", timeout: 120000 });
await page.waitForFunction(() => {
  const bigBox = document.querySelector("#bigBox");
  if (!bigBox || bigBox.hidden) return false;
  const stage = document.querySelector("#bigBoxStage");
  return stage && stage.childElementCount > 0;
}, { timeout: 60000 });
await page.waitForFunction(() => {
  const images = [...document.querySelectorAll("#bigBox img")];
  return images.some((img) => img.complete && img.naturalWidth > 32);
}, { timeout: 60000 });
await new Promise((r) => setTimeout(r, 1200));
await shot("openbox-bigbox.png");

await browser.close();
