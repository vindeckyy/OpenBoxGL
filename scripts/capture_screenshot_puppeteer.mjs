import puppeteer from "puppeteer";

const [appUrl, outputPath, detailGameId = ""] = process.argv.slice(2);
if (!appUrl || !outputPath) {
  console.error("Usage: node capture_screenshot_puppeteer.mjs <app-url> <output.png> [detail-game-id]");
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
  () => document.querySelectorAll("#grid .card[data-game]").length >= 12,
  { timeout: 60000 },
);
await page.waitForFunction(
  () => {
    const images = [...document.querySelectorAll("#grid img")];
    return images.length >= 12 && images.every((img) => img.complete && img.naturalWidth > 32);
  },
  { timeout: 120000 },
);
if (detailGameId !== "") {
  await page.click(`.card[data-game="${detailGameId}"]`);
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
