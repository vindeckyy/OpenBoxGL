/* mood.js — cover-driven adaptive theming for OpenBox 1.9.0
   Extracts a 5-color palette from the selected/focused game's cover art and
   applies it as CSS custom properties on :root. No dependencies.
*/
import { AppState, media } from './state.js';

const MOOD_PROPS = ['--mood-primary', '--mood-ink', '--mood-secondary', '--mood-glow', '--mood-tint'];
const MOOD_CACHE = new Map();
const MAX_CACHE = 128;
let moodSeq = 0;
let canvas = null;

function $(sel) { return document.getElementById(sel); }

function rgbToHex(r, g, b) {
  return '#' + [r, g, b].map(v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('');
}

function rgbToRgba(r, g, b, a) {
  return `rgba(${Math.round(r)},${Math.round(g)},${Math.round(b)},${a})`;
}

function parseCssColor(value) {
  const s = (value || '').trim();
  if (s.startsWith('#')) {
    const hex = s.slice(1);
    if (hex.length === 3) {
      const [r, g, b] = [...hex].map(c => parseInt(c + c, 16));
      return [r, g, b];
    }
    if (hex.length === 6) {
      return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)];
    }
  }
  const m = s.match(/rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3])];
  return null;
}

function relativeLuminance(r, g, b) {
  const [rs, gs, bs] = [r, g, b].map(v => {
    v /= 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * rs + 0.7152 * gs + 0.0722 * bs;
}

function contrastRatio(a, b) {
  const l1 = relativeLuminance(...a);
  const l2 = relativeLuminance(...b);
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
}

function pickInk(r, g, b) {
  const root = document.documentElement;
  const style = getComputedStyle(root);
  const white = parseCssColor(style.getPropertyValue('--white')) || [246, 239, 228];
  const black = parseCssColor(style.getPropertyValue('--black')) || [11, 11, 11];
  const whiteContrast = contrastRatio([r, g, b], white);
  const blackContrast = contrastRatio([r, g, b], black);
  return (whiteContrast >= blackContrast) ? rgbToHex(...white) : rgbToHex(...black);
}

function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return { h: 0, s: 0, l };
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h = 0;
  if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
  else if (max === g) h = ((b - r) / d + 2) / 6;
  else h = ((r - g) / d + 4) / 6;
  return { h: h * 360, s, l };
}

function hslToRgb(h, s, l) {
  h /= 360;
  const hue2rgb = (p, q, t) => {
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  };
  if (s === 0) return [l * 255, l * 255, l * 255];
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  return [hue2rgb(p, q, h + 1 / 3) * 255, hue2rgb(p, q, h) * 255, hue2rgb(p, q, h - 1 / 3) * 255];
}

function hueDistance(a, b) {
  const d = Math.abs(a - b) % 360;
  return d > 180 ? 360 - d : d;
}

function extractPalette(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.src = src;
    const timeout = setTimeout(() => { img.src = ''; reject(new Error('mood image timeout')); }, 3000);
    img.onerror = () => { clearTimeout(timeout); reject(new Error('mood image error')); };
    img.onload = () => { clearTimeout(timeout); resolve(processImage(img)); };
    if (img.decode) img.decode().then(() => { clearTimeout(timeout); resolve(processImage(img)); }).catch(() => {});
  });
}

function processImage(img) {
  if (!canvas) canvas = document.createElement('canvas');
  const size = 48;
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.clearRect(0, 0, size, size);
  ctx.drawImage(img, 0, 0, size, size);
  const data = ctx.getImageData(0, 0, size, size).data;
  // ponytail: 4x4x4 RGB binning is a fast, good-enough palette summarizer.
  // If users report muddy or off-primary palettes, upgrade to median-cut/k-means.
  const bins = Array.from({ length: 64 }, () => ({ r: 0, g: 0, b: 0, count: 0 }));
  for (let i = 0; i < data.length; i += 4) {
    const r = data[i], g = data[i + 1], b = data[i + 2], a = data[i + 3];
    if (a < 128) continue;
    const sum = r + g + b;
    if (sum < 30 || sum > 740) continue;
    const bin = (r >> 6) * 16 + (g >> 6) * 4 + (b >> 6);
    bins[bin].r += r;
    bins[bin].g += g;
    bins[bin].b += b;
    bins[bin].count++;
  }
  const scored = bins.filter(b => b.count > 0).map(b => {
    const r = b.r / b.count;
    const g = b.g / b.count;
    const b2 = b.b / b.count;
    const max = Math.max(r, g, b2), min = Math.min(r, g, b2);
    const saturation = (max - min) / 255;
    const lum = relativeLuminance(r, g, b2);
    const lumWeight = (lum < 0.15 || lum > 0.85) ? 0.3 : 1.0;
    return { r, g, b: b2, count: b.count, score: b.count * saturation * lumWeight, saturation, lum };
  }).sort((a, b) => b.score - a.score);
  if (!scored.length) throw new Error('no usable palette');

  const primary = scored[0];
  const pHsl = rgbToHsl(primary.r, primary.g, primary.b);
  let secondary = null;
  for (const c of scored.slice(1)) {
    const cHsl = rgbToHsl(c.r, c.g, c.b);
    if (hueDistance(pHsl.h, cHsl.h) > 30) { secondary = c; break; }
  }
  if (!secondary) {
    // No distinct hue found — lighten the primary.
    const l = Math.min(0.95, pHsl.l + 0.18);
    const [r, g, b] = hslToRgb(pHsl.h, pHsl.s, l);
    secondary = { r, g, b };
  }

  return {
    primary: rgbToHex(primary.r, primary.g, primary.b),
    ink: pickInk(primary.r, primary.g, primary.b),
    secondary: rgbToHex(secondary.r, secondary.g, secondary.b),
    glow: rgbToRgba(primary.r, primary.g, primary.b, 0.35),
    tint: rgbToRgba(primary.r, primary.g, primary.b, 0.10),
  };
}

export async function applyMoodForGame(game) {
  moodSeq++;
  const mySeq = moodSeq;
  const enabled = AppState.appSettings?.mood_match_enabled;
  const bigboxOpen = $('bigBox') && !$('bigBox').hidden;
  if (!enabled || (bigboxOpen && !AppState.appSettings?.mood_match_bigbox)) {
    clearMood();
    return;
  }
  if (!game || !game.has_cover) {
    clearMood();
    return;
  }
  const cacheKey = `${game.id}:${AppState.mediaEpoch}:${game.cover || ''}`;
  let palette = MOOD_CACHE.get(cacheKey);
  if (!palette) {
    try {
      palette = await extractPalette(media(game, 'cover'));
      if (MOOD_CACHE.size >= MAX_CACHE) {
        const firstKey = MOOD_CACHE.keys().next().value;
        MOOD_CACHE.delete(firstKey);
      }
      MOOD_CACHE.set(cacheKey, palette);
    } catch {
      if (mySeq === moodSeq) clearMood();
      return;
    }
  }
  if (mySeq !== moodSeq) return;
  const root = document.documentElement;
  root.style.setProperty('--mood-primary', palette.primary);
  root.style.setProperty('--mood-ink', palette.ink);
  root.style.setProperty('--mood-secondary', palette.secondary);
  root.style.setProperty('--mood-glow', palette.glow);
  root.style.setProperty('--mood-tint', palette.tint);
  root.classList.add('mood-active');
}

export function clearMood() {
  moodSeq++;
  const root = document.documentElement;
  for (const p of MOOD_PROPS) root.style.removeProperty(p);
  root.classList.remove('mood-active');
}

export function initMood() {
  const game = AppState.games?.find(g => g.id === AppState.selectedId);
  if (game) applyMoodForGame(game).catch(() => {});
}
