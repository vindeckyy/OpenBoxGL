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

// S19: plural selection. `interpolate` substituted `{count}` verbatim, so a game
// with one Moment read "1 moments" -- on its card and in its aria-label -- in
// every one of the five locales. A game with one entry in the trash read
// "Trash emptied (1 entries)".
//
// A `count` parameter now prefers `<key>_one` when the count is 1 and
// `<key>_many` otherwise, falling back to the base key when neither variant
// exists. That fallback is what makes this safe to add to: every existing key
// keeps its current behaviour, and only the strings that gain a variant change.
// The mechanism already existed in the tree -- `household.stats_skipped_one` /
// `_many` and a hand-rolled branch in `library.js` -- it was just not in the
// one place every caller goes through.
//
// Only the singular is required in practice: a key that has no `_one` variant
// keeps its existing plural text, so this does not force a translation pass
// across every counted string in the app.
function pluralVariant(key, params) {
  if (!params || typeof params.count !== 'number' || !Number.isFinite(params.count)) return null;
  return deepGet(_strings, `${key}_${params.count === 1 ? 'one' : 'many'}`)
    ?? deepGet(_enStrings || _strings, `${key}_${params.count === 1 ? 'one' : 'many'}`);
}

function t(key, params) {
  const variant = pluralVariant(key, params);
  if (variant != null && typeof variant === 'string') return interpolate(variant, params);
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

async function init() {
  // Priority: explicit choice (localStorage) > server setting > browser language > en.
  const fallback = (AppState.appSettings && AppState.appSettings.locale) ||
    (typeof navigator !== 'undefined' && (navigator.language || '').slice(0, 2)) || '';
  const locale = storedLocale() || (SUPPORTED_LOCALES.includes(fallback) ? fallback : 'en');
  await loadLocale(locale);
}

export { t, init, setLocale, getLocale, getSupportedLocales, applyTranslations, loadLocale };
