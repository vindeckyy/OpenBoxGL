import puppeteer from "puppeteer";
const [appUrl, outputPath] = process.argv.slice(2);
const browser = await puppeteer.launch({ headless: true, defaultViewport: { width: 1280, height: 780 },
  args: ["--hide-scrollbars", "--force-device-scale-factor=1", "--no-sandbox", "--disable-setuid-sandbox"] });
const page = await browser.newPage();
await page.goto(appUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
await page.waitForFunction(() => document.querySelectorAll("#grid [data-game]").length >= 1, { timeout: 60000 });
await page.evaluate(() => document.querySelector("#grid [data-game]")?.click());
await new Promise(r => setTimeout(r, 1500));
// scroll details to bottom so LAUNCHES / LAST PLAYED is visible
await page.evaluate(() => { const d = document.querySelector(".details"); if (d) d.scrollTop = d.scrollHeight; });
await new Promise(r => setTimeout(r, 500));
await page.screenshot({ path: outputPath });
console.log("saved", outputPath);
await browser.close();
