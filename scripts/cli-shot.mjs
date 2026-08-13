import puppeteer from "puppeteer";
import fs from "node:fs";
const browser = await puppeteer.launch({
  headless: true,
  defaultViewport: { width: 1440, height: 900 },
  args: ["--hide-scrollbars", "--force-device-scale-factor=1", "--no-sandbox", "--disable-setuid-sandbox"],
});
const page = await browser.newPage();
await page.goto(process.argv[2], { waitUntil: "networkidle0", timeout: 120000 });
await new Promise((r) => setTimeout(r, 1200));
const full = await page.screenshot({ fullPage: true });
fs.writeFileSync(process.argv[3], full);
console.log("height:", await page.evaluate(() => document.body.scrollHeight));
await browser.close();
