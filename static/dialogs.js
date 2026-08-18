import { $, escapeHtml } from './util.js';
import { AppState, ensureProfiles, filteredGames } from './state.js';
import { closeBigBoxMenu } from './bigbox.js';



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
      // Focus the first focusable control so keyboard users land somewhere
      // useful, not on the inert backdrop.
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
    $('closeDialog').onclick = $('cancelDialog').onclick = () => closeDialog($('gameDialog'));
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
    async function openGameDialog(game = null) {
      await ensureProfiles();
      AppState.editingId = game ? game.id : null;
      $('dialogTitle').textContent = game ? 'Edit game' : 'Add game';
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
      const visible = typeof filteredGames === 'function' ? filteredGames() : (AppState.games || []);
      const currentIndex = game ? visible.findIndex(g => g.id === game.id) : -1;
      const prevBtn = $('prevGameDialog');
      const nextBtn = $('nextGameDialog');
      if (prevBtn) {
        prevBtn.disabled = !game || currentIndex <= 0;
        prevBtn.onclick = (game && currentIndex > 0) ? () => openGameDialog(visible[currentIndex - 1]) : null;
      }
      if (nextBtn) {
        nextBtn.disabled = !game || currentIndex === -1 || currentIndex >= visible.length - 1;
        nextBtn.onclick = (game && currentIndex !== -1 && currentIndex < visible.length - 1) ? () => openGameDialog(visible[currentIndex + 1]) : null;
      }
      openDialog($('gameDialog'));
    }
    function closeContextMenu() { $('contextMenu').hidden = true; AppState.contextGameId = null; }
    function openContextMenu(event, id) {
      event.preventDefault();
      AppState.contextGameId = id;
      AppState.selectedId = id;
      const menu = $('contextMenu');
      $('contextPlaylist').innerHTML = '<option value="">Add to playlist...</option>' + AppState.playlists.filter(item => item.type === 'manual').map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join('');
      menu.hidden = false;
      menu.style.left = `${Math.min(event.clientX, window.innerWidth - menu.offsetWidth - 8)}px`;
      menu.style.top = `${Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 8)}px`;
    }
    document.addEventListener('mousedown', event => {
      // Native <select> menus paint outside the dialog rectangle; ignore those clicks.
      if (document.activeElement?.tagName === 'SELECT') return;
      if (event.target.closest?.('select, option')) return;
      document.querySelectorAll('dialog[open]').forEach(dialog => {
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
          $('readerFrame').removeAttribute('src');
          AppState.readerUrl = '';
          AppState.readerPage = 1;
        }
        if (dialog.id === 'mediaDialog') $('fullScreenshot')?.remove();
        const trigger = dialogTriggers.get(dialog);
        dialogTriggers.delete(dialog);
        if (trigger?.isConnected) trigger.focus();
      });
    });

export { openDialog, closeDialog, openGameDialog, openContextMenu, closeContextMenu };
