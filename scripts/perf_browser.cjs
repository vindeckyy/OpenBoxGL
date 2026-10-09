#!/usr/bin/env node
// Browser timings for perf_bench.py --browser: first render of a large library,
// long tasks while it renders, and layout shift caused by a search that empties
// the grid. Prints one JSON object on stdout.
//
// Env: ORIGIN, TOKEN (from the running server), RUNS (default 3),
//      PUPPETEER_EXECUTABLE_PATH (optional; otherwise puppeteer's own Chrome).
// Exits 0 with {"available": false, "reason": ...} when no browser can start, so a
// machine without Chrome reports "not measured" instead of failing the bench.
'use strict';

const puppeteer = require('puppeteer');

const origin = process.env.ORIGIN;
const token = process.env.TOKEN;
const runs = Math.max(1, Number(process.env.RUNS || 3));

const INSTRUMENT = () => {
  window.__perf = {longTasks: 0, longTaskMs: 0, cls: 0, sources: []};
  new PerformanceObserver(list => {
    for (const entry of list.getEntries()) {
      window.__perf.longTasks += 1;
      window.__perf.longTaskMs += entry.duration;
    }
  }).observe({type: 'longtask', buffered: true});
  new PerformanceObserver(list => {
    for (const entry of list.getEntries()) {
      if (!entry.hadRecentInput) {
        window.__perf.cls += entry.value;
        for (const source of entry.sources || []) {
          const node = source.node;
          window.__perf.sources.push(node ? (node.id ? '#' + node.id : (node.className ? '.' + String(node.className).trim().split(/\s+/).join('.') : node.nodeName)) : 'unknown');
        }
      }
    }
  }).observe({type: 'layout-shift', buffered: true});
};

async function measureOnce(browser) {
  const page = await browser.newPage();
  await page.evaluateOnNewDocument(INSTRUMENT);
  const started = Date.now();
  await page.goto(`${origin}/?token=${encodeURIComponent(token)}`, {waitUntil: 'domcontentloaded', timeout: 120000});
  await page.waitForSelector('.cover', {timeout: 120000});
  const firstRenderMs = Date.now() - started;
  await new Promise(resolve => setTimeout(resolve, 800));
  const afterLoad = await page.evaluate(() => ({...window.__perf}));

  // A search that matches nothing empties the grid: the shift it causes is what
  // the layout-stability gate measures.
  const before = await page.evaluate(() => window.__perf.cls);
  const sourcesBefore = await page.evaluate(() => window.__perf.sources.length);
  await page.evaluate(() => {
    const search = document.getElementById('sidebarSearch');
    search.value = 'zzzz-no-match-perf';
    search.dispatchEvent(new Event('input', {bubbles: true}));
  });
  await new Promise(resolve => setTimeout(resolve, 800));
  const searchCls = (await page.evaluate(() => window.__perf.cls)) - before;
  const searchSources = await page.evaluate(n => [...new Set(window.__perf.sources.slice(n))].slice(0, 6), sourcesBefore);
  await page.close();
  return {firstRenderMs, longTasks: afterLoad.longTasks, longTaskMs: Math.round(afterLoad.longTaskMs), searchCls, searchSources};
}

(async () => {
  let browser;
  try {
    const launch = {headless: true, args: ['--no-sandbox', '--disable-gpu']};
    if (process.env.PUPPETEER_EXECUTABLE_PATH) launch.executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
    browser = await puppeteer.launch(launch);
  } catch (error) {
    console.log(JSON.stringify({available: false, reason: String(error.message || error).split('\n')[0]}));
    return;
  }
  try {
    const samples = [];
    for (let i = 0; i < runs; i += 1) samples.push(await measureOnce(browser));
    console.log(JSON.stringify({available: true, runs, samples}));
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.log(JSON.stringify({available: false, reason: String(error.message || error).split('\n')[0]}));
  process.exitCode = 0;
});
