import { $, escapeHtml } from './util.js';
import { AppState, api, ensureProfiles, filteredGames, nativePickFile } from './state.js';
import { t } from './i18n.js';
import { closeBigBoxMenu } from './bigbox.js';
import { resetReaderFrame } from './reader.js';

let lastDialogTrigger = null;
const dialogTriggers = new WeakMap();
document.addEventListener('click', event => {
  const trigger = event.target.closest?.('button,summary');
  if (trigger) lastDialogTrigger = trigger;
}, true);
const dialogObserver = new MutationObserver(() => {
  document.querySelectorAll('dialog[open]').forEach(dialog => {
    if (!dialogTriggers.has(dialog)) dialogTriggers.set(dialog, lastDialogTrigger);
  });
});
dialogObserver.observe(document.body, {subtree:true, attributes:true, attributeFilter:['open']});

function openDialog(dialog, trigger = lastDialogTrigger || document.activeElement) {
  const opener = trigger instanceof HTMLElement ? trigger : null;
  if (opener) dialogTriggers.set(dialog, opener);
  if (!dialog.showModal) { dialog.setAttribute('open', ''); return; }
  dialog.showModal();
  const first = dialog.querySelector('button, input, select, textarea, [tabindex]:not([tabindex="-1"])');
  if (first) first.focus();
}
function closeDialog(dialog) {
  if (dialog.open && typeof dialog.close === 'function') dialog.close();
  else dialog.removeAttribute('open');
}

document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  const open = [...document.querySelectorAll('dialog[open]')].at(-1);
  if (!open) return;
  event.preventDefault();
  closeDialog(open);
});

let a11yHostsReady = false;
function ensureA11yDialogHosts() {
  if (a11yHostsReady) return;
  a11yHostsReady = true;
  const wrap = document.createElement('div');
  wrap.hidden = true;
  wrap.innerHTML = `
    <dialog id="a11yInputDialog" aria-modal="true" aria-labelledby="a11yInputTitle">
      <form id="a11yInputForm">
        <div class="dialog-head"><h2 id="a11yInputTitle"></h2><button type="button" id="a11yInputClose" aria-label="Close dialog">×</button></div>
        <div class="form-grid" style="padding:16px">
          <p class="wide description" id="a11yInputMessage" hidden></p>
          <label class="field wide"><span id="a11yInputLabel">Value</span><input id="a11yInputField" autocomplete="off"></label>
        </div>
        <div class="dialog-actions"><button type="button" id="a11yInputCancel">Cancel</button><button type="submit" class="primary" id="a11yInputOk">OK</button></div>
      </form>
    </dialog>
    <dialog id="a11yChoiceDialog" aria-modal="true" aria-labelledby="a11yChoiceTitle">
      <form id="a11yChoiceForm">
        <div class="dialog-head"><h2 id="a11yChoiceTitle"></h2><button type="button" id="a11yChoiceClose" aria-label="Close dialog">×</button></div>
        <div class="form-grid" style="padding:16px">
          <p class="wide description" id="a11yChoiceMessage" hidden></p>
          <label class="field wide"><span id="a11yChoiceLabel">Choice</span><select id="a11yChoiceSelect"></select></label>
        </div>
        <div class="dialog-actions"><button type="button" id="a11yChoiceCancel">Cancel</button><button type="submit" class="primary" id="a11yChoiceOk">Choose</button></div>
      </form>
    </dialog>
    <dialog id="a11yConfirmDialog" aria-modal="true" aria-labelledby="a11yConfirmTitle">
      <div class="dialog-head"><h2 id="a11yConfirmTitle"></h2><button type="button" id="a11yConfirmClose" aria-label="Close dialog">×</button></div>
      <div class="form-grid" style="padding:16px">
        <p class="wide description" id="a11yConfirmMessage" hidden></p>
        <p class="wide description" id="a11yConfirmTarget" hidden></p>
        <p class="wide description" id="a11yConfirmConsequence" hidden></p>
        <p class="wide description" id="a11yConfirmRetained" hidden></p>
        <p class="wide description" id="a11yConfirmRecovery" hidden></p>
      </div>
      <div class="dialog-actions"><button type="button" id="a11yConfirmCancel">Cancel</button><button type="button" class="primary" id="a11yConfirmOk">Confirm</button></div>
    </dialog>`;
  document.body.appendChild(wrap);
  document.querySelectorAll('#a11yInputDialog,#a11yChoiceDialog,#a11yConfirmDialog').forEach(dialog => {
    dialog.setAttribute('closedby', 'closerequest');
  });
}

function settleDialog(dialog, resolve, value) {
  if (dialog.dataset.a11ySettled) return;
  dialog.dataset.a11ySettled = '1';
  closeDialog(dialog);
  delete dialog.dataset.a11ySettled;
  resolve(value);
}

function promptInput({ title = 'OpenBox', message = '', label = 'Value', defaultValue = '' } = {}) {
  ensureA11yDialogHosts();
  const dialog = $('a11yInputDialog');
  const field = $('a11yInputField');
  $('a11yInputTitle').textContent = title;
  const messageEl = $('a11yInputMessage');
  if (message) {
    messageEl.textContent = message;
    messageEl.hidden = false;
  } else messageEl.hidden = true;
  $('a11yInputLabel').textContent = label;
  field.value = defaultValue;
  return new Promise(resolve => {
    const finish = value => settleDialog(dialog, resolve, value);
    $('a11yInputForm').onsubmit = event => { event.preventDefault(); finish(field.value); };
    $('a11yInputCancel').onclick = () => finish(null);
    $('a11yInputClose').onclick = () => finish(null);
    dialog.addEventListener('close', () => finish(null), {once:true});
    openDialog(dialog);
    field.focus();
    field.select();
  });
}

function promptChoice({ title = 'OpenBox', message = '', label = 'Choice', choices = [], defaultValue = '' } = {}) {
  ensureA11yDialogHosts();
  const dialog = $('a11yChoiceDialog');
  const select = $('a11yChoiceSelect');
  $('a11yChoiceTitle').textContent = title;
  const messageEl = $('a11yChoiceMessage');
  if (message) {
    messageEl.textContent = message;
    messageEl.hidden = false;
  } else messageEl.hidden = true;
  $('a11yChoiceLabel').textContent = label;
  select.innerHTML = choices.map(choice => {
    const value = choice.value ?? choice.label ?? choice;
    const text = choice.label ?? choice.value ?? choice;
    return `<option value="${escapeHtml(String(value))}">${escapeHtml(String(text))}</option>`;
  }).join('');
  if (defaultValue) select.value = String(defaultValue);
  return new Promise(resolve => {
    const finish = value => settleDialog(dialog, resolve, value);
    $('a11yChoiceForm').onsubmit = event => { event.preventDefault(); finish(select.value); };
    $('a11yChoiceCancel').onclick = () => finish(null);
    $('a11yChoiceClose').onclick = () => finish(null);
    dialog.addEventListener('close', () => finish(null), {once:true});
    openDialog(dialog);
    select.focus();
  });
}

function confirmAction({
  title = 'Confirm',
  message = '',
  target = '',
  consequence = '',
  retained = '',
  recovery = '',
  confirmLabel = 'Confirm',
  destructive = false,
} = {}) {
  ensureA11yDialogHosts();
  const dialog = $('a11yConfirmDialog');
  $('a11yConfirmTitle').textContent = title;
  const setLine = (id, prefix, text) => {
    const el = $(id);
    if (text) {
      el.textContent = prefix ? `${prefix}: ${text}` : text;
      el.hidden = false;
    } else el.hidden = true;
  };
  setLine('a11yConfirmMessage', '', message);
  setLine('a11yConfirmTarget', 'Target', target);
  setLine('a11yConfirmConsequence', 'Consequence', consequence);
  setLine('a11yConfirmRetained', 'Retained', retained);
  setLine('a11yConfirmRecovery', 'Recovery', recovery);
  const okBtn = $('a11yConfirmOk');
  okBtn.textContent = confirmLabel;
  okBtn.className = 'primary';
  return new Promise(resolve => {
    const finish = value => settleDialog(dialog, resolve, value);
    okBtn.onclick = () => finish(true);
    $('a11yConfirmCancel').onclick = () => finish(false);
    $('a11yConfirmClose').onclick = () => finish(false);
    dialog.addEventListener('close', () => finish(false), {once:true});
    openDialog(dialog);
    okBtn.focus();
  });
}

let contextMenuTrigger = null;
function contextMenuItems() {
  const menu = $('contextMenu');
  if (!menu) return [];
  return [...menu.querySelectorAll('[role="menuitem"]:not(:disabled)')];
}
function closeContextMenu(restoreFocus = false) {
  $('contextMenu').hidden = true;
  const gameId = AppState.contextGameId;
  AppState.contextGameId = null;
  if (restoreFocus) {
    let trigger = contextMenuTrigger;
    contextMenuTrigger = null;
    if (!trigger?.isConnected && gameId !== null && gameId !== undefined) {
      trigger = document.querySelector(`[data-game="${gameId}"]`);
    }
    try {
      if (trigger && typeof trigger.focus === 'function') {
        if (!trigger.hasAttribute('tabindex') && trigger.tabIndex === -1) trigger.tabIndex = 0;
        trigger.focus({ preventScroll: true });
        if (document.activeElement !== trigger) {
          const focusable = trigger.closest?.('[data-game]') || trigger;
          if (focusable && focusable !== trigger && typeof focusable.focus === 'function') {
            if (!focusable.hasAttribute('tabindex') && focusable.tabIndex === -1) focusable.tabIndex = 0;
            focusable.focus({ preventScroll: true });
          }
        }
      }
    } catch {}
  }
}
function openContextMenu(event, id) {
  event.preventDefault();
  AppState.contextGameId = id;
  AppState.selectedId = id;
  const menu = $('contextMenu');
  $('contextPlaylist').innerHTML = '<option value="">Add to playlist...</option>' + AppState.playlists.filter(item => item.type === 'manual').map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join('');
  menu.hidden = false;
  menu.style.left = `${Math.min(event.clientX, window.innerWidth - menu.offsetWidth - 8)}px`;
  menu.style.top = `${Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 8)}px`;
  contextMenuTrigger = event.target?.closest?.('[data-game]') || document.activeElement;
  const items = contextMenuItems();
  items.forEach((item, index) => item.tabIndex = index === 0 ? 0 : -1);
  items[0]?.focus();
}

function bindContextMenuA11y() {
  const menu = $('contextMenu');
  if (!menu) return;
  document.addEventListener('contextmenu', event => {
    const target = event.target.closest?.('[data-game]');
    if (target) openContextMenu(event, Number(target.dataset.game));
  });
  document.addEventListener('click', event => {
    if (!event.target.closest?.('#contextMenu')) closeContextMenu();
  });
  document.addEventListener('keydown', event => {
    const gameEl = document.activeElement?.closest?.('[data-game]');
    if (gameEl && (event.key === 'ContextMenu' || (event.key === 'F10' && event.shiftKey))) {
      event.preventDefault();
      const rect = gameEl.getBoundingClientRect();
      openContextMenu({preventDefault:()=>{}, clientX:rect.left + rect.width / 2, clientY:rect.top + rect.height / 2, target:gameEl}, Number(gameEl.dataset.game));
      return;
    }
    if (event.key !== 'Escape' || menu.hidden) return;
    if ([...document.querySelectorAll('dialog[open]')].length) return;
    event.preventDefault();
    closeContextMenu(true);
  });
  menu.addEventListener('keydown', event => {
    const items = contextMenuItems();
    const idx = items.indexOf(document.activeElement);
    if (event.key === 'ArrowDown') { event.preventDefault(); items[(idx + 1) % items.length]?.focus(); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); items[(idx - 1 + items.length) % items.length]?.focus(); }
    else if (event.key === 'Home') { event.preventDefault(); items[0]?.focus(); }
    else if (event.key === 'End') { event.preventDefault(); items[items.length - 1]?.focus(); }
    else if (event.key === 'Escape') { event.preventDefault(); closeContextMenu(true); }
    else if (event.key === 'Tab') closeContextMenu();
  });
}

let gameFormSnapshot = '';
function syncGameEntryType() {
  const form = $('gameForm');
  const entryType = form?.elements?.entry_type;
  if (!form || !entryType) return;
  const shelf = entryType.value === 'shelf';
  const path = form.elements.path;
  if (path) {
    path.required = !shelf;
    path.disabled = shelf;
  }
  const hint = $('gameShelfHint');
  if (hint) hint.hidden = !shelf;
  const launchNav = document.querySelector('.game-editor-nav-item[data-game-section="launch"]');
  if (launchNav) launchNav.hidden = shelf;
  const launchSection = document.querySelector('.game-editor-section[data-game-section="launch"]');
  if (shelf && launchSection && !launchSection.hidden) {
    document.querySelectorAll('.game-editor-nav-item').forEach(item => item.classList.toggle('active', item.dataset.gameSection === 'basics'));
    document.querySelectorAll('.game-editor-section').forEach(panel => { panel.hidden = panel.dataset.gameSection !== 'basics'; });
  }
}

function snapshotGameForm() {
  const form = $('gameForm');
  if (!form) return '';
  return [...new FormData(form).entries()].map(([key, value]) => `${key}=${value}`).join('&');
}
function gameFormDirty() {
  return $('gameDialog')?.open && gameFormSnapshot !== snapshotGameForm();
}
async function guardUnsavedGameEditor(action) {
  if (!gameFormDirty()) {
    action();
    return;
  }
  const ok = await confirmAction({
    title: 'Discard unsaved changes?',
    target: $('dialogTitle')?.textContent?.trim() || 'Game editor',
    consequence: 'Unsaved edits in this dialog will be lost.',
    retained: 'Saved library data stays unchanged.',
    recovery: 'Re-open the game editor to edit again.',
    confirmLabel: 'Discard changes',
    destructive: true,
  });
  if (ok) {
    gameFormSnapshot = snapshotGameForm();
    action();
  }
}
async function guardUnsavedAndCloseGameDialog() {
  if (!gameFormDirty()) {
    closeDialog($('gameDialog'));
    return;
  }
  const ok = await confirmAction({
    title: 'Discard unsaved changes?',
    target: $('dialogTitle')?.textContent?.trim() || 'Game editor',
    consequence: 'Unsaved edits in this dialog will be lost.',
    retained: 'Saved library data stays unchanged.',
    recovery: 'Re-open the game editor to edit again.',
    confirmLabel: 'Discard changes',
    destructive: true,
  });
  if (ok) closeDialog($('gameDialog'));
}

function bindGameEditorUnsavedGuard() {
  $('closeDialog').onclick = () => guardUnsavedAndCloseGameDialog();
  $('cancelDialog').onclick = () => guardUnsavedAndCloseGameDialog();
  document.querySelectorAll('.game-editor-nav-item').forEach(button => {
    button.addEventListener('click', event => {
      if (!gameFormDirty()) return;
      event.stopImmediatePropagation();
      event.preventDefault();
      guardUnsavedGameEditor(() => {
        document.querySelectorAll('.game-editor-nav-item').forEach(item => item.classList.toggle('active', item === button));
        const target = button.dataset.gameSection;
        document.querySelectorAll('.game-editor-section').forEach(panel => {
          panel.hidden = panel.dataset.gameSection !== target;
        });
      });
    }, true);
  });
  const gameDialog = $('gameDialog');
  if (gameDialog) {
    gameDialog.addEventListener('close', () => { gameFormSnapshot = ''; }, true);
    gameDialog.addEventListener('cancel', event => {
      if (!gameFormDirty()) return;
      event.preventDefault();
      guardUnsavedAndCloseGameDialog();
    });
  }
}

async function bindGameEditorBrowse() {
  const { nativePickFile, nativePickFolder } = await import('./state.js');
  const dialog = $('gameDialog');
  if (!dialog) return;
  dialog.querySelectorAll('button.path-browse').forEach(button => {
    button.addEventListener('click', async () => {
      const fieldName = button.dataset.browseFor;
      const kind = button.dataset.browseKind || 'file';
      const field = dialog.querySelector(`[name="${fieldName}"]`);
      if (!field) return;
      const title = `Choose ${fieldName.replace(/_/g, ' ')}`;
      const path = kind === 'folder' ? await nativePickFolder(title) : await nativePickFile(title);
      if (!path) return;
      if (kind === 'multi') {
        const current = field.value.trim();
        field.value = current ? `${current}\n${path}` : path;
      } else field.value = path;
    });
  });
}

document.addEventListener('mousedown', event => {
  if (document.activeElement?.tagName === 'SELECT') return;
  if (event.target.closest?.('select, option')) return;
  document.querySelectorAll('dialog[open]').forEach(dialog => {
    if (dialog.id === 'gameDialog' && gameFormDirty()) return;
    if (dialog.contains(event.target)) return;
    const rect = dialog.getBoundingClientRect();
    const inside = event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom;
    if (!inside) dialog.close();
  });
  [
    [$('bigBoxMenu'), '.bigbox-menu-panel', closeBigBoxMenu],
    [$('bigBoxPause'), '.bigbox-pause-panel', () => { $('bigBoxPause').hidden = true; }],
  ].forEach(([overlay, panelSelector, close]) => {
    if (!overlay || overlay.hidden) return;
    if (event.target.closest(panelSelector)) return;
    if (overlay.contains(event.target)) close();
  });
});
document.querySelectorAll('dialog').forEach(dialog => {
  dialog.setAttribute('closedby', 'closerequest');
  const heading = dialog.querySelector('h2');
  if (heading) {
    if (!heading.id) heading.id = `${dialog.id}Title`;
    dialog.setAttribute('aria-labelledby', heading.id);
  }
  dialog.addEventListener('close', () => {
    if (dialog.id === 'readerDialog') {
      resetReaderFrame();
    }
    if (dialog.id === 'mediaDialog') $('fullScreenshot')?.remove();
    const trigger = dialogTriggers.get(dialog);
    dialogTriggers.delete(dialog);
    if (trigger?.isConnected) trigger.focus();
  });
});

async function openGameDialog(game = null, options = {}) {
  await ensureProfiles();
  AppState.editingId = game ? game.id : null;
  const shelf = options.shelf ?? Boolean(game?.manual_entry);
  $('dialogTitle').textContent = game ? (shelf ? 'Edit shelf entry' : 'Edit game') : (shelf ? 'Add shelf entry' : 'Add game');
  [...$('gameForm').elements].forEach(element => {
    if (!element.name) return;
    if (element.type === 'checkbox') element.checked = Boolean(game?.[element.name]);
    else if (element.name === 'applications') element.value = (game?.applications || []).map(item => [item.name,item.path,item.command].filter(Boolean).join(' | ')).join('\n');
    else if (element.name === 'versions') element.value = (game?.versions || []).map(item => [item.name,item.path,item.command].filter(Boolean).join(' | ')).join('\n');
    else if (element.name === 'documents') element.value = (game?.documents || []).map(item => [item.name,item.path].join(' | ')).join('\n');
    else if (element.name === 'save_paths') element.value = (game?.save_paths || []).join('\n');
    else if (element.name === 'screenshots') element.value = (game?.screenshots || []).join('\n');
    else if (element.name === 'alternate_names') element.value = Array.isArray(game?.alternate_names) ? game.alternate_names.join('; ') : (game?.alternate_names || '');
    else element.value = game?.[element.name] || '';
  });
  const entryType = $('gameForm').elements.entry_type;
  if (entryType) {
    entryType.value = shelf ? 'shelf' : 'playable';
    // Editing an existing record keeps its identity and entry kind. Use the
    // explicit shelf conversion action for supported transitions so the save
    // endpoint cannot be selected from a stale form value.
    entryType.disabled = Boolean(game);
    entryType.onchange = syncGameEntryType;
  }
  const container = $('customFieldInputs');
  const defs = AppState.appSettings.custom_field_defs || [];
  container.innerHTML = defs.length ? defs.map(def => {
    const value = game?.custom_fields?.[def.name] || '';
    if (def.options?.length) {
      return `<label class="field"><span>${escapeHtml(def.name)}</span><select data-custom-field="${escapeHtml(def.name)}">${['', ...def.options].map(option => `<option value="${escapeHtml(option)}" ${option === value ? 'selected' : ''}>${escapeHtml(option || 'Not set')}</option>`).join('')}</select></label>`;
    }
    return `<label class="field"><span>${escapeHtml(def.name)}</span><input data-custom-field="${escapeHtml(def.name)}" value="${escapeHtml(value)}"></label>`;
  }).join('') : '';
  const profileSelect = $('gameForm').elements.launch_profile;
  const platformName = $('gameForm').elements.platform.value.trim();
  if (profileSelect) {
    const currentProfile = game?.launch_profile || '';
    profileSelect.innerHTML = '<option value="">Default platform profile</option>' + Object.keys(AppState.availableProfiles).filter(name => !platformName || name === platformName).map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
    profileSelect.value = currentProfile;
  }
  const gamescopeSelect = $('gameForm').elements.gamescope_preset;
  if (gamescopeSelect) {
    const currentPreset = game?.gamescope_preset || '';
    gamescopeSelect.innerHTML = '<option value="">Global setting</option>' + (AppState.appSettings.gamescope_presets || []).map(([name, label]) => `<option value="${escapeHtml(name)}">${escapeHtml(label)}</option>`).join('');
    gamescopeSelect.value = currentPreset;
  }
  const visible = typeof filteredGames === 'function' ? filteredGames() : (AppState.games || []);
  const currentIndex = game ? visible.findIndex(g => g.id === game.id) : -1;
  const prevBtn = $('prevGameDialog');
  const nextBtn = $('nextGameDialog');
  if (prevBtn) {
    prevBtn.disabled = !game || currentIndex <= 0;
    prevBtn.onclick = (game && currentIndex > 0) ? () => guardUnsavedGameEditor(() => openGameDialog(visible[currentIndex - 1])) : null;
  }
  if (nextBtn) {
    nextBtn.disabled = !game || currentIndex === -1 || currentIndex >= visible.length - 1;
    nextBtn.onclick = (game && currentIndex !== -1 && currentIndex < visible.length - 1) ? () => guardUnsavedGameEditor(() => openGameDialog(visible[currentIndex + 1])) : null;
  }
  syncGameEntryType();
  gameFormSnapshot = snapshotGameForm();
  openDialog($('gameDialog'));
}

async function convertShelfEntry(game) {
  if (!game?.manual_entry) return false;
  const path = await nativePickFile('Choose a game or ROM file');
  if (!path) return false;
  await api('/api/v2/library/manual-entry/convert', {
    method: 'POST',
    body: JSON.stringify({game_id: game.game_id || game.id, path}),
  });
  return true;
}

bindGameEditorUnsavedGuard();
bindGameEditorBrowse();

$('closeProfiles').onclick = $('cancelProfiles').onclick = () => closeDialog($('profilesDialog'));
$('closeThemes').onclick = $('cancelThemes').onclick = () => closeDialog($('themesDialog'));
$('closeAchievements').onclick = $('cancelAchievements').onclick = () => closeDialog($('achievementsDialog'));
$('closePlugins').onclick = $('donePlugins').onclick = () => closeDialog($('pluginsDialog'));
$('closeMetadata').onclick = $('doneMetadata').onclick = () => closeDialog($('metadataDialog'));
$('closeMediaManager').onclick = $('doneMediaManager').onclick = () => closeDialog($('mediaManagerDialog'));
$('closeHealth').onclick = $('doneHealth').onclick = () => closeDialog($('healthDialog'));
$('closeBulk').onclick = $('cancelBulk').onclick = () => closeDialog($('bulkDialog'));
$('closeSessions').onclick = $('doneSessions').onclick = () => closeDialog($('sessionsDialog'));
$('closeHistory').onclick = $('doneHistory').onclick = () => closeDialog($('historyDialog'));

// ── Trophy case (S6): launcher-level achievements ────────────────────────────
// The dialog DOM is built lazily here so index.html stays untouched. The case
// opens from the award toast, the exported openTrophyCase(), or the
// app:open-trophy-case document event for other surfaces (tools menu, tabs).
let trophyCaseReady = false;
function ensureTrophyCaseHost() {
  if (trophyCaseReady) return;
  trophyCaseReady = true;
  const wrap = document.createElement('div');
  wrap.hidden = true;
  wrap.innerHTML = `
    <dialog id="trophyCaseDialog" aria-modal="true" aria-labelledby="trophyCaseTitle">
      <div class="dialog-head"><h2 id="trophyCaseTitle"></h2><button type="button" id="trophyCaseClose" aria-label="Close">×</button></div>
      <p class="trophy-case-count" id="trophyCaseCount"></p>
      <div class="trophy-case-body" id="trophyCaseBody"></div>
    </dialog>`;
  document.body.appendChild(wrap);
  const dialog = $('trophyCaseDialog');
  dialog.setAttribute('closedby', 'closerequest');
  $('trophyCaseClose').onclick = () => closeDialog(dialog);
}

function trophyCardHtml(item) {
  const current = item.progress?.current ?? 0;
  const target = item.progress?.target ?? 0;
  const pct = target > 0 ? Math.min(100, Math.round(100 * current / target)) : 0;
  const meta = item.awarded
    ? (item.awarded_at ? t('trophy.earned_on', {date: String(item.awarded_at).slice(0, 10)}) : t('trophy.earned'))
    : t('trophy.progress', {current, target});
  return `<div class="trophy-card ${item.awarded ? 'is-earned' : 'is-locked'}">
    <span class="trophy-card-icon" aria-hidden="true"></span>
    <div class="trophy-card-text">
      <strong>${escapeHtml(t(`trophy.rules.${item.id}.name`))}</strong>
      <small>${escapeHtml(t(`trophy.rules.${item.id}.desc`))}</small>
      <div class="trophy-progress"><div class="trophy-progress-fill" style="width:${pct}%"></div></div>
      <small class="trophy-card-meta">${escapeHtml(meta)}</small>
    </div>
  </div>`;
}

function renderTrophyCase(payload) {
  const body = $('trophyCaseBody');
  if (!body) return;
  const trophies = payload?.trophies || [];
  $('trophyCaseTitle').textContent = t('trophy.case_title');
  $('trophyCaseClose').setAttribute('aria-label', t('trophy.close'));
  $('trophyCaseCount').textContent = t('trophy.count', {earned: payload?.earned ?? 0, total: payload?.total ?? trophies.length});
  const earned = trophies.filter(item => item.awarded);
  const locked = trophies.filter(item => !item.awarded);
  if (!earned.length && !locked.length) {
    body.innerHTML = `<p class="trophy-empty">${escapeHtml(t('trophy.empty'))}</p>`;
    return;
  }
  body.innerHTML =
    (earned.length ? `<h3 class="trophy-case-head">${escapeHtml(t('trophy.earned'))}</h3><div class="trophy-grid">${earned.map(trophyCardHtml).join('')}</div>` : '') +
    (locked.length ? `<h3 class="trophy-case-head">${escapeHtml(t('trophy.locked'))}</h3><div class="trophy-grid">${locked.map(trophyCardHtml).join('')}</div>` : '');
}

async function openTrophyCase() {
  ensureTrophyCaseHost();
  const dialog = $('trophyCaseDialog');
  if (!dialog.open) openDialog(dialog);
  try {
    renderTrophyCase(await api('/api/v2/insights/trophies'));
  } catch (error) {
    $('trophyCaseBody').innerHTML = `<p class="trophy-empty">${escapeHtml(error?.message || String(error))}</p>`;
  }
}

function showTrophyToast(awards) {
  const toast = $('toast');
  if (!toast || !awards?.length) return;
  const name = t(`trophy.rules.${awards[0].id}.name`);
  const text = awards.length > 1 ? t('trophy.unlocked_more', {name, count: awards.length - 1}) : t('trophy.unlocked', {name});
  toast.innerHTML = `<span class="trophy-toast-text">${escapeHtml(text)}</span><button type="button" class="trophy-view" id="trophyViewCase">${escapeHtml(t('trophy.view_case'))}</button>`;
  toast.dataset.notifyLevel = 'success';
  toast.classList.add('show');
  $('trophyViewCase').onclick = () => { toast.classList.remove('show'); openTrophyCase(); };
  clearTimeout(showTrophyToast.timer);
  showTrophyToast.timer = setTimeout(() => toast.classList.remove('show'), 4000);
}

let trophyCheckTimer = 0;
let trophyCheckBusy = false;
async function checkTrophies() {
  if (trophyCheckBusy) return;
  trophyCheckBusy = true;
  try {
    const result = await api('/api/v2/insights/trophies/evaluate', {method: 'POST', body: '{}'});
    showTrophyToast(result.newly_awarded);
  } catch { /* trophies are best-effort; never block the refresh bus */ }
  finally { trophyCheckBusy = false; }
}
function scheduleTrophyCheck() {
  if (trophyCheckTimer) clearTimeout(trophyCheckTimer);
  trophyCheckTimer = setTimeout(() => { trophyCheckTimer = 0; checkTrophies(); }, 600);
}
document.addEventListener('app:state-refreshed', scheduleTrophyCheck);
document.addEventListener('app:open-trophy-case', () => { openTrophyCase(); });
document.addEventListener('localechange', () => {
  if ($('trophyCaseDialog')?.open) api('/api/v2/insights/trophies').then(renderTrophyCase).catch(() => {});
});

export {
  openDialog,
  closeDialog,
  openGameDialog,
  convertShelfEntry,
  openContextMenu,
  closeContextMenu,
  bindContextMenuA11y,
  promptInput,
  promptChoice,
  confirmAction,
  openTrophyCase,
};
