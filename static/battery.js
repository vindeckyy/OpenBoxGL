/* Battery indicator for Big Box. Browser API first, Linux sysfs bridge second. */
import { api } from './state.js';

const CACHE_MS = 60_000;
let cached = { at: 0, value: { present: false, percent: 0, charging: false } };

function normalize(value, percent, charging) {
  if (!value) return { present: false, percent: 0, charging: false };
  const number = Number(percent);
  return {
    present: true,
    percent: Number.isFinite(number) ? Math.min(100, Math.max(0, Math.round(number))) : 0,
    charging: Boolean(charging),
  };
}

export async function readBattery({ force = false, now = Date.now() } = {}) {
  if (!force && cached.at && now - cached.at < CACHE_MS) return cached.value;
  let result = { present: false, percent: 0, charging: false };
  try {
    if (typeof navigator !== 'undefined' && typeof navigator.getBattery === 'function') {
      const status = await navigator.getBattery();
      result = normalize(true, Number(status.level) * 100, status.charging);
    } else {
      const payload = await api('/api/native/battery');
      result = normalize(payload.present, payload.percent, payload.charging);
    }
  } catch {
    result = { present: false, percent: 0, charging: false };
  }
  cached = { at: now, value: result };
  return result;
}

export function batteryLabel(status) {
  if (!status?.present) return '';
  return `${Math.round(status.percent)}%${status.charging ? ' ⚡' : ''}`;
}

export function resetBatteryCache() {
  cached = { at: 0, value: { present: false, percent: 0, charging: false } };
}
