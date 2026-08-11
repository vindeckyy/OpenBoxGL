import puppeteer from "puppeteer";

const [appUrl, outputPath, mode = "", detailGameId = ""] = process.argv.slice(2);
if (!appUrl || !outputPath) {
  console.error("Usage: node capture_screenshot_puppeteer.mjs <app-url> <output.png> [bigbox|detail|<game-id>]");
  process.exit(1);
}

const browser = await puppeteer.launch({
  headless: true,
  defaultViewport: { width: 1920, height: 1080 },
  args: ["--hide-scrollbars", "--force-device-scale-factor=1", "--no-sandbox", "--disable-setuid-sandbox"],
});
const page = await browser.newPage();
await page.goto(appUrl, { waitUntil: "networkidle0", timeout: 120000 });
await page.waitForFunction(
  () => document.querySelectorAll("#grid [data-game]").length >= 12,
  { timeout: 60000 },
);
await page.waitForFunction(
  () => {
    const images = [...document.querySelectorAll("#grid img")];
    return images.length >= 12 && images.every((img) => img.complete && img.naturalWidth > 32);
  },
  { timeout: 120000 },
);

if (mode === "bigbox") {
  // Navigate to Big Box via the deeplink, then wait for it to be visible
  // with the stage populated (startup video is disabled in the fixture).
  await page.goto(`${appUrl}&deeplink=bigbox`, { waitUntil: "networkidle0", timeout: 120000 });
  await page.waitForFunction(
    () => {
      const bigBox = document.querySelector("#bigBox");
      if (!bigBox || bigBox.hidden) return false;
      const stage = document.querySelector("#bigBoxStage");
      return stage && stage.childElementCount > 0;
    },
    { timeout: 60000 },
  );
  // Let the cover art and any transition settle before capturing.
  await page.waitForFunction(
    () => {
      const images = [...document.querySelectorAll("#bigBox img")];
      return images.some((img) => img.complete && img.naturalWidth > 32);
    },
    { timeout: 60000 },
  );
} else if (mode === "detail" || detailGameId !== "") {
  const gameId = detailGameId || mode;
  await page.click(`[data-game="${gameId}"]`);
  await page.waitForSelector("#details .play", { timeout: 30000 });
  await page.waitForFunction(
    () => {
      const hero = document.querySelector("#details .hero");
      if (!hero) return false;
      const background = getComputedStyle(hero).backgroundImage;
      if (background && background !== "none") return true;
      const images = [...document.querySelectorAll("#details img")];
      return images.some((img) => img.complete && img.naturalWidth > 32);
    },
    { timeout: 60000 },
  );
}

await page.screenshot({ path: outputPath, type: "png" });
await browser.close();
