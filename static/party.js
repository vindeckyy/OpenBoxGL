/* party.js — Game Night Big Box party mode: setup, wheel, up-next, launch. */
import { $, escapeHtml } from './util.js';
import { t } from './i18n.js';
import { AppState, api, media, notify } from './state.js';
import { launch } from './sessions.js';

const WHEEL_COLORS = ['var(--brand)', 'var(--accent)', 'var(--active)', 'var(--focus)'];
const SPIN_MS = 2400;
const SESSION_LENGTHS = [0, 15, 30, 45, 60, 90, 120];

let players = 2;
let minutes = 0;
let queue = [];
let queueIndex = 0;
let queueGames = [];
let spun = false;
let spinning = false;
let currentRotation = 0;
let keyHandler = null;

function partyOverlayOpen() {
  return Boolean($('partyOverlay') && !$('partyOverlay').hidden && !$('bigBox').hidden);
}

function clampPlayers(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 2;
  return Math.max(2, Math.min(8, Math.round(n)));
}

function resolveQueueGames() {
  const byId = new Map();
  for (const game of AppState.games || []) {
    byId.set(String(game.game_id || ''), game);
    byId.set(String(game.id || ''), game);
  }
  queueGames = queue.map(id => byId.get(String(id))).filter(Boolean);
}

function openParty() {
  if ($('bigBoxMenu')) $('bigBoxMenu').hidden = true;
  players = clampPlayers(AppState.appSettings.party_players || 2);
  minutes = 0;
  queue = [];
  queueIndex = 0;
  queueGames = [];
  spun = false;
  spinning = false;
  $('partyOverlay').hidden = false;
  renderSetup();
  if (!keyHandler) {
    keyHandler = event => partyKeydown(event);
    // Capture phase: the overlay swallows keys so Big Box navigation
    // (arrows/Enter/Escape on #bigBox) does not fire underneath it.
    document.addEventListener('keydown', keyHandler, true);
  }
  $('partyBuild')?.focus();
}

function closeParty() {
  if ($('partyOverlay')) $('partyOverlay').hidden = true;
  if (keyHandler) {
    document.removeEventListener('keydown', keyHandler, true);
    keyHandler = null;
  }
  if ($('bigBox') && !$('bigBox').hidden) $('bigBox').focus();
}

function partyKeydown(event) {
  if (!partyOverlayOpen()) {
    // Big Box closed under us: drop the listener.
    if ($('partyOverlay') && !$('partyOverlay').hidden) closeParty();
    return;
  }
  event.stopPropagation();
  if (event.key === 'Escape') {
    event.preventDefault();
    closeParty();
    return;
  }
  const tag = (document.activeElement?.tagName || '').toUpperCase();
  if (event.key === 'Enter' && tag !== 'BUTTON') {
    event.preventDefault();
    primaryAction();
    return;
  }
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    event.preventDefault();
    if (isSetupView()) {
      players = clampPlayers(players + (event.key === 'ArrowRight' ? 1 : -1));
      renderSetup();
    }
  } else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
    event.preventDefault();
    if (isSetupView()) {
      const at = SESSION_LENGTHS.indexOf(minutes);
      const next = event.key === 'ArrowDown'
        ? SESSION_LENGTHS[Math.min(SESSION_LENGTHS.length - 1, (at < 0 ? 0 : at) + 1)]
        : SESSION_LENGTHS[Math.max(0, (at < 0 ? 0 : at) - 1)];
      minutes = next;
      renderSetup();
    }
  } else if (event.key === 'Enter') {
    event.preventDefault();
    primaryAction();
  } else if (event.key.toLowerCase() === 'n' && !isSetupView()) {
    event.preventDefault();
    nextRound();
  }
}

/* Gamepad entry point, called from pollGamepads edge detection in bigbox.js:
 * left/right adjust setup values, play = primary (build/spin/launch),
 * back = close, menu = exit to the Big Box menu. */
function partyGamepad(edge) {
  if (edge('back')) {
    closeParty();
    return;
  }
  if (edge('menu')) {
    closeParty();
    import('./bigbox.js').then(mod => mod.openBigBoxMenu()).catch(() => {});
    return;
  }
  if (!isSetupView() && (edge('up') || edge('down'))) {
    if (edge('up')) nextRound();
    return;
  }
  if ((edge('left') || edge('right')) && isSetupView()) {
    players = clampPlayers(players + (edge('right') ? 1 : -1));
    renderSetup();
    return;
  }
  if (edge('play')) primaryAction();
}

function isSetupView() {
  return !queue.length;
}

function primaryAction() {
  if (isSetupView()) {
    if (!spinning) buildQueue();
    return;
  }
  if (spinning) return;
  if (!spun) {
    spinWheel();
    return;
  }
  launchWinner();
}

function setupHtml() {
  const lengthOptions = SESSION_LENGTHS.map(m =>
    `<option value="${m}"${m === minutes ? ' selected' : ''}>${m === 0 ? escapeHtml(t('party.minutes_any')) : `${m} min`}</option>`).join('');
  return `<div class="party-panel" role="dialog" aria-label="${escapeHtml(t('party.title'))}">
    <div class="dialog-head"><h2>${escapeHtml(t('party.title'))}</h2><button type="button" id="closeParty" aria-label="${escapeHtml(t('party.close'))}">×</button></div>
    <p class="description">${escapeHtml(t('party.hint'))}</p>
    <div class="party-setup">
      <label class="field"><span>${escapeHtml(t('party.players'))}</span>
        <span class="party-stepper"><button type="button" id="partyFewer" aria-label="−">−</button><strong id="partyPlayers">${players}</strong><button type="button" id="partyMore" aria-label="+">+</button></span>
      </label>
      <label class="field"><span>${escapeHtml(t('party.minutes'))}</span><select id="partyMinutes">${lengthOptions}</select></label>
    </div>
    <div class="dialog-actions">
      <button type="button" class="icon-button" id="closeParty2">${escapeHtml(t('party.close'))}</button>
      <button type="button" class="primary" id="partyBuild">${escapeHtml(t('party.build'))}</button>
    </div>
    <div id="partyStatus" class="description" role="status"></div>
  </div>`;
}

function wheelHtml() {
  return `<div class="party-panel party-wheel-panel" role="dialog" aria-label="${escapeHtml(t('party.title'))}">
    <div class="dialog-head"><h2>${escapeHtml(t('party.title'))}</h2><button type="button" id="closeParty" aria-label="${escapeHtml(t('party.close'))}">×</button></div>
    <div class="party-stage">
      <div class="party-wheel-wrap">
        <div class="party-pointer" aria-hidden="true"></div>
        <div class="party-wheel" id="partyWheel"></div>
        <div class="party-hub" id="partyHub"></div>
      </div>
      <div class="party-side">
        <div id="partyWinner" class="party-winner"></div>
        <div class="party-upnext"><h3>${escapeHtml(t('party.up_next'))}</h3><div id="partyUpNext" class="party-upnext-strip"></div></div>
        <div class="dialog-actions party-actions">
          <button type="button" class="icon-button" id="partyRebuild">${escapeHtml(t('party.rebuild'))}</button>
          <button type="button" class="icon-button" id="partyNext">${escapeHtml(t('party.next_round'))}</button>
          <button type="button" class="primary" id="partyPrimary">${escapeHtml(t('party.spin'))}</button>
        </div>
        <div id="partyStatus" class="description" role="status"></div>
      </div>
    </div>
  </div>`;
}

function renderSetup() {
  $('partyOverlay').innerHTML = setupHtml();
  $('closeParty').onclick = closeParty;
  $('closeParty2').onclick = closeParty;
  $('partyFewer').onclick = () => { players = clampPlayers(players - 1); renderSetup(); $('partyBuild')?.focus(); };
  $('partyMore').onclick = () => { players = clampPlayers(players + 1); renderSetup(); $('partyBuild')?.focus(); };
  $('partyMinutes').onchange = event => { minutes = Number(event.target.value) || 0; };
  $('partyBuild').onclick = () => buildQueue();
}

async function buildQueue() {
  if (spinning) return;
  spinning = true;
  const status = $('partyStatus');
  if (status) status.textContent = t('common.loading');
  try {
    const result = await api('/api/v2/party/queue', {
      method: 'POST',
      body: JSON.stringify({ players, minutes }),
    });
    queue = Array.isArray(result.queue) ? result.queue.map(String) : [];
    queueIndex = 0;
    spun = false;
    if (!queue.length) {
      if (status) status.textContent = t('party.empty');
      return;
    }
    resolveQueueGames();
    if (!queueGames.length) {
      if (status) status.textContent = t('party.empty');
      queue = [];
      return;
    }
    renderWheel();
  } catch (error) {
    if (status) status.textContent = error.message;
    else notify(error.message);
  } finally {
    spinning = false;
  }
}

function wheelGradient() {
  const n = queueGames.length;
  const seg = 360 / n;
  const stops = queueGames.map((_, i) =>
    `${WHEEL_COLORS[i % WHEEL_COLORS.length]} ${(i * seg).toFixed(2)}deg ${((i + 1) * seg).toFixed(2)}deg`).join(', ');
  return `conic-gradient(from -90deg, ${stops})`;
}

function renderWheel() {
  $('partyOverlay').innerHTML = wheelHtml();
  $('closeParty').onclick = closeParty;
  $('partyRebuild').onclick = () => { queue = []; renderSetup(); };
  $('partyNext').onclick = () => nextRound();
  $('partyPrimary').onclick = () => primaryAction();
  const wheel = $('partyWheel');
  wheel.style.background = wheelGradient();
  const n = queueGames.length;
  const seg = 360 / n;
  const labelCount = Math.min(n, 12);
  let labels = '';
  for (let i = 0; i < labelCount; i++) {
    const angle = i * seg + seg / 2;
    labels += `<span class="party-slice-label" style="transform: rotate(${angle.toFixed(2)}deg) translateY(-9.5rem)">${escapeHtml(queueGames[i].name)}</span>`;
  }
  wheel.innerHTML = labels;
  wheel.style.transform = 'rotate(0deg)';
  currentRotation = 0;
  renderHub();
  renderUpNext();
  setPrimary(t('party.spin'));
  $('partyPrimary')?.focus();
}

function renderHub() {
  const hub = $('partyHub');
  if (!hub) return;
  const game = queueGames[queueIndex];
  hub.innerHTML = game ? `<strong>${escapeHtml(game.name)}</strong><small>${escapeHtml(game.platform || '')}</small>` : '';
}

function coverThumb(game) {
  if (game.has_cover) {
    return `<img src="${escapeHtml(media(game, 'cover'))}" alt="" loading="lazy" decoding="async">`;
  }
  return `<span class="cover-title">${escapeHtml((game.name || '?').slice(0, 1))}</span>`;
}

function renderUpNext() {
  const strip = $('partyUpNext');
  if (!strip) return;
  const n = queueGames.length;
  let html = '';
  for (let k = 1; k <= Math.min(3, n - 1); k++) {
    const game = queueGames[(queueIndex + k) % n];
    html += `<div class="cover party-cover" title="${escapeHtml(game.name)}">${coverThumb(game)}</div>`;
  }
  strip.innerHTML = html || `<span class="description">${escapeHtml(t('party.only_one'))}</span>`;
}

function setPrimary(label) {
  const btn = $('partyPrimary');
  if (btn) btn.textContent = label;
}

function renderWinner() {
  const el = $('partyWinner');
  const game = queueGames[queueIndex];
  if (!el || !game) return;
  el.innerHTML = `<div class="hero-kicker">${escapeHtml(t('party.winner'))}</div><h3>${escapeHtml(game.name)}</h3>`;
  renderHub();
  renderUpNext();
  setPrimary(t('party.launch'));
}

function spinWheel() {
  if (spinning || spun || !queueGames.length) return;
  spinning = true;
  setPrimary(t('party.spinning'));
  const n = queueGames.length;
  const seg = 360 / n;
  // Pointer sits at the top; rotate so the winner segment center lands under it.
  const target = currentRotation + 360 * 5 + (((-(queueIndex + 0.5) * seg - currentRotation) % 360 + 360) % 360);
  currentRotation = target;
  const wheel = $('partyWheel');
  wheel.style.transition = `transform ${SPIN_MS}ms cubic-bezier(0.2, 0.8, 0.2, 1)`;
  // Force style recalc so the transition runs from the current angle.
  void wheel.offsetWidth;
  wheel.style.transform = `rotate(${target.toFixed(2)}deg)`;
  window.setTimeout(() => {
    spinning = false;
    spun = true;
    renderWinner();
  }, SPIN_MS + 60);
}

function launchWinner() {
  const game = queueGames[queueIndex];
  if (!game) return;
  launch(game, $('partyPrimary'));
}

async function nextRound() {
  if (spinning || isSetupView()) return;
  spinning = true;
  try {
    const result = await api('/api/v2/party/next', { method: 'POST' });
    queueIndex = Number(result.index) || 0;
    resolveQueueGames();
    spun = false;
    renderWheel();
  } catch (error) {
    notify(error.message);
  } finally {
    spinning = false;
  }
}

export { openParty, closeParty, partyOverlayOpen, partyGamepad };
