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
await page.goto(appUrl, { waitUntil: "domcontentloaded", timeout: 120000 });
await page.waitForFunction(
  () => document.querySelectorAll("#grid [data-game]").length >= 12,
  { timeout: 60000 },
);

// Cover ratio measurement regroups the grid after images load, which swaps
// the img elements. Wait until the DOM is stable AND every cover is decoded
// before capturing, so no card is ever a placeholder in the screenshot.
async function waitForStableGrid() {
  for (let attempt = 0; attempt < 25; attempt++) {
    const previous = await page.evaluate(() =>
      [...document.querySelectorAll("#grid img")].map((img) => img.src).join("\n"),
    );
    await new Promise((resolve) => setTimeout(resolve, 400));
    const settled = await page.evaluate(async (prev) => {
      const images = [...document.querySelectorAll("#grid img")];
      if (images.map((img) => img.src).join("\n") !== prev) return false;
      if (!images.every((img) => img.complete && img.naturalWidth > 32)) return false;
      await Promise.all(images.map((img) => img.decode().catch(() => {})));
      return true;
    }, previous);
    if (settled) return;
  }
  throw new Error("Grid never stabilized with decoded covers.");
}

await waitForStableGrid();

if (mode === "bigbox") {
  // Navigate to Big Box via the deeplink, then wait for the overlay-in
  // animation (.22s fill-mode both) to finish and the cover art to be
  // decoded before capturing, so the screenshot is never a mid-fade frame.
  await page.goto(`${appUrl}&deeplink=bigbox`, { waitUntil: "domcontentloaded", timeout: 120000 });
  await page.waitForFunction(
    () => {
      const bigBox = document.querySelector("#bigBox");
      if (!bigBox || bigBox.hidden) return false;
      // overlay-in .22s still running -> opacity < 1
      if (getComputedStyle(bigBox).opacity !== "1") return false;
      const img = document.querySelector("#bigBox .bigbox-cover img");
      const coverReady = !img || (img.complete && img.naturalWidth > 32);
      const copy = document.querySelector("#bigBox .bigbox-copy h2");
      return coverReady && copy && copy.textContent.trim().length > 0;
    },
    { timeout: 60000 },
  );
  // One extra frame for the compositor after the animation end.
  await new Promise((resolve) => setTimeout(resolve, 400));
} else if (mode === "detail" || detailGameId !== "") {
  const gameId = detailGameId || mode;
  await page.click(`[data-game="${gameId}"]`);
  await page.waitForSelector("#details .play", { timeout: 30000 });
  await page.waitForFunction(
    () => {
      const hero = document.querySelector("#details .hero");
      if (!hero) return false;
      // surface-in .24s still running -> opacity < 1
      if (getComputedStyle(hero).opacity !== "1") return false;
      const inlineBg = hero.style.backgroundImage; // only set when has_background
      if (!inlineBg || inlineBg === "none") return true; // gradient-only hero
      const url = inlineBg.replace(/^url\(["']?/, "").replace(/["']?\)$/, "");
      const probe = new Image();
      probe.src = url;
      return probe.complete && probe.naturalWidth > 32;
    },
    { timeout: 60000 },
  );
  await new Promise((resolve) => setTimeout(resolve, 300));
  // Selecting a game re-renders the grid; wait for the new img elements to
  // settle and decode so no cover is a placeholder in the detail capture.
  await waitForStableGrid();
}

await page.screenshot({ path: outputPath, type: "png" });
await browser.close();
