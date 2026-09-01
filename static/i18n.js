/* i18n — OpenBox internationalization module (1.7.2)
   Loads locale JSON files, applies translations to data-i18n attributes,
   and provides t(key) for JS string lookups. No deps.
   Lazy-loads locale files via fetch. Falls back to en for missing keys.
*/
import { AppState } from './state.js';

const SUPPORTED_LOCALES = ['en', 'es', 'de', 'fr', 'pt'];
let _locale = 'en';
let _strings = {};
let _enStrings = null;

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
    _strings = data || _enStrings;
  }
  _locale = locale;
  applyTranslations();
  document.dispatchEvent(new CustomEvent('localechange', { detail: { locale } }));
}

function t(key, params) {
  const val = deepGet(_strings, key);
  if (val != null && typeof val === 'string') return interpolate(val, params);
  const enVal = deepGet(_enStrings || _strings, key);
  if (enVal != null && typeof enVal === 'string') return interpolate(enVal, params);
  return key;
}

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

async function setLocale(locale) {
  await loadLocale(locale);
  try { AppState.set('locale', locale); } catch { /* settings may not be ready */ }
}

function getLocale() { return _locale; }

function getSupportedLocales() {
  return SUPPORTED_LOCALES.slice();
}

async function init() {
  let locale = 'en';
  try { locale = AppState.get('locale') || 'en'; } catch { /* ignore */ }
  await loadLocale(locale);
}

export { t, init, setLocale, getLocale, getSupportedLocales, applyTranslations, loadLocale };
