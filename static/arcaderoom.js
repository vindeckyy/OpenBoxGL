/* arcaderoom.js — the T5 canvas Arcade Room and Museum surface.
 *
 * The room is deliberately self-contained.  It creates its own accessible
 * overlay when opened, so the first T5 slice does not need a parallel HTML or
 * CSS contract.  Library state and media URLs still come from the existing
 * app utilities.  Later surfaces can use the exported controller or the
 * document events without knowing anything about the canvas implementation.
 */
import { $, escapeHtml, duration, defaultControllerMap } from './util.js';
import { AppState, media } from './state.js';

const MAX_TEXTURE_CACHE = 48;
const MAX_DPR = 3;
const CABINET_WINDOW_RADIUS = 4;
const ZONE_WINDOW_RADIUS = 1;
const SCREEN_CYCLE_MS = 4200;
const MUSEUM_STEP_MS = 5200;
const DEFAULT_IDLE_MS = 90 * 1000;
const MIN_IDLE_MS = 1000;
const MAX_IDLE_MS = 24 * 60 * 60 * 1000;
const SWIPE_DISTANCE = 42;
const SHOW_GAME_EVENT = 'app:show-game';
const LAUNCH_EVENT = 'app:launch-game';

const hasDocument = () => typeof document !== 'undefined';
const hasWindow = () => typeof window !== 'undefined';
const clock = () => typeof performance !== 'undefined' && typeof performance.now === 'function'
  ? performance.now()
  : Date.now();

function arcadeText(key, fallback, params) {
  const translate = hasWindow() ? window.OpenBoxI18n?.t : null;
  if (typeof translate !== 'function') return fallback;
  try {
    const value = translate(key, params);
    return value && value !== key ? value : fallback;
  } catch {
    return fallback;
  }
}

function requestFrame(callback) {
  if (hasWindow() && typeof window.requestAnimationFrame === 'function') return window.requestAnimationFrame(callback);
  return typeof setTimeout === 'function' ? setTimeout(() => callback(clock()), 16) : 0;
}

function cancelFrame(frame) {
  if (!frame) return;
  if (hasWindow() && typeof window.cancelAnimationFrame === 'function') window.cancelAnimationFrame(frame);
  else if (typeof clearTimeout === 'function') clearTimeout(frame);
}

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function numberOr(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function gameIdentifiers(game) {
  if (!game || typeof game !== 'object') return [];
  return [game.game_id, game.id]
    .filter(value => value !== undefined && value !== null && value !== '')
    .map(value => String(value));
}

function sameGame(a, b) {
  const right = new Set(gameIdentifiers(b));
  return gameIdentifiers(a).some(value => right.has(value));
}

function gameTitle(game) {
  return String(game?.name || game?.title || arcadeText('arcade.untitled_game', 'Untitled game'));
}

function platformName(game) {
  return String(game?.platform || arcadeText('arcade.uncategorized', 'Uncategorized')).trim() ||
    arcadeText('arcade.uncategorized', 'Uncategorized');
}

function titleSort(a, b) {
  return String(a?.sort_title || a?.name || '').localeCompare(String(b?.sort_title || b?.name || ''));
}

function preferredState(options = {}) {
  if (options.state && typeof options.state === 'object') return options.state;
  if (hasWindow() && window.AppState && typeof window.AppState === 'object') return window.AppState;
  return AppState;
}

function prefersReducedMotion() {
  try {
    return Boolean(hasWindow() && typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch {
    return false;
  }
}

function cssToken(name, fallback) {
  try {
    if (hasWindow() && typeof window.getComputedStyle === 'function' && hasDocument()) {
      const value = window.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      if (value) return value;
    }
  } catch { /* a detached/test document has no computed style */ }
  return fallback;
}

function themeTokens() {
  // Canvas needs computed values rather than var(...) strings.  All normal
  // values are existing app.css tokens; named fallbacks only cover a canvas
  // rendered before the stylesheet has been applied.
  return {
    bg: cssToken('--surface-deep', 'black'),
    panel: cssToken('--surface-card', 'black'),
    field: cssToken('--surface-field', 'black'),
    hover: cssToken('--surface-hover', 'black'),
    line: cssToken('--line', 'currentColor'),
    border: cssToken('--border-card', 'currentColor'),
    brand: cssToken('--brand', 'currentColor'),
    focus: cssToken('--focus', 'currentColor'),
    action: cssToken('--action', 'currentColor'),
    actionInk: cssToken('--action-ink', 'white'),
    text: cssToken('--text', 'white'),
    muted: cssToken('--muted', 'currentColor'),
    section: cssToken('--section-muted', 'currentColor'),
    gold: cssToken('--gold', 'currentColor'),
    accent: cssToken('--accent', 'currentColor'),
    fontMicro: cssToken('--font-micro', '10px'),
    fontLabel: cssToken('--font-label', '12px'),
    fontBody: cssToken('--font-body', '14px'),
    fontHeading: cssToken('--font-heading', '24px'),
    fontDisplay: cssToken('--font-bigbox-display', '48px'),
  };
}

/**
 * Small insertion-ordered LRU for image and video textures.
 *
 * The cache is intentionally bounded by entries, not by the number of games
 * in the library.  Failed loads resolve to null and remain as a bounded
 * negative entry, so a broken artwork URL cannot be retried every frame.
 */
class TextureCache {
  constructor(maxEntries = MAX_TEXTURE_CACHE) {
    this.maxEntries = clamp(Math.floor(numberOr(maxEntries, MAX_TEXTURE_CACHE)), 1, 512);
    this.entries = new Map();
  }

  get size() {
    return this.entries.size;
  }

  _key(url, kind = 'image') {
    return `${kind}:${String(url || '')}`;
  }

  _touch(key, entry) {
    this.entries.delete(key);
    this.entries.set(key, entry);
    return entry;
  }

  get(url, kind = 'image') {
    const key = this._key(url, kind);
    const entry = this.entries.get(key);
    if (!entry) return null;
    return this._touch(key, entry).texture || null;
  }

  has(url, kind = 'image') {
    return this.entries.has(this._key(url, kind));
  }

  set(url, texture, kind = 'image') {
    const value = String(url || '');
    if (!value || !texture) return texture || null;
    this._touch(this._key(value, kind), { texture, kind, ready: true, promise: Promise.resolve(texture) });
    this._trim();
    return texture;
  }

  _disposeEntry(entry) {
    const { texture, kind } = entry || {};
    if (kind !== 'video' || !texture) return;
    try { texture.pause?.(); texture.removeAttribute?.('src'); texture.load?.(); } catch { /* stale media */ }
  }

  _trim() {
    while (this.entries.size > this.maxEntries) {
      const oldest = this.entries.keys().next().value;
      if (oldest === undefined) break;
      const entry = this.entries.get(oldest);
      this.entries.delete(oldest);
      this._disposeEntry(entry);
    }
  }

  load(url, kind = 'image') {
    const value = String(url || '');
    if (!value) return Promise.resolve(null);
    const key = this._key(value, kind);
    const existing = this.entries.get(key);
    if (existing) {
      this._touch(key, existing);
      return existing.promise || Promise.resolve(existing.texture || null);
    }
    if (kind === 'video') return this.loadVideo(value);
    if (typeof Image !== 'function') {
      const promise = Promise.resolve(null);
      this.entries.set(key, { texture: null, kind, ready: true, promise });
      this._trim();
      return promise;
    }

    const image = new Image();
    image.decoding = 'async';
    image.loading = 'eager';
    const entry = { texture: image, kind, ready: false, promise: null };
    this.entries.set(key, entry);
    this._trim();
    entry.promise = new Promise(resolve => {
      const finish = texture => {
        entry.ready = Boolean(texture);
        resolve(texture || null);
      };
      image.onload = () => finish(image);
      image.onerror = () => finish(null);
      try { image.src = value; } catch { finish(null); }
    });
    return entry.promise;
  }

  loadVideo(url) {
    const value = String(url || '');
    const key = this._key(value, 'video');
    const existing = this.entries.get(key);
    if (existing) {
      this._touch(key, existing);
      return existing.promise || Promise.resolve(existing.texture || null);
    }
    if (!hasDocument() || typeof document.createElement !== 'function') {
      const promise = Promise.resolve(null);
      this.entries.set(key, { texture: null, kind: 'video', ready: true, promise });
      this._trim();
      return promise;
    }
    const video = document.createElement('video');
    video.muted = true;
    video.loop = true;
    video.playsInline = true;
    video.preload = 'auto';
    const entry = { texture: video, kind: 'video', ready: false, promise: null };
    this.entries.set(key, entry);
    this._trim();
    entry.promise = new Promise(resolve => {
      let settled = false;
      const finish = texture => {
        if (settled) return;
        settled = true;
        entry.ready = Boolean(texture);
        resolve(texture || null);
      };
      if (typeof video.addEventListener === 'function') {
        video.addEventListener('loadeddata', () => finish(video), { once: true });
        video.addEventListener('error', () => finish(null), { once: true });
      } else {
        video.onloadeddata = () => finish(video);
        video.onerror = () => finish(null);
      }
      try {
        video.src = value;
        video.load?.();
      } catch { finish(null); }
    });
    return entry.promise;
  }

  clear() {
    for (const entry of this.entries.values()) this._disposeEntry(entry);
    this.entries.clear();
  }
}

function groupArcadeZones(games = [], platforms = []) {
  const zones = new Map();
  const addZone = name => {
    const label = String(name || arcadeText('arcade.uncategorized', 'Uncategorized')).trim() ||
      arcadeText('arcade.uncategorized', 'Uncategorized');
    if (!zones.has(label)) zones.set(label, { id: label, name: label, games: [] });
    return zones.get(label);
  };
  for (const platform of Array.isArray(platforms) ? platforms : []) {
    addZone(typeof platform === 'object' ? platform.name || platform.platform : platform);
  }
  for (const game of Array.isArray(games) ? games : []) {
    if (!game || typeof game !== 'object') continue;
    addZone(platformName(game)).games.push(game);
  }
  const result = [...zones.values()];
  result.sort((a, b) => a.name.localeCompare(b.name));
  for (const zone of result) zone.games.sort(titleSort);
  return result.length ? result : [{ id: 'Arcade Room', name: arcadeText('arcade.room_title', 'Arcade Room'), games: [] }];
}

function visibleWindow(length, active, radius = CABINET_WINDOW_RADIUS) {
  const count = Math.max(0, Math.floor(numberOr(length, 0)));
  if (!count) return [];
  const center = clamp(Math.floor(numberOr(active, 0)), 0, count - 1);
  const span = Math.max(0, Math.floor(numberOr(radius, CABINET_WINDOW_RADIUS)));
  const from = Math.max(0, center - span);
  const to = Math.min(count - 1, center + span);
  const indexes = [];
  for (let index = from; index <= to; index += 1) indexes.push(index);
  return indexes;
}

function firstNumber(object, names) {
  for (const name of names) {
    if (object?.[name] === '' || object?.[name] === null || object?.[name] === undefined) continue;
    const value = Number(object[name]);
    if (Number.isFinite(value)) return value;
  }
  return null;
}

function buildMuseumFacts(game) {
  if (!game || typeof game !== 'object') return [];
  const facts = [];
  const year = String(game.year ?? '').trim();
  const developer = String(game.developer ?? '').trim();
  const genre = String(game.genre ?? '').trim();
  if (year) facts.push({ label: arcadeText('arcade.year', 'Year'), value: year });
  if (developer) facts.push({ label: arcadeText('arcade.developer', 'Developer'), value: developer });
  if (genre) facts.push({ label: arcadeText('arcade.genre', 'Genre'), value: genre });

  const playtime = firstNumber(game, ['playtime_seconds']);
  if (playtime !== null && playtime > 0) facts.push({ label: arcadeText('arcade.play_time', 'Play time'), value: duration(playtime) });

  const earned = firstNumber(game, ['ra_achievements_earned', 'achievements_earned']);
  const total = firstNumber(game, ['ra_achievements_total', 'achievements_total']);
  if (earned !== null && total !== null && total >= 0 && earned >= 0) {
    facts.push({ label: arcadeText('arcade.achievements_left', 'Achievements left'), value: String(Math.max(0, Math.floor(total - earned))) });
  }
  return facts;
}

function formatArcadeFacts(facts) {
  return facts.map(fact => `${fact.label}: ${fact.value}`).join(' · ');
}

function roundedPath(context, x, y, width, height, radius) {
  const r = Math.max(0, Math.min(radius, width / 2, height / 2));
  context.beginPath();
  if (typeof context.roundRect === 'function') {
    context.roundRect(x, y, width, height, r);
    return;
  }
  context.moveTo(x + r, y);
  context.lineTo(x + width - r, y);
  context.quadraticCurveTo(x + width, y, x + width, y + r);
  context.lineTo(x + width, y + height - r);
  context.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  context.lineTo(x + r, y + height);
  context.quadraticCurveTo(x, y + height, x, y + height - r);
  context.lineTo(x, y + r);
  context.quadraticCurveTo(x, y, x + r, y);
  context.closePath();
}

function fillRoundRect(context, x, y, width, height, radius, fill) {
  roundedPath(context, x, y, width, height, radius);
  context.fillStyle = fill;
  context.fill();
}

function strokeRoundRect(context, x, y, width, height, radius, stroke, lineWidth = 1) {
  roundedPath(context, x, y, width, height, radius);
  context.strokeStyle = stroke;
  context.lineWidth = lineWidth;
  context.stroke();
}

function initials(name) {
  const words = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  return words.slice(0, 2).map(word => word[0]).join('').toUpperCase();
}

function mediaSourceList(game, room) {
  if (!game) return [];
  const sources = [];
  const videoAvailable = Boolean(game.has_video || game.video_snap || game.video);
  if (videoAvailable) {
    const url = room.mediaUrl(game, 'video');
    if (url) sources.push({ kind: 'video', url });
  }
  const screenshots = Array.isArray(game.available_screenshots)
    ? game.available_screenshots
    : Array.isArray(game.screenshots) ? game.screenshots : [];
  for (let index = 0; index < screenshots.length; index += 1) {
    const value = screenshots[index];
    const url = typeof value === 'string' && value.includes('/')
      ? value
      : room.mediaUrl(game, 'screenshot', typeof value === 'number' ? value : index);
    if (url) sources.push({ kind: 'image', url });
  }
  return sources;
}

function mediaAvailable(game, kind) {
  if (!game) return false;
  const flags = { clear_logo: 'has_clear_logo', cover: 'has_cover', video: 'has_video' };
  const value = game[kind];
  return Boolean(game[flags[kind]] || (typeof value === 'string' && value.trim()) ||
    (kind === 'screenshot' && (game.available_screenshots?.length || game.screenshots?.length)));
}

function dispatchDocumentEvent(type, detail, cancelable = false) {
  if (!hasDocument() || typeof document.dispatchEvent !== 'function') return { defaultPrevented: false, detail };
  let event;
  if (typeof CustomEvent === 'function') {
    event = new CustomEvent(type, { detail, cancelable, bubbles: false });
  } else {
    event = document.createEvent('CustomEvent');
    event.initCustomEvent(type, false, cancelable, detail);
  }
  document.dispatchEvent(event);
  return event;
}

function readEventGame(event) {
  const detail = event?.detail || {};
  return detail.game || detail.item || detail.session || detail;
}

function gameFromState(value, state = AppState) {
  const ids = typeof value === 'object' ? gameIdentifiers(value) : [String(value ?? '')];
  if (!ids[0]) return typeof value === 'object' ? value : null;
  return (Array.isArray(state?.games) ? state.games : []).find(game => gameIdentifiers(game).some(candidate => ids.includes(candidate))) ||
    (typeof value === 'object' ? value : null);
}

function safeFocus(element) {
  try { element?.focus?.({ preventScroll: true }); } catch { /* focus is advisory */ }
}

class ArcadeRoom {
  constructor(options = {}) {
    this.options = { ...options };
    this.state = preferredState(options);
    this.textureCache = options.textureCache instanceof TextureCache
      ? options.textureCache
      : new TextureCache(options.textureCacheSize ?? MAX_TEXTURE_CACHE);
    this.zones = [];
    this.zoneIndex = 0;
    this.cabinetIndex = 0;
    this.canvas = options.canvas || null;
    this.container = options.container || null;
    this.root = null;
    this.hud = null;
    this.titleElement = null;
    this.statusElement = null;
    this.factsElement = null;
    this.detailsButton = null;
    this.context = null;
    this.width = 0;
    this.height = 0;
    this.dpr = 1;
    this.opened = false;
    this.museum = false;
    this.museumUnlocked = false;
    this.museumKioskPending = false;
    this.reducedMotion = prefersReducedMotion();
    this.frame = 0;
    this.idleTimer = 0;
    this.museumTimer = 0;
    this.renderStartedAt = clock();
    this.previousFocus = null;
    this.previousHidden = null;
    this.touchStart = null;
    this.lastTouchAt = 0;
    this.bound = false;
    this.resizeObserver = null;
    this.mediaQuery = null;
    this.gamepadState = {};
    this._onKeyDown = event => this.handleKeyDown(event);
    this._onResize = () => this.resizeCanvas();
    this._onStateRefresh = () => {
      if (!this.opened) return;
      this.syncGames();
      this.render();
    };
    this._onLocaleChange = () => { if (this.opened) this.render(); };
    this._onShowGame = event => this.handleExternalGameEvent(event);
    this._onLaunch = event => this.handleExternalGameEvent(event);
    this._onPointerDown = event => this.handlePointerDown(event);
    this._onPointerUp = event => this.handlePointerUp(event);
    this._onTouchStart = event => this.handleTouchStart(event);
    this._onTouchEnd = event => this.handleTouchEnd(event);
    this._onWheel = event => this.handleWheel(event);
    this._onGamepadConnected = () => { if (this.opened) this.startGamepadPoll(); };
    this._onMotionChange = event => {
      this.reducedMotion = Boolean(event.matches);
      if (this.reducedMotion) this.stopAnimation();
      else if (this.opened) this.startAnimation();
      this.render();
    };
  }

  get games() {
    const source = this.options.games;
    const value = typeof source === 'function' ? source() : source;
    if (Array.isArray(value)) return value;
    return Array.isArray(this.state?.games) ? this.state.games : [];
  }

  get currentZone() {
    return this.zones[this.zoneIndex] || null;
  }

  get currentGame() {
    const zone = this.currentZone;
    return zone?.games?.[this.cabinetIndex] || null;
  }

  get idleMilliseconds() {
    const settingSeconds = this.state?.appSettings?.screensaver_seconds;
    const configured = this.options.idleMs ??
      (settingSeconds === undefined ? DEFAULT_IDLE_MS : Number(settingSeconds) * 1000);
    const delay = numberOr(configured, DEFAULT_IDLE_MS);
    return delay === 0 ? 0 : clamp(delay, MIN_IDLE_MS, MAX_IDLE_MS);
  }

  mediaUrl(game, kind, index = '') {
    const custom = this.options.mediaUrl || this.options.media;
    if (typeof custom === 'function') {
      try {
        const value = custom(game, kind, index);
        if (value) return String(value);
      } catch { /* fall through to the app media helper */ }
    }
    try {
      if (typeof media === 'function' && game?.id !== undefined && game?.id !== null) {
        const value = media(game, kind, index);
        if (value) return String(value);
      }
    } catch { /* state may not be ready during an early standalone mount */ }
    const explicit = game?.media?.[kind] ?? game?.[kind];
    if (typeof explicit === 'string' && explicit.trim()) return explicit;
    if (typeof explicit === 'object' && explicit?.url) return String(explicit.url);
    if (game?.id === undefined || game?.id === null) return '';
    const params = new URLSearchParams({ id: String(game.id), kind: String(kind) });
    if (index !== '') params.set('index', String(index));
    const epoch = this.state?.mediaEpoch;
    if (epoch !== undefined) params.set('v', String(epoch));
    return `/api/media?${params.toString()}`;
  }

  syncGames() {
    const previous = this.currentGame ||
      (this.state?.selectedId !== null && this.state?.selectedId !== undefined
        ? (this.state.games || []).find(game => gameIdentifiers(game).includes(String(this.state.selectedId)))
        : null);
    const platformList = this.options.platforms || this.state?.appSettings?.platforms || [];
    this.zones = groupArcadeZones(this.games, platformList);
    let zoneIndex = previous ? this.zones.findIndex(zone => zone.games.some(game => sameGame(game, previous))) : -1;
    if (zoneIndex < 0) zoneIndex = clamp(this.zoneIndex, 0, Math.max(0, this.zones.length - 1));
    this.zoneIndex = zoneIndex;
    const zone = this.currentZone;
    const gameIndex = previous && zone ? zone.games.findIndex(game => sameGame(game, previous)) : -1;
    this.cabinetIndex = gameIndex >= 0 ? gameIndex : clamp(this.cabinetIndex, 0, Math.max(0, (zone?.games?.length || 1) - 1));
    return this.zones;
  }

  createOverlay() {
    if (!hasDocument()) return null;
    const suppliedCanvas = this.canvas || (this.container?.tagName === 'CANVAS' ? this.container : null);
    if (suppliedCanvas) {
      this.canvas = suppliedCanvas;
      this.root = this.container && this.container.tagName !== 'CANVAS' ? this.container : suppliedCanvas.parentElement;
      if (!this.root) this.root = suppliedCanvas;
      return this.root;
    }
    // A later HTML shell may provide a host, but the bounded core remains
    // useful today by creating the host on demand.
    const existingRoot = this.container || $('arcadeRoom');
    this.root = existingRoot || document.createElement('section');
    const ownsRoot = !existingRoot;
    this.ownsRoot = ownsRoot;
    if (ownsRoot) {
      this.root.className = 'arcade-room';
      this.root.setAttribute('role', 'dialog');
      this.root.setAttribute('aria-modal', 'true');
      this.root.setAttribute('aria-label', arcadeText('arcade.room_title', 'Arcade Room'));
      this.root.tabIndex = 0;
      Object.assign(this.root.style, {
        position: 'fixed', inset: '0', zIndex: '60', overflow: 'hidden',
        background: 'var(--surface-deep)', color: 'var(--text)',
        font: 'var(--font-body)/1.45 ui-sans-serif,system-ui,sans-serif',
      });
      document.body?.appendChild(this.root);
    }
    this.canvas = document.createElement('canvas');
    this.canvas.className = 'arcade-room-canvas';
    this.canvas.setAttribute('role', 'img');
    this.canvas.setAttribute('aria-label', arcadeText('arcade.canvas_label', 'Arcade Room, use arrow keys to walk between cabinets'));
    Object.assign(this.canvas.style, {
      display: 'block', width: '100%', height: '100%', touchAction: 'none',
    });
    this.root.appendChild(this.canvas);
    this.createHud();
    return this.root;
  }

  createHud() {
    if (!this.root || !hasDocument() || this.hud) return;
    this.hud = document.createElement('div');
    this.hud.className = 'arcade-room-hud';
    Object.assign(this.hud.style, {
      position: 'absolute', top: '0', left: '0', right: '0',
      display: 'flex', flexDirection: 'column', gap: 'var(--moments-gap, 8px)',
      padding: 'var(--moments-padding, 11px)', pointerEvents: 'none',
    });

    const bar = document.createElement('div');
    bar.className = 'arcade-room-bar';
    Object.assign(bar.style, {
      display: 'flex', alignItems: 'center', gap: 'var(--moments-gap, 8px)',
      color: 'var(--text)', pointerEvents: 'auto',
    });
    this.titleElement = document.createElement('strong');
    this.titleElement.textContent = arcadeText('arcade.room_title', 'Arcade Room');
    this.titleElement.style.fontSize = 'var(--font-title-large)';
    this.statusElement = document.createElement('span');
    this.statusElement.className = 'arcade-room-status';
    this.statusElement.style.cssText = 'color:var(--section-muted);font-size:var(--font-body-small);';
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'icon-button';
    close.textContent = '×';
    close.setAttribute('aria-label', arcadeText('arcade.close', 'Close Arcade Room'));
    close.style.cssText = 'margin-left:auto;border:1px solid var(--border-card);border-radius:var(--radius-cover);padding:4px 9px;background:var(--surface-card);color:var(--text);font-size:var(--font-subtitle);';
    close.addEventListener('click', () => this.close());
    bar.append(this.titleElement, this.statusElement, close);

    const card = document.createElement('div');
    card.className = 'arcade-room-facts';
    Object.assign(card.style, {
      alignSelf: 'flex-start', maxWidth: 'min(620px, 90vw)', padding: 'var(--moments-padding, 11px)',
      border: '1px solid var(--border-card)', borderRadius: 'var(--radius-panel)',
      background: 'var(--surface-sheet)', color: 'var(--text)', pointerEvents: 'auto',
    });
    this.factsElement = card;

    const actions = document.createElement('div');
    actions.className = 'arcade-room-actions';
    Object.assign(actions.style, {
      display: 'flex', flexWrap: 'wrap', gap: 'var(--moments-gap, 8px)', pointerEvents: 'auto',
    });
    const details = document.createElement('button');
    details.type = 'button'; details.className = 'icon-button'; details.textContent = arcadeText('arcade.open_details', 'Open game details');
    details.addEventListener('click', () => this.showCurrentGame());
    this.detailsButton = details;
    const play = document.createElement('button');
    play.type = 'button'; play.className = 'primary'; play.textContent = arcadeText('arcade.play', 'Play');
    play.addEventListener('click', () => { this.launchCurrentGame(); });
    actions.append(details, play);

    const help = document.createElement('p');
    help.className = 'arcade-room-help';
    help.textContent = arcadeText('arcade.help', '← → Walk · ↑ ↓ Zones · Enter Details · Space Play · Esc Close');
    help.style.cssText = 'margin:0;color:var(--nav-muted);font-size:var(--font-label);pointer-events:none;';
    this.hud.append(bar, card, actions, help);
    this.root.appendChild(this.hud);
  }

  mount(target = this.container) {
    if (target) this.container = target;
    this.createOverlay();
    if (!this.canvas) return this;
    this.context = this.canvas.getContext?.('2d') || null;
    this.bindEvents();
    this.resizeCanvas();
    this.syncGames();
    this.render();
    return this;
  }

  open() {
    if (!this.root || !this.canvas) this.mount(this.container);
    if (!this.root || !this.canvas) return this;
    this.previousFocus = hasDocument() ? document.activeElement : null;
    if (this.previousHidden === null) this.previousHidden = Boolean(this.root.hidden);
    this.root.hidden = false;
    this.opened = true;
    this.museumUnlocked = false;
    this.museumKioskPending = false;
    this.reducedMotion = prefersReducedMotion();
    this.syncGames();
    this.resizeCanvas();
    this.renderStartedAt = clock();
    this.noteActivity();
    safeFocus(this.root);
    this.startGamepadPoll();
    if (!this.reducedMotion) this.startAnimation();
    dispatchDocumentEvent('app:arcade-room-opened', { room: this });
  }

  close({ restoreFocus = true } = {}) {
    if (!this.opened && (!this.root || this.root.hidden)) return;
    this.exitMuseumMode({ schedule: false });
    this.opened = false;
    this.stopAnimation();
    this.stopGamepadPoll();
    this.clearIdleTimer();
    if (this.root && this.ownsRoot) this.root.hidden = true;
    if (restoreFocus) safeFocus(this.previousFocus);
    dispatchDocumentEvent('app:arcade-room-closed', { room: this });
  }

  dispose() {
    this.close({ restoreFocus: false });
    this.unbindEvents();
    this.resizeObserver?.disconnect?.();
    this.resizeObserver = null;
    this.textureCache.clear();
    if (this.root && this.ownsRoot) this.root.remove();
    this.root = null;
    this.canvas = null;
    this.context = null;
  }

  bindEvents() {
    if (this.bound || !hasDocument() || !this.canvas) return;
    this.bound = true;
    document.addEventListener('keydown', this._onKeyDown);
    document.addEventListener(SHOW_GAME_EVENT, this._onShowGame);
    document.addEventListener(LAUNCH_EVENT, this._onLaunch);
    document.addEventListener('app:launch', this._onLaunch);
    document.addEventListener('app:state-refreshed', this._onStateRefresh);
    document.addEventListener('localechange', this._onLocaleChange);
    this.canvas.addEventListener('pointerdown', this._onPointerDown, { passive: true });
    this.canvas.addEventListener('pointerup', this._onPointerUp, { passive: true });
    this.canvas.addEventListener('touchstart', this._onTouchStart, { passive: true });
    this.canvas.addEventListener('touchend', this._onTouchEnd, { passive: true });
    this.canvas.addEventListener('wheel', this._onWheel, { passive: true });
    if (hasWindow()) {
      window.addEventListener('resize', this._onResize);
      window.addEventListener('orientationchange', this._onResize);
      window.addEventListener('gamepadconnected', this._onGamepadConnected);
      if (typeof window.matchMedia === 'function') {
        try {
          this.mediaQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
          this.mediaQuery.addEventListener?.('change', this._onMotionChange);
          this.mediaQuery.addListener?.(this._onMotionChange);
        } catch { this.mediaQuery = null; }
      }
    }
    if (typeof ResizeObserver === 'function') {
      try {
        this.resizeObserver = new ResizeObserver(this._onResize);
        this.resizeObserver.observe(this.root || this.canvas);
      } catch { this.resizeObserver = null; }
    }
  }

  unbindEvents() {
    if (!this.bound || !hasDocument()) return;
    this.bound = false;
    document.removeEventListener('keydown', this._onKeyDown);
    document.removeEventListener(SHOW_GAME_EVENT, this._onShowGame);
    document.removeEventListener(LAUNCH_EVENT, this._onLaunch);
    document.removeEventListener('app:launch', this._onLaunch);
    document.removeEventListener('app:state-refreshed', this._onStateRefresh);
    document.removeEventListener('localechange', this._onLocaleChange);
    this.canvas?.removeEventListener('pointerdown', this._onPointerDown);
    this.canvas?.removeEventListener('pointerup', this._onPointerUp);
    this.canvas?.removeEventListener('touchstart', this._onTouchStart);
    this.canvas?.removeEventListener('touchend', this._onTouchEnd);
    this.canvas?.removeEventListener('wheel', this._onWheel);
    if (hasWindow()) {
      window.removeEventListener('resize', this._onResize);
      window.removeEventListener('orientationchange', this._onResize);
      window.removeEventListener('gamepadconnected', this._onGamepadConnected);
      this.mediaQuery?.removeEventListener?.('change', this._onMotionChange);
      this.mediaQuery?.removeListener?.(this._onMotionChange);
    }
  }

  resizeCanvas() {
    if (!this.canvas) return { width: this.width, height: this.height, dpr: this.dpr };
    const rect = this.canvas.getBoundingClientRect?.() || {};
    const widthFallback = this.canvas.clientWidth || (hasWindow() ? window.innerWidth : 960) || 960;
    const heightFallback = this.canvas.clientHeight || (hasWindow() ? window.innerHeight : 540) || 540;
    const width = Math.max(1, Math.round(numberOr(rect.width > 0 ? rect.width : widthFallback, widthFallback)));
    const height = Math.max(1, Math.round(numberOr(rect.height > 0 ? rect.height : heightFallback, heightFallback)));
    const dpr = clamp(numberOr(hasWindow() ? window.devicePixelRatio : 1, 1), 1, MAX_DPR);
    const pixelWidth = Math.max(1, Math.round(width * dpr));
    const pixelHeight = Math.max(1, Math.round(height * dpr));
    if (this.canvas.width !== pixelWidth) this.canvas.width = pixelWidth;
    if (this.canvas.height !== pixelHeight) this.canvas.height = pixelHeight;
    this.width = width;
    this.height = height;
    this.dpr = dpr;
    this.context = this.context || this.canvas.getContext?.('2d') || null;
    if (this.context?.setTransform) this.context.setTransform(dpr, 0, 0, dpr, 0, 0);
    else if (this.context?.scale) this.context.scale(dpr, dpr);
    if (this.opened) this.render();
    return { width, height, dpr, pixelWidth, pixelHeight };
  }

  startAnimation() {
    if (this.frame || this.reducedMotion || !this.opened) return;
    const tick = timestamp => {
      this.frame = 0;
      if (!this.opened || this.reducedMotion) return;
      this.render(timestamp);
      this.frame = requestFrame(tick);
    };
    this.frame = requestFrame(tick);
  }

  stopAnimation() {
    if (this.frame) cancelFrame(this.frame);
    this.frame = 0;
  }

  startGamepadPoll() {
    if (!this.opened || this.gamepadFrame || !hasWindow() || typeof navigator?.getGamepads !== 'function') return;
    const poll = () => {
      this.gamepadFrame = 0;
      if (!this.opened) return;
      this.pollGamepads();
      this.gamepadFrame = requestFrame(poll);
    };
    this.gamepadFrame = requestFrame(poll);
  }

  stopGamepadPoll() {
    if (this.gamepadFrame) cancelFrame(this.gamepadFrame);
    this.gamepadFrame = 0;
    this.gamepadState = {};
  }

  controllerMap() {
    return { ...defaultControllerMap, ...(this.state?.appSettings?.controller_map || {}), ...(this.options.controllerMap || {}) };
  }

  pollGamepads() {
    if (!hasWindow() || typeof navigator?.getGamepads !== 'function') return;
    let pads = [];
    try { pads = [...(navigator.getGamepads() || [])].filter(Boolean); } catch { return; }
    const pad = pads.find(item => item.connected !== false);
    if (!pad) return;
    const map = this.controllerMap();
    const pressed = index => Boolean(pad.buttons?.[index]?.pressed);
    const axis = index => numberOr(pad.axes?.[index], 0);
    const current = {
      left: pressed(14) || axis(0) < -0.5,
      right: pressed(15) || axis(0) > 0.5,
      up: pressed(12) || axis(1) < -0.5,
      down: pressed(13) || axis(1) > 0.5,
      play: pressed(map.play ?? 0),
      back: pressed(map.back ?? 1),
      favorite: pressed(map.favorite ?? 2),
      menu: pressed(map.menu ?? 9),
    };
    const edge = key => current[key] && !this.gamepadState[key];
    const any = Object.values(current).some(Boolean);
    if (any) {
      if (this.museum) {
        this.exitMuseumMode();
      } else {
        if (edge('left') || edge('up')) this.move(-1, edge('up') ? 'zone' : 'cabinet');
        if (edge('right') || edge('down')) this.move(1, edge('down') ? 'zone' : 'cabinet');
        if (edge('play')) this.launchCurrentGame();
        if (edge('back')) this.close();
        if (edge('favorite')) this.showCurrentGame();
        if (edge('menu')) this.toggleMuseumMode();
      }
      this.noteActivity();
    }
    this.gamepadState = current;
  }

  isEditableTarget(target) {
    const tag = String(target?.tagName || '').toLowerCase();
    return target?.isContentEditable || ['input', 'textarea', 'select'].includes(tag);
  }

  handleKeyDown(event) {
    if (!this.opened || this.isEditableTarget(event.target)) return;
    if (this.museum) {
      this.exitMuseumMode();
      event.preventDefault();
      return;
    }
    const key = event.key;
    let handled = true;
    if (key === 'ArrowLeft') this.move(-1, 'cabinet');
    else if (key === 'ArrowRight') this.move(1, 'cabinet');
    else if (key === 'ArrowUp') this.move(-1, 'zone');
    else if (key === 'ArrowDown') this.move(1, 'zone');
    else if (key === 'Home') this.jumpToEdge(-1);
    else if (key === 'End') this.jumpToEdge(1);
    else if (key === 'Enter') this.showCurrentGame();
    else if (key === ' ' || key.toLowerCase() === 'p') this.launchCurrentGame();
    else if (key === 'Escape' || key === 'Backspace') this.close();
    else if (key.toLowerCase() === 'm') this.toggleMuseumMode();
    else handled = false;
    if (handled) {
      this.noteActivity();
      event.preventDefault();
    }
  }

  handlePointerDown(event) {
    if (event.pointerType === 'touch') return;
    if (!this.opened) return;
    this.touchStart = { x: event.clientX, y: event.clientY, time: clock() };
  }

  handlePointerUp(event) {
    if (event.pointerType === 'touch' || !this.opened || !this.touchStart) return;
    const start = this.touchStart;
    this.touchStart = null;
    this.handleGesture(event.clientX - start.x, event.clientY - start.y, clock() - start.time);
  }

  handleTouchStart(event) {
    const touch = event.changedTouches?.[0];
    if (!touch || !this.opened) return;
    this.lastTouchAt = clock();
    this.touchStart = { x: touch.clientX, y: touch.clientY, time: this.lastTouchAt };
  }

  handleTouchEnd(event) {
    const touch = event.changedTouches?.[0];
    if (!touch || !this.opened || !this.touchStart) return;
    const start = this.touchStart;
    this.touchStart = null;
    this.handleGesture(touch.clientX - start.x, touch.clientY - start.y, clock() - start.time);
  }

  handleGesture(deltaX, deltaY, elapsed) {
    if (this.museum) {
      this.exitMuseumMode();
      return;
    }
    if (Math.abs(deltaX) >= SWIPE_DISTANCE && Math.abs(deltaX) >= Math.abs(deltaY)) {
      this.move(deltaX < 0 ? 1 : -1, 'cabinet');
    } else if (Math.abs(deltaY) >= SWIPE_DISTANCE) {
      this.move(deltaY < 0 ? 1 : -1, 'zone');
    } else if (elapsed < 700) {
      this.showCurrentGame();
    }
    this.noteActivity();
  }

  handleWheel(event) {
    if (!this.opened) return;
    if (this.museum) this.exitMuseumMode();
    else if (Math.abs(event.deltaY) > Math.abs(event.deltaX)) this.move(event.deltaY > 0 ? 1 : -1, 'cabinet');
    else if (event.deltaX) this.move(event.deltaX > 0 ? 1 : -1, 'cabinet');
    this.noteActivity();
  }

  handleExternalGameEvent(event) {
    if (!this.opened || event?.detail?.source === 'arcaderoom') return;
    const target = gameFromState(readEventGame(event), this.state);
    if (!target) return;
    const zoneIndex = this.zones.findIndex(zone => zone.games.some(game => sameGame(game, target)));
    if (zoneIndex < 0) return;
    const cabinetIndex = this.zones[zoneIndex].games.findIndex(game => sameGame(game, target));
    this.zoneIndex = zoneIndex;
    this.cabinetIndex = Math.max(0, cabinetIndex);
    if (this.museum) this.exitMuseumMode();
    this.render();
  }

  move(delta, axis = 'cabinet') {
    if (!this.zones.length) return false;
    const change = delta < 0 ? -1 : 1;
    if (axis === 'zone') {
      this.zoneIndex = (this.zoneIndex + change + this.zones.length) % this.zones.length;
      const zone = this.currentZone;
      this.cabinetIndex = zone?.games?.length ? clamp(this.cabinetIndex, 0, zone.games.length - 1) : 0;
    } else {
      const zone = this.currentZone;
      const count = zone?.games?.length || 0;
      if (!count) return false;
      const next = this.cabinetIndex + change;
      if (next < 0 || next >= count) {
        this.zoneIndex = (this.zoneIndex + change + this.zones.length) % this.zones.length;
        const target = this.currentZone;
        this.cabinetIndex = change > 0 ? 0 : Math.max(0, (target?.games?.length || 1) - 1);
      } else this.cabinetIndex = next;
    }
    this.render();
    return true;
  }

  jumpToEdge(direction) {
    if (!this.currentZone?.games?.length) return;
    this.cabinetIndex = direction < 0 ? 0 : this.currentZone.games.length - 1;
    this.render();
  }

  showCurrentGame() {
    const game = this.currentGame;
    if (!game) return false;
    const detail = { gameId: game.game_id ?? game.id, game, source: 'arcaderoom' };
    dispatchDocumentEvent(SHOW_GAME_EVENT, detail);
    if (typeof this.options.onShowGame === 'function') {
      try { this.options.onShowGame(game, detail); } catch { /* host callback is optional */ }
    }
    this.close();
    return true;
  }

  async launchCurrentGame() {
    const game = this.currentGame;
    if (!game) return false;
    const detail = { gameId: game.game_id ?? game.id, game, source: 'arcaderoom' };
    const event = dispatchDocumentEvent(LAUNCH_EVENT, detail, true);
    if (event.defaultPrevented) {
      this.close();
      return true;
    }
    const callback = this.options.onLaunch ||
      (hasWindow() && (window.openBoxLaunch || window.OpenBox?.launch));
    try {
      if (typeof callback === 'function') {
        await callback(game, detail);
      } else if (this.options.fallbackLaunch !== false) {
        // The import keeps the room free of a static sessions/library cycle,
        // while still using the app's real preflight and launch path.
        const sessions = await import('./sessions.js');
        if (typeof sessions.launch === 'function') await sessions.launch(game.id ?? game.game_id);
      }
      this.close();
      return true;
    } catch (error) {
      dispatchDocumentEvent('app:launch-failed', { ...detail, error });
      return false;
    }
  }

  currentScreen(game, timestamp) {
    const sources = mediaSourceList(game, this);
    if (!sources.length) return null;
    const index = Math.floor((numberOr(timestamp, clock()) - this.renderStartedAt) / SCREEN_CYCLE_MS) % sources.length;
    return sources[(index + sources.length) % sources.length];
  }

  noteActivity() {
    if (!this.opened) return;
    if (this.museum) {
      this.exitMuseumMode();
      return;
    }
    this.scheduleMuseumMode();
  }

  clearIdleTimer() {
    if (this.idleTimer) clearTimeout(this.idleTimer);
    this.idleTimer = 0;
  }

  scheduleMuseumMode() {
    this.clearIdleTimer();
    if (!this.opened || this.museum || !this.zones.some(zone => zone.games.length) || this.idleMilliseconds === 0) return;
    this.idleTimer = setTimeout(() => {
      this.idleTimer = 0;
      this.enterMuseumMode();
    }, this.idleMilliseconds);
    return this.idleTimer;
  }

  stopMuseumMode() {
    if (this.museumTimer) clearTimeout(this.museumTimer);
    this.museumTimer = 0;
  }

  enterMuseumMode() {
    if (!this.opened || !this.zones.some(zone => zone.games.length)) return false;
    if (this.options.museumKioskEnabled && !this.museumUnlocked) {
      if (this.museumKioskPending) return false;
      const verify = this.options.verifyMuseumPin;
      if (typeof verify !== 'function') return false;
      this.museumKioskPending = true;
      Promise.resolve(verify()).then(ok => {
        this.museumKioskPending = false;
        if (ok) {
          this.museumUnlocked = true;
          this.enterMuseumMode();
        }
      }).catch(() => { this.museumKioskPending = false; });
      return false;
    }
    this.clearIdleTimer();
    this.museum = true;
    this.stopMuseumMode();
    this.render();
    dispatchDocumentEvent('app:arcade-museum-entered', { room: this, game: this.currentGame });
    if (!this.reducedMotion) this.scheduleMuseumStep();
    return true;
  }

  scheduleMuseumStep() {
    this.stopMuseumMode();
    if (!this.museum || !this.opened || this.reducedMotion) return;
    this.museumTimer = setTimeout(() => {
      this.museumTimer = 0;
      if (!this.museum || !this.opened) return;
      this.move(1, 'cabinet');
      this.scheduleMuseumStep();
    }, numberOr(this.options.museumStepMs, MUSEUM_STEP_MS));
  }

  exitMuseumMode({ schedule = true } = {}) {
    if (!this.museum) return false;
    this.museum = false;
    this.stopMuseumMode();
    if (schedule) this.scheduleMuseumMode();
    this.render();
    dispatchDocumentEvent('app:arcade-museum-exited', { room: this });
    return true;
  }

  toggleMuseumMode() {
    return this.museum ? this.exitMuseumMode() : this.enterMuseumMode();
  }

  drawMissingArt(context, game, x, y, width, height, tokens, label = arcadeText('arcade.no_art', 'NO ART')) {
    const palette = [tokens.brand, tokens.action, tokens.gold, tokens.accent];
    const seed = [...gameTitle(game)].reduce((sum, char) => sum + char.charCodeAt(0), 0);
    fillRoundRect(context, x, y, width, height, 5, palette[seed % palette.length]);
    context.fillStyle = tokens.actionInk;
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.font = `900 ${tokens.fontHeading} ui-sans-serif,system-ui,sans-serif`;
    context.fillText(initials(gameTitle(game)), x + width / 2, y + height / 2 - 5);
    context.font = `700 ${tokens.fontMicro} ui-sans-serif,system-ui,sans-serif`;
    context.fillText(label, x + width / 2, y + height - 10);
    context.textAlign = 'left';
    context.textBaseline = 'alphabetic';
  }

  drawTexture(context, source, x, y, width, height, tokens, game) {
    if (!source?.url) return false;
    const texture = this.textureCache.get(source.url, source.kind);
    if (!texture) {
      if (!this.textureCache.has(source.url, source.kind)) {
        this.textureCache.load(source.url, source.kind).then(() => { if (this.opened) this.render(); });
      }
      return false;
    }
    if (source.kind === 'video') {
      if (texture.readyState < 2) return false;
      if (!this.reducedMotion) texture.play?.().catch?.(() => {});
    }
    try {
      const sourceWidth = Number(texture.videoWidth || texture.naturalWidth || texture.width || 0);
      const sourceHeight = Number(texture.videoHeight || texture.naturalHeight || texture.height || 0);
      if (!sourceWidth || !sourceHeight) return false;
      const scale = Math.max(width / sourceWidth, height / sourceHeight);
      const drawWidth = sourceWidth * scale;
      const drawHeight = sourceHeight * scale;
      context.save();
      roundedPath(context, x, y, width, height, 5);
      context.clip();
      context.drawImage(texture, x + (width - drawWidth) / 2, y + (height - drawHeight) / 2, drawWidth, drawHeight);
      context.restore();
      return true;
    } catch {
      return false;
    }
  }

  drawCabinet(context, game, x, floor, cabinetWidth, cabinetHeight, selected, timestamp, tokens) {
    const top = floor - cabinetHeight;
    fillRoundRect(context, x, top, cabinetWidth, cabinetHeight, 8, tokens.panel);
    strokeRoundRect(context, x, top, cabinetWidth, cabinetHeight, 8, selected ? tokens.focus : tokens.border, selected ? 3 : 1);

    const marqueeHeight = Math.max(34, cabinetHeight * 0.19);
    const logoKind = mediaAvailable(game, 'clear_logo') ? 'clear_logo' : mediaAvailable(game, 'cover') ? 'cover' : '';
    const logoSource = logoKind ? { kind: 'image', url: this.mediaUrl(game, logoKind) } : null;
    const logoDrawn = this.drawTexture(context, logoSource, x + 8, top + 8, cabinetWidth - 16, marqueeHeight - 12, tokens, game);
    if (!logoDrawn) this.drawMissingArt(context, game, x + 8, top + 8, cabinetWidth - 16, marqueeHeight - 12, tokens, arcadeText('arcade.marquee', 'MARQUEE'));

    const screenTop = top + marqueeHeight + 12;
    const screenHeight = Math.max(48, cabinetHeight * 0.42);
    fillRoundRect(context, x + 12, screenTop, cabinetWidth - 24, screenHeight, 5, tokens.field);
    const screen = this.currentScreen(game, timestamp);
    const screenDrawn = this.drawTexture(context, screen, x + 15, screenTop + 3, cabinetWidth - 30, screenHeight - 6, tokens, game);
    if (!screenDrawn) this.drawMissingArt(context, game, x + 15, screenTop + 3, cabinetWidth - 30, screenHeight - 6, tokens, arcadeText('arcade.screen', 'SCREEN'));

    const controlTop = screenTop + screenHeight + 16;
    context.fillStyle = tokens.muted;
    context.fillRect(x + 17, controlTop, cabinetWidth - 34, 2);
    context.fillStyle = tokens.brand;
    context.beginPath(); context.arc(x + cabinetWidth * 0.66, controlTop + 13, 5, 0, Math.PI * 2); context.fill();
    context.fillStyle = tokens.action;
    context.beginPath(); context.arc(x + cabinetWidth * 0.76, controlTop + 13, 5, 0, Math.PI * 2); context.fill();
    context.fillStyle = tokens.section;
    context.font = `700 ${tokens.fontMicro} ui-sans-serif,system-ui,sans-serif`;
    context.fillText(gameTitle(game).slice(0, 26), x + 12, floor - 11);
    if (selected) {
      context.fillStyle = tokens.focus;
      context.fillRect(x + 10, floor + 4, cabinetWidth - 20, 3);
    }
  }

  drawZone(context, zone, offset, zoneWidth, centerX, floor, cabinetWidth, cabinetHeight, tokens, timestamp) {
    const zoneCenter = centerX + offset * zoneWidth;
    const zonePanelWidth = Math.max(zoneWidth - 28, cabinetWidth + 28);
    const panelLeft = zoneCenter - zonePanelWidth / 2;
    const panelTop = Math.max(80, floor - cabinetHeight - 66);
    fillRoundRect(context, panelLeft, panelTop, zonePanelWidth, cabinetHeight + 92, 12, tokens.bg);
    strokeRoundRect(context, panelLeft, panelTop, zonePanelWidth, cabinetHeight + 92, 12, tokens.line, 1);
    context.fillStyle = offset === 0 ? tokens.focus : tokens.section;
    context.font = `800 ${tokens.fontLabel} ui-sans-serif,system-ui,sans-serif`;
    context.fillText(zone.name, panelLeft + 16, panelTop + 23);
    context.fillStyle = tokens.muted;
    context.font = `${tokens.fontMicro} ui-sans-serif,system-ui,sans-serif`;
    const cabinetLabel = zone.games.length
      ? arcadeText(zone.games.length === 1 ? 'arcade.cabinet_singular' : 'arcade.cabinet_plural',
        `${zone.games.length} cabinet${zone.games.length === 1 ? '' : 's'}`, { count: zone.games.length })
      : arcadeText('arcade.empty_zone', 'Empty zone');
    context.fillText(cabinetLabel, panelLeft + 16, panelTop + 40);

    if (!zone.games.length) {
      this.drawMissingArt(context, { name: zone.name }, zoneCenter - cabinetWidth / 2, floor - cabinetHeight * 0.7, cabinetWidth, cabinetHeight * 0.7, tokens, arcadeText('arcade.empty_zone_art', 'EMPTY ZONE'));
      return;
    }
    const indexes = visibleWindow(zone.games.length, offset === 0 ? this.cabinetIndex : 0, CABINET_WINDOW_RADIUS);
    const pitch = cabinetWidth + 18;
    for (const index of indexes) {
      const relative = offset === 0 ? index - this.cabinetIndex : index - Math.min(indexes[0], 1);
      const x = zoneCenter + relative * pitch - cabinetWidth / 2;
      if (x + cabinetWidth < panelLeft || x > panelLeft + zonePanelWidth) continue;
      this.drawCabinet(context, zone.games[index], x, floor, cabinetWidth, cabinetHeight, offset === 0 && index === this.cabinetIndex, timestamp, tokens);
    }
  }

  drawEmptyRoom(context, tokens) {
    context.fillStyle = tokens.text;
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.font = `900 ${tokens.fontDisplay} ui-sans-serif,system-ui,sans-serif`;
    context.fillText(arcadeText('arcade.room_title', 'Arcade Room'), this.width / 2, this.height / 2 - 28);
    context.fillStyle = tokens.section;
    context.font = `${tokens.fontBody} ui-sans-serif,system-ui,sans-serif`;
    context.fillText(arcadeText('arcade.empty_room_message', 'Your library has no cabinets yet.'), this.width / 2, this.height / 2 + 18);
    context.font = `${tokens.fontLabel} ui-sans-serif,system-ui,sans-serif`;
    context.fillText(arcadeText('arcade.empty_room_hint', 'Import or add a game to start the exhibit.'), this.width / 2, this.height / 2 + 46);
    context.textAlign = 'left';
    context.textBaseline = 'alphabetic';
  }

  draw(timestamp = clock()) {
    const context = this.context;
    if (!context || !this.width || !this.height) return;
    const tokens = themeTokens();
    context.clearRect(0, 0, this.width, this.height);
    context.fillStyle = tokens.bg;
    context.fillRect(0, 0, this.width, this.height);
    if (!this.zones.some(zone => zone.games.length)) {
      this.drawEmptyRoom(context, tokens);
      return;
    }

    // The room remains bounded by drawing only the active zone and its two
    // neighbours.  Each zone in turn draws a cabinet window around focus.
    const floor = this.height * 0.82;
    const cabinetHeight = clamp(this.height * 0.48, 220, 430);
    const cabinetWidth = clamp(this.width * 0.17, 128, 220);
    const zoneWidth = Math.max(this.width * 0.78, cabinetWidth * 2.4);
    const centerX = this.width / 2;
    context.fillStyle = tokens.field;
    context.fillRect(0, floor, this.width, this.height - floor);
    context.fillStyle = tokens.line;
    context.fillRect(0, floor, this.width, 2);
    for (let offset = -ZONE_WINDOW_RADIUS; offset <= ZONE_WINDOW_RADIUS; offset += 1) {
      const index = (this.zoneIndex + offset + this.zones.length) % this.zones.length;
      this.drawZone(context, this.zones[index], offset, zoneWidth, centerX, floor, cabinetWidth, cabinetHeight, tokens, timestamp);
    }
    if (this.museum) {
      context.fillStyle = tokens.gold;
      context.font = `800 ${tokens.fontLabel} ui-sans-serif,system-ui,sans-serif`;
      context.fillText(this.reducedMotion
        ? arcadeText('arcade.museum_static', 'MUSEUM · STATIC')
        : arcadeText('arcade.museum_attract', 'MUSEUM · ATTRACT MODE'), 20, this.height - 18);
    }
  }

  updateHud() {
    if (!this.statusElement || !this.factsElement) return;
    const zone = this.currentZone;
    const game = this.currentGame;
    if (this.detailsButton) this.detailsButton.disabled = this.museum;
    this.titleElement.textContent = this.museum
      ? arcadeText('arcade.museum_title', 'Museum')
      : arcadeText('arcade.room_title', 'Arcade Room');
    this.statusElement.textContent = zone
      ? `${zone.name} · ${game ? gameTitle(game) : arcadeText('arcade.empty_zone', 'Empty zone')}`
      : arcadeText('arcade.no_zones', 'No zones');
    const facts = buildMuseumFacts(game);
    const copy = this.museum
      ? `<strong>${escapeHtml(game ? gameTitle(game) : arcadeText('arcade.empty_exhibit', 'Empty exhibit'))}</strong><br><span>${escapeHtml(formatArcadeFacts(facts) || arcadeText('arcade.no_exhibit_notes', 'No exhibit notes yet.'))}</span>`
      : `<strong>${escapeHtml(game ? gameTitle(game) : arcadeText('arcade.no_cabinet', 'No cabinet selected'))}</strong><br><span>${escapeHtml(facts.length ? formatArcadeFacts(facts) : arcadeText('arcade.select_cabinet', 'Select a cabinet to inspect it.'))}</span>`;
    this.factsElement.innerHTML = copy;
  }

  render(timestamp = clock()) {
    if (!this.canvas) return;
    this.updateHud();
    this.draw(timestamp);
  }
}

let activeRoom = null;

function createArcadeRoom(options = {}) {
  return new ArcadeRoom(options);
}

function openArcadeRoom(options = {}) {
  if (!activeRoom) activeRoom = createArcadeRoom(options);
  else if (options && Object.keys(options).length) {
    activeRoom.options = { ...activeRoom.options, ...options };
    if (options.state) activeRoom.state = options.state;
    if (options.games || options.platforms) activeRoom.syncGames();
  }
  activeRoom.open();
  return activeRoom;
}

function closeArcadeRoom() {
  activeRoom?.close();
}

function renderArcadeRoom(timestamp) {
  activeRoom?.render(timestamp);
}

function getArcadeRoom() {
  return activeRoom;
}

function moveArcadeRoom(delta, axis = 'cabinet') {
  return activeRoom?.move(delta, axis) || false;
}

function enterMuseumMode() {
  return activeRoom?.enterMuseumMode() || false;
}

function exitMuseumMode() {
  return activeRoom?.exitMuseumMode() || false;
}

function scheduleMuseumMode() {
  return activeRoom?.scheduleMuseumMode() || 0;
}

function stopMuseumMode() {
  activeRoom?.stopMuseumMode();
}

function resizeArcadeCanvas() {
  return activeRoom?.resizeCanvas() || null;
}

// Event hooks keep this module useful before a dedicated Tools button or a
// Big Box layout is wired.  Existing app:show-game and launch events select a
// cabinet when the room is open; opening is explicit and never steals focus.
if (hasDocument()) {
  document.addEventListener('app:open-arcade-room', event => openArcadeRoom(event.detail || {}));
  document.addEventListener('app:close-arcade-room', () => closeArcadeRoom());
}

if (hasWindow()) {
  const bridge = window.OpenBoxArcadeRoom || {};
  window.OpenBoxArcadeRoom = {
    ...bridge,
    open: openArcadeRoom,
    close: closeArcadeRoom,
    render: renderArcadeRoom,
    move: moveArcadeRoom,
    enterMuseum: enterMuseumMode,
    exitMuseum: exitMuseumMode,
  };
}

export {
  ArcadeRoom,
  TextureCache,
  MAX_TEXTURE_CACHE,
  CABINET_WINDOW_RADIUS,
  groupArcadeZones,
  visibleWindow,
  buildMuseumFacts,
  createArcadeRoom,
  openArcadeRoom,
  closeArcadeRoom,
  renderArcadeRoom,
  getArcadeRoom,
  moveArcadeRoom,
  enterMuseumMode,
  exitMuseumMode,
  scheduleMuseumMode,
  stopMuseumMode,
  resizeArcadeCanvas,
};
