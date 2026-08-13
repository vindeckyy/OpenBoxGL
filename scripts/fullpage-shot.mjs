import puppeteer from "puppeteer";

const url = process.argv[2];
const out = process.argv[3];
const browser = await puppeteer.launch({
  headless: true,
  defaultViewport: { width: 1440, height: 900 },
  args: ["--hide-scrollbars", "--force-device-scale-factor=1", "--no-sandbox", "--disable-setuid-sandbox"],
});
const page = await browser.newPage();
await page.goto(url, { waitUntil: "networkidle0", timeout: 120000 });
await new Promise((r) => setTimeout(r, 1500));
// scroll through to force lazy content
await page.evaluate(async () => {
  const h = document.body.scrollHeight;
  for (let y = 0; y < h; y += 600) { window.scrollTo(0, y); await new Promise((r) => setTimeout(r, 30)); }
  window.scrollTo(0, 0);
  await new Promise((r) => setTimeout(r, 800));
});
const full = await page.screenshot({ fullPage: true });
require("fs").writeFileSync(out, full);
console.log("height:", (await page.evaluate(() => document.body.scrollHeight)), "saved", out);
await browser.close();
