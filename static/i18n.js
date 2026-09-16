/* i18n — OpenBox internationalization module (1.7.2)
   Loads locale JSON files, applies translations to data-i18n attributes,
   and provides t(key) for JS string lookups. No deps.
   Lazy-loads locale files via fetch. Falls back to en for missing keys.
*/
import { AppState, notify } from './state.js';

const SUPPORTED_LOCALES = ['en', 'es', 'de', 'fr', 'pt'];
let _locale = 'en';
let _strings = {};
let _enStrings = null;
let _fallbackWarned = false;
// P8: JS-rendered surfaces subscribe here instead of listening for the raw
// DOM event, so re-render registrations are explicit, removable, and testable.
const localeChangeListeners = new Set();

function deepGet(obj, path) {
  const parts = path.split('.');
  let cur = obj;
  for (const part of parts) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[part];
  }
  return cur;
}

function interpolate(str, params) {
  if (!params || typeof str !== 'string') return str;
  return str.replace(/\{(\w+)\}/g, (_, key) => String(params[key] ?? ''));
}

async function loadLocaleFile(locale) {
  try {
    const resp = await fetch(`/locales/${locale}.json`, { cache: 'no-cache' });
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  }
}

// P8: a failed locale fetch used to fall back to English silently. Surface it
// exactly once (toast when the shell is ready, console otherwise) so a broken
// deploy is diagnosable without spamming every navigation.
function warnLocaleFallback(locale) {
  if (_fallbackWarned || locale === 'en') return;
  _fallbackWarned = true;
  const message = t('common.locale_fallback', { locale });
  if (message !== 'common.locale_fallback') {
    try {
      notify('warning', message);
      return;
    } catch { /* shell not ready yet */ }
  }
  if (typeof console !== 'undefined' && console.warn) {
    console.warn(`OpenBox: could not load the "${locale}" language pack; falling back to English.`);
  }
}

async function loadLocale(locale) {
  if (!SUPPORTED_LOCALES.includes(locale)) locale = 'en';
  if (!_enStrings) {
    _enStrings = await loadLocaleFile('en');
    if (!_enStrings) _enStrings = {};
  }
  if (locale === 'en') {
    _strings = _enStrings;
  } else {
    const data = await loadLocaleFile(locale);
    if (!data) warnLocaleFallback(locale);
    _strings = data || _enStrings;
  }
  _locale = locale;
  applyTranslations();
  for (const listener of localeChangeListeners) {
    try { listener(locale); } catch (error) { console.error('localechange listener failed', error); }
  }
  document.dispatchEvent(new CustomEvent('localechange', { detail: { locale } }));
}

/**
 * Register a callback that runs after a locale finishes loading, including the
 * initial load. Returns an unsubscribe function. Prefer this over listening for
 * the raw `localechange` DOM event so registrations are discoverable.
 * @param {(locale: string) => void} fn
 * @returns {() => void}
 */
function onLocaleChange(fn) {
  if (typeof fn !== 'function') return () => {};
  localeChangeListeners.add(fn);
  return () => localeChangeListeners.delete(fn);
}

function t(key, params) {
  const val = deepGet(_strings, key);
  if (val != null && typeof val === 'string') return interpolate(val, params);
  const enVal = deepGet(_enStrings || _strings, key);
  if (enVal != null && typeof enVal === 'string') return interpolate(enVal, params);
  return key;
}

// Dependency-sensitive surfaces such as the canvas Arcade Room keep their
// static imports small. Expose the same translator for those surfaces without
// introducing a circular import.
if (typeof window !== 'undefined') window.OpenBoxI18n = { ...(window.OpenBoxI18n || {}), t };

function applyTranslations() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    const val = t(key);
    if (val && val !== key) el.textContent = val;
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    const key = el.getAttribute('data-i18n-placeholder');
    const val = t(key);
    if (val && val !== key) el.setAttribute('placeholder', val);
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    const key = el.getAttribute('data-i18n-title');
    const val = t(key);
    if (val && val !== key) el.setAttribute('title', val);
  });
  document.querySelectorAll('[data-i18n-aria-label]').forEach(el => {
    const key = el.getAttribute('data-i18n-aria-label');
    const val = t(key);
    if (val && val !== key) el.setAttribute('aria-label', val);
  });
  const html = document.documentElement;
  html.setAttribute('lang', _locale);
}

const LOCALE_STORAGE_KEY = 'openbox-locale';

async function setLocale(locale) {
  await loadLocale(locale);
  try { localStorage.setItem(LOCALE_STORAGE_KEY, locale); } catch { /* storage unavailable */ }
}

function getLocale() { return _locale; }

function getSupportedLocales() {
  return SUPPORTED_LOCALES.slice();
}

function storedLocale() {
  try {
    const stored = localStorage.getItem(LOCALE_STORAGE_KEY);
    if (stored && SUPPORTED_LOCALES.includes(stored)) return stored;
  } catch { /* storage unavailable */ }
  return '';
}

function populateLocaleSelector(settings) {
  // Called once before settings load (defaults) and again from refresh() with
  // the real available_locales. Before 1.13.0 the selector was built from the
  // empty AppState at DOMContentLoaded, so non-English locales were not
  // selectable until localStorage already held a choice.
  const sel = typeof document !== 'undefined' ? document.getElementById('localeSetting') : null;
  if (!sel) return;
  const locales = (settings && Array.isArray(settings.available_locales) && settings.available_locales.length)
    ? settings.available_locales
    : [{ code: 'en', name: 'English', native: 'English' }];
  const signature = locales.map(item => `${item.code}:${item.native || item.name || ''}`).join('|');
  if (sel.dataset.localeSignature === signature) {
    sel.value = getLocale();
    return;
  }
  sel.dataset.localeSignature = signature;
  sel.innerHTML = '';
  for (const loc of locales) {
    const opt = document.createElement('option');
    opt.value = loc.code;
    opt.textContent = loc.native || loc.name || loc.code;
    sel.appendChild(opt);
  }
  sel.value = getLocale();
  sel.onchange = () => { setLocale(sel.value).catch(() => {}); };
}

async function syncLocaleFromSettings(settings) {
  // Explicit choice (localStorage) still wins; the server setting is honored
  // when no explicit choice exists and settings arrive after startup.
  const target = storedLocale() || (settings && settings.locale) || '';
  if (target && SUPPORTED_LOCALES.includes(target) && target !== getLocale()) {
    await setLocale(target);
  }
}

async function init() {
  // Priority: explicit choice (localStorage) > server setting > browser language > en.
  const fallback = (AppState.appSettings && AppState.appSettings.locale) ||
    (typeof navigator !== 'undefined' && (navigator.language || '').slice(0, 2)) || '';
  const locale = storedLocale() || (SUPPORTED_LOCALES.includes(fallback) ? fallback : 'en');
  await loadLocale(locale);
}

export { t, init, setLocale, getLocale, getSupportedLocales, applyTranslations, loadLocale, populateLocaleSelector, syncLocaleFromSettings, onLocaleChange };
