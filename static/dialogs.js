import { $, escapeHtml, motionMs } from './util.js';
import { AppState, api, ensureProfiles, filteredGames, nativePickFile, showToast, raiseToasts } from './state.js';
import { t } from './i18n.js';
import { closeBigBoxMenu } from './bigbox.js';
import { renderGrid, renderDetails } from './library.js';
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

// ADR 0063: one choke point gives every dialog an exit animation and the shared focus wiring, without
// sweeping every raw .close()/.showModal() call site. close() plays .closing for --dur-out, then closes;
// with reduced motion the token is ~0 and it closes at once. Callers that need "closed" must listen for
// the 'close' event, which is what closeDialog() does below.
const nativeShowModal = HTMLDialogElement.prototype.showModal;
const nativeClose = HTMLDialogElement.prototype.close;
HTMLDialogElement.prototype.showModal = function () {
  wireDialogFocus(this);
  if (this.classList.contains('closing')) { this.classList.remove('closing'); return; } // reopened mid-exit
  const result = nativeShowModal.call(this);
  // B5: a native <dialog> is itself top layer, ordered by when it was shown, so
  // a toast already on screen ends up underneath it and ::backdrop covers the
  // Undo button for the rest of its life. Re-showing moves the toast container
  // to the top.
  raiseToasts();
  return result;
};
HTMLDialogElement.prototype.close = function (returnValue) {
  if (!this.open || this.classList.contains('closing')) return;
  const ms = motionMs('--dur-out');
  if (ms <= 1) { nativeClose.call(this, returnValue); return; }
  this.classList.add('closing');
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    this.removeEventListener('animationend', onEnd);
    if (!this.classList.contains('closing')) return; // showModal() cancelled the exit
    this.classList.remove('closing');
    if (this.open) nativeClose.call(this, returnValue);
  };
  const onEnd = event => { if (event.target === this) finish(); };
  this.addEventListener('animationend', onEnd);
  setTimeout(finish, ms + 60);
};

// Focus the dialog's *primary* control, not the first control in the DOM.
//
// `querySelector('button, input, ...')` returns document order, and every dialog
// in this app leads with the header's "×" close button -- so opening a dialog
// put the caret on "close this", which is the one action the user did not ask
// for. Three signals, in order of how explicit they are:
//   1. [data-dialog-primary]  -- a dialog states its own primary action
//   2. .primary               -- the existing primary-button class
//   3. autofocus              -- the native attribute, for hand-written markup
// and only then, as a last resort, the first control that is not a close button.
function primaryControl(dialog) {
  const explicit = dialog.querySelector('[data-dialog-primary]');
  if (explicit) return explicit;
  const primary = dialog.querySelector('.primary');
  if (primary) return primary;
  const native = dialog.querySelector('[autofocus]');
  if (native) return native;
  return dialog.querySelector(
    'button:not([data-dialog-close]):not(.icon-button), input, select, textarea, [tabindex]:not([tabindex="-1"])'
  );
}

function openDialog(dialog, trigger = lastDialogTrigger || document.activeElement) {
  const opener = trigger instanceof HTMLElement ? trigger : null;
  if (opener) dialogTriggers.set(dialog, opener);
  if (!dialog.showModal) { dialog.setAttribute('open', ''); return; }
  dialog.showModal();
  const first = primaryControl(dialog);
  if (first) first.focus();
}
function closeDialog(dialog) {
  // Every dialog close restores focus to the element that opened it, so no
  // caller needs its own focus dance (and competing Escape handlers can't
  // blur the restored target afterwards — this runs inside the consuming
  // handler, before later listeners fire).
  const trigger = dialogTriggers.get(dialog);
  const restore = () => {
    try {
      if (trigger?.isConnected && typeof trigger.focus === 'function') trigger.focus({ preventScroll: true });
    } catch {}
  };
  if (dialog.open && typeof dialog.close === 'function') {
    // The exit animation delays the real close; focus can only return once the modal is gone.
    dialog.addEventListener('close', restore, { once: true });
    dialog.close();
  } else {
    dialog.removeAttribute('open');
    restore();
  }
}

// B1: the two ways a browser closes a modal <dialog> without calling close().
//
// The override above only intercepts the JS-visible close() method. Per the HTML
// spec, Escape and a click on ::backdrop both go through the *close watcher*,
// which fires a cancelable `cancel` event and then removes the `open`
// attribute directly -- the JS-visible method is never involved. So for those
// two paths the exit animation never played and the focus restore in
// closeDialog() never ran, dropping focus to <body>. 1.15.0 fixed 8 dialogs by
// hand and left the other 21, which is what "every dialog now plays an exit"
// turned out to mean.
//
// One delegated listener instead of 21 per-dialog handlers.
//
// This listens in the bubble phase, not the capture phase, on purpose: a dialog
// that guards its own close (the game editor's unsaved-changes prompt, the
// setup centre's dismissed flag) has already had its turn, and honouring
// defaultPrevented is what keeps the delegated path from closing behind its
// back.
document.addEventListener('cancel', event => {
  const dialog = event.target;
  if (!(dialog instanceof HTMLDialogElement) || !dialog.open) return;
  if (event.defaultPrevented) return; // the dialog decided; leave its verdict alone
  event.preventDefault();            // abort the browser's own close
  closeDialog(dialog);               // re-enter the exit animation and focus restore
});

// A click on ::backdrop retargets to the dialog element itself, and the spec
// only treats it as a close request when the click landed outside the dialog's
// border box -- a click on the dialog's own padding must not close it.
document.addEventListener('click', event => {
  const dialog = event.target;
  if (event.target !== dialog || !(dialog instanceof HTMLDialogElement) || !dialog.open) return;
  const rect = dialog.getBoundingClientRect();
  const outside = event.clientX < rect.left || event.clientX > rect.right
    || event.clientY < rect.top || event.clientY > rect.bottom;
  if (!outside) return;
  closeDialog(dialog);
});

let a11yHostsReady = false;
function ensureA11yDialogHosts() {
  if (a11yHostsReady) return;
  a11yHostsReady = true;
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <dialog id="a11yInputDialog" aria-modal="true" aria-labelledby="a11yInputTitle">
      <form id="a11yInputForm">
        <div class="dialog-head"><h2 id="a11yInputTitle"></h2><button type="button" id="a11yInputClose" aria-label="Close dialog">×</button></div>
        <div class="form-grid" style="padding:var(--space-md)">

          <p class="wide description" id="a11yInputMessage" hidden></p>
          <label class="field wide"><span id="a11yInputLabel">Value</span><input id="a11yInputField" autocomplete="off"></label>
        </div>
        <div class="dialog-actions"><button type="button" id="a11yInputCancel">Cancel</button><button type="submit" class="primary" id="a11yInputOk">OK</button></div>
      </form>
    </dialog>
    <dialog id="a11yChoiceDialog" aria-modal="true" aria-labelledby="a11yChoiceTitle">
      <form id="a11yChoiceForm">
        <div class="dialog-head"><h2 id="a11yChoiceTitle"></h2><button type="button" id="a11yChoiceClose" aria-label="Close dialog">×</button></div>
        <div class="form-grid" style="padding:var(--space-md)">
          <p class="wide description" id="a11yChoiceMessage" hidden></p>
          <label class="field wide"><span id="a11yChoiceLabel">Choice</span><select id="a11yChoiceSelect"></select></label>
        </div>
        <div class="dialog-actions"><button type="button" id="a11yChoiceCancel">Cancel</button><button type="submit" class="primary" id="a11yChoiceOk">Choose</button></div>
      </form>
    </dialog>
    <dialog id="a11yConfirmDialog" aria-modal="true" aria-labelledby="a11yConfirmTitle">
        <div class="dialog-head"><h2 id="a11yConfirmTitle"></h2><button type="button" id="a11yConfirmClose" aria-label="Close dialog">×</button></div>
        <div class="form-grid" style="padding:var(--space-md)">
          <p class="wide description" id="a11yConfirmMessage" hidden></p>
        <p class="wide description" id="a11yConfirmTarget" hidden></p>
        <p class="wide description" id="a11yConfirmConsequence" hidden></p>
        <p class="wide description" id="a11yConfirmRetained" hidden></p>
        <p class="wide description" id="a11yConfirmRecovery" hidden></p>
      </div>
      <div class="dialog-actions"><button type="button" id="a11yConfirmCancel">Cancel</button><button type="button" class="primary" id="a11yConfirmOk">Confirm</button></div>
    </dialog>`;
  document.body.appendChild(wrap);
  // G-D1: lazy hosts get the same focus-restoration wiring as static dialogs.
  document.querySelectorAll('#a11yInputDialog,#a11yChoiceDialog,#a11yConfirmDialog').forEach(wireDialogFocus);
}

// Each prompt owns its own 'close' listener and removes it when it settles.
// close() can fire its 'close' event as a queued task (always when reduced
// motion makes the exit instant), so a prompt opened straight after another
// one settles -- one promptChoice per platform in the import wizard -- used to
// receive the previous prompt's stale event and cancel itself at once. A close
// event that arrives while the dialog is open again is not this prompt's.
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
    const finish = value => { dialog.removeEventListener('close', onClose); settleDialog(dialog, resolve, value); };
    const onClose = () => { if (!dialog.open) finish(null); };
    $('a11yInputForm').onsubmit = event => { event.preventDefault(); finish(field.value); };
    $('a11yInputCancel').onclick = () => finish(null);
    $('a11yInputClose').onclick = () => finish(null);
    dialog.addEventListener('close', onClose);
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
    const finish = value => { dialog.removeEventListener('close', onClose); settleDialog(dialog, resolve, value); };
    const onClose = () => { if (!dialog.open) finish(null); };
    $('a11yChoiceForm').onsubmit = event => { event.preventDefault(); finish(select.value); };
    $('a11yChoiceCancel').onclick = () => finish(null);
    $('a11yChoiceClose').onclick = () => finish(null);
    dialog.addEventListener('close', onClose);
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
    const finish = value => { dialog.removeEventListener('close', onClose); settleDialog(dialog, resolve, value); };
    const onClose = () => { if (!dialog.open) finish(false); };
    okBtn.onclick = () => finish(true);
    $('a11yConfirmCancel').onclick = () => finish(false);
    $('a11yConfirmClose').onclick = () => finish(false);
    dialog.addEventListener('close', onClose);
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
  // F14: this set selectedId without re-rendering, so the card the user
  // right-clicked was never highlighted and the details pane still described a
  // different game. The menu's own actions read contextGameId and were correct,
  // which is what made it confusing: the screen and the keyboard disagreed about
  // which game "this" was, and Enter then launched a game that was not on
  // screen. Same shape as closeBigBoxMenu below -- a live binding, resolved when
  // the function runs rather than at module evaluation.
  AppState.selectedId = id;
  renderGrid();
  renderDetails();
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
  // G-D3 (known limitation, 1.13.1): "topmost" is DOM order, not true
  // stacking order. This holds only because lazily created dialogs are
  // appended to document.body (hence last). A dialog inserted earlier in
  // the DOM, or inside a shadow root, would break the nesting assumption
  // silently — a real stacking-order comparator is 1.14 work.
  const topDialog = [...document.querySelectorAll('dialog[open]')].at(-1);
  if (topDialog) {
    if (!(topDialog.id === 'gameDialog' && gameFormDirty()) && !topDialog.contains(event.target)) {
      const rect = topDialog.getBoundingClientRect();
      const inside = event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom;
      if (!inside) {
        if (topDialog.id === 'setupCenter') AppState.setupDismissed = true;
        // G-D2: always route through closeDialog() so focus restoration
        // and the recorded-trigger contract match every other close path.
        closeDialog(topDialog);
      }
    }
  }
  [
    [$('bigBoxMenu'), '.bigbox-menu-panel', closeBigBoxMenu],
    [$('bigBoxPause'), '.bigbox-pause-panel', () => { $('bigBoxPause').hidden = true; }],
  ].forEach(([overlay, panelSelector, close]) => {
    if (!overlay || overlay.hidden) return;
    if (event.target.closest(panelSelector)) return;
    if (overlay.contains(event.target)) close();
  });
});
// G-D1: shared dialog wiring. The module-load block below wires every
// dialog present in index.html at load; lazily created hosts
// (ensureA11yDialogHosts / ensureTrophyCaseHost) call this too, so they get
// the same closedby/aria/focus-restoration wiring as static dialogs.
function wireDialogFocus(dialog) {
  if (dialog.dataset.focusWired) return;
  dialog.dataset.focusWired = '1';
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
    if (trigger?.isConnected) trigger.focus({ preventScroll: true });
  });
}
document.querySelectorAll('dialog').forEach(wireDialogFocus);

async function openGameDialog(game = null, options = {}) {
  await ensureProfiles();
  AppState.editingId = game ? game.id : null;
  // The stable id travels with the index so a save can refuse a game that
  // moved or vanished since the dialog opened (see game_from_payload).
  AppState.editingGameId = game ? String(game.game_id || '') : '';
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
    else if (element.name === 'launch_env') element.value = Object.entries(game?.launch_env || {}).map(([key, value]) => `${key}=${value}`).join('\n');
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
    prevBtn.hidden = !game;
    prevBtn.disabled = !game || currentIndex <= 0;
    prevBtn.onclick = (game && currentIndex > 0) ? () => guardUnsavedGameEditor(() => openGameDialog(visible[currentIndex - 1])) : null;
  }
  if (nextBtn) {
    nextBtn.hidden = !game;
    nextBtn.disabled = !game || currentIndex === -1 || currentIndex >= visible.length - 1;
    nextBtn.onclick = (game && currentIndex !== -1 && currentIndex < visible.length - 1) ? () => guardUnsavedGameEditor(() => openGameDialog(visible[currentIndex + 1])) : null;
  }
  const mediaToggle = $('mediaMoreToggle');
  const rareRows = [...$('gameDialog').querySelectorAll('.media-rare')];
  const expandRare = rareRows.some(row => row.querySelector('[name]')?.value.trim());
  rareRows.forEach(row => { row.hidden = !expandRare; });
  if (mediaToggle) {
    const setLabel = show => { mediaToggle.textContent = show ? t('dialog.media_show_fewer') : t('dialog.media_show_more', {count: rareRows.length}); mediaToggle.setAttribute('aria-expanded', String(show)); };
    setLabel(expandRare);
    mediaToggle.onclick = () => { const show = rareRows[0]?.hidden ?? true; rareRows.forEach(row => { row.hidden = !show; }); setLabel(show); };
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
  wrap.innerHTML = `
    <dialog id="trophyCaseDialog" aria-modal="true" aria-labelledby="trophyCaseTitle">
      <div class="dialog-head"><h2 id="trophyCaseTitle"></h2><button type="button" id="trophyCaseClose" aria-label="Close">×</button></div>
      <p class="trophy-case-count" id="trophyCaseCount"></p>
      <div class="trophy-case-body" id="trophyCaseBody"></div>
    </dialog>`;
  document.body.appendChild(wrap);
  const dialog = $('trophyCaseDialog');
  wireDialogFocus(dialog);
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
  if (!awards?.length) return;
  const name = t(`trophy.rules.${awards[0].id}.name`);
  const text = awards.length > 1 ? t('trophy.unlocked_more', {name, count: awards.length - 1}) : t('trophy.unlocked', {name});
  // Goes through the toast manager like every other writer. This used to assign
  // #toast.innerHTML, which is how a trophy unlock silently deleted a live
  // "moved to trash - Undo" button.
  showToast({
    level: 'success',
    text,
    action: t('trophy.view_case'),
    ms: 4000,
    onAction: () => openTrophyCase(),
  });
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
