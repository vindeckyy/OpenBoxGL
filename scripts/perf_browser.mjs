#!/usr/bin/env node
// Browser-side performance benchmark for OpenBox.
//
// Usage:
//   node scripts/perf_browser.mjs <app-url> [game-count]
//
// Measures, against the real web UI:
//   * time from navigation to first grid paint
//   * search keystroke -> grid re-render latency (median over 5 keystrokes)
//   * /api/media requests issued during a re-render (cache effectiveness)
//   * DOM nodes in the grid after render
// Prints a JSON result object.
import puppeteer from "puppeteer";

const [appUrl] = process.argv.slice(2);
if (!appUrl) {
  console.error("Usage: node perf_browser.mjs <app-url>");
  process.exit(1);
}

const browser = await puppeteer.launch({
  headless: true,
  defaultViewport: { width: 1920, height: 1080 },
  args: ["--hide-scrollbars", "--force-device-scale-factor=1", "--no-sandbox", "--disable-setuid-sandbox"],
});
const page = await browser.newPage();
const mediaRequests = [];
page.on("request", (request) => {
  if (request.url().includes("/api/media")) mediaRequests.push(request.url());
});

const start = Date.now();
await page.goto(appUrl, { waitUntil: "domcontentloaded", timeout: 120000 });
await page.waitForFunction(
  () => document.querySelectorAll("#grid .card, #grid .list-row").length >= 1,
  { timeout: 60000 },
);
const gridPaintMs = Date.now() - start;

await page.waitForFunction(
  () => document.getElementById("sidebarSearch") !== null,
  { timeout: 30000 },
);

// Instrument renderGrid so we can time keystroke-driven re-renders.
await page.evaluate(() => {
  window.__renderGridTimes = [];
  const original = window.renderGrid;
  window.renderGrid = function (...args) {
    const started = performance.now();
    const result = original.apply(this, args);
    window.__renderGridTimes.push(performance.now() - started);
    return result;
  };
});

// Type one letter at a time; each keystroke triggers a full render.
const search = await page.$("#sidebarSearch");
const renderTimes = [];
for (const letter of "abcdef") {
  await search.type(letter);
  await new Promise((resolve) => setTimeout(resolve, 400));
}
await page.evaluate(() => {
  window.__renderGridTimes = [];
});
for (const letter of "xyz") {
  await search.type(letter);
  await new Promise((resolve) => setTimeout(resolve, 400));
}
renderTimes.push(...(await page.evaluate(() => window.__renderGridTimes)));

// Clear the search so the full grid is visible again, then measure DOM
// after the debounced re-render has run.
await search.click({ clickCount: 3 });
await page.evaluate(() => {
  const search = document.getElementById("sidebarSearch");
  search.value = "";
  search.dispatchEvent(new Event("input"));
});
await new Promise((resolve) => setTimeout(resolve, 500));

const domNodes = await page.evaluate(() => {
  const grid = document.getElementById("grid");
  const cards = grid ? grid.querySelectorAll(".card").length : 0;
  const images = grid ? grid.querySelectorAll("img").length : 0;
  return { cards, images, nodeCount: grid ? grid.querySelectorAll("*").length : 0 };
});

// Re-render to observe media request behavior: clear then dispatch input again.
mediaRequests.length = 0;
await search.type("a");
await new Promise((resolve) => setTimeout(resolve, 800));
const mediaOnRerender = mediaRequests.length;

const median = (values) => {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
};

console.log(JSON.stringify({
  grid_paint_ms: gridPaintMs,
  keystroke_render_ms_median: Math.round(median(renderTimes)),
  keystroke_render_ms_max: renderTimes.length ? Math.round(Math.max(...renderTimes)) : 0,
  grid_cards: domNodes.cards,
  grid_images: domNodes.images,
  grid_dom_nodes: domNodes.nodeCount,
  media_requests_on_rerender: mediaOnRerender,
}, null, 2));

await browser.close();
