// Measure the details panel scroll metrics after the CSS fix.
// Usage: node scripts/measure-details.mjs <app-url>
import puppeteer from "puppeteer";

const [appUrl] = process.argv.slice(2);
if (!appUrl) {
  console.error("Usage: node scripts/measure-details.mjs <app-url>");
  process.exit(1);
}

const browser = await puppeteer.launch({
  headless: true,
  defaultViewport: { width: 1920, height: 1080 },
  args: ["--hide-scrollbars", "--force-device-scale-factor=1", "--no-sandbox", "--disable-setuid-sandbox"],
});
const page = await browser.newPage();
await page.goto(appUrl, { waitUntil: "domcontentloaded", timeout: 60000 });

// Wait for the grid to render at least one game
await page.waitForFunction(
  () => document.querySelectorAll("#grid [data-game]").length >= 1,
  { timeout: 60000 },
);

// Select the first game card
await page.evaluate(() => {
  const card = document.querySelector("#grid [data-game]");
  if (card) card.click();
});

// Give the details render a moment
await new Promise((r) => setTimeout(r, 1500));

const metrics = await page.evaluate(() => {
  const details = document.querySelector(".details");
  const detailBody = document.querySelector(".detail-body");
  const grid = document.querySelector(".grid");
  const lib = document.querySelector("main.library");
  const workspace = document.querySelector(".workspace");
  const infoSection = [...document.querySelectorAll(".details h2, .details .section, .details h3")]
    .map((el) => el.textContent.trim())
    .filter((t) => /information|facts|details/i.test(t));

  const rect = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, height: r.height, clientH: el.clientHeight, scrollH: el.scrollHeight, overflowY: getComputedStyle(el).overflowY, scrollTop: el.scrollTop };
  };

  const results = {
    workspaceRows: workspace ? getComputedStyle(workspace).gridTemplateRows : null,
    details: rect(details),
    detailBody: rect(detailBody),
    library: rect(lib),
    windowH: window.innerHeight,
    detailsScrollable: details ? details.scrollHeight > details.clientHeight : false,
    detailsScrollbarVisible: details ? getComputedStyle(details).overflowY === "auto" || getComputedStyle(details).overflowY === "scroll" : false,
  };

  // Try scrolling the details panel to the bottom and check if last rows become visible
  if (details && details.scrollHeight > details.clientHeight) {
    details.scrollTop = details.scrollHeight;
    results.detailsScrolledToBottom = details.scrollTop > 0;
    results.lastRowVisibleAfterScroll = (() => {
      // find last text row in the info section
      const rows = [...document.querySelectorAll(".detail-body .facts, .detail-body .fact, .detail-body dl, .detail-body .info-row")];
      if (!rows.length) return null;
      const last = rows[rows.length - 1].getBoundingClientRect();
      const dRect = details.getBoundingClientRect();
      return last.bottom <= dRect.bottom + 2;
    })();
  }

  return results;
});

console.log(JSON.stringify(metrics, null, 2));
await browser.close();
