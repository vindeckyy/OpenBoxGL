import { $, escapeHtml } from './util.js';
import { api, notify, AppState } from './state.js';
import { refresh, render, renderDetails } from './library.js';
import { filteredBigBoxGames, renderBigBox } from './bigbox.js';



    async function openDiscovery() {
      try {
        const result = await api('/api/discovery');
        const labels = {recently_added:'Recently added',never_played:'Never played',continue_playing:'Continue playing',highly_rated:'Highly rated',random_picks:'Random picks',short_sessions:'Short sessions'};
        $('discoveryContent').innerHTML = Object.entries(labels).map(([key,label]) => {
          const ids = result[key] || [];
          const items = ids.map(id => AppState.games.find(game => game.id === id)).filter(Boolean);
          return `<div class="discovery-section"><h3>${label}</h3><div class="discovery-row">${items.length ? items.map(game => `<button class="related-game" data-discovery="${game.id}">${escapeHtml(game.name)}</button>`).join('') : '<span class="description">No matches yet.</span>'}</div></div>`;
        }).join('');
        document.querySelectorAll('[data-discovery]').forEach(button => button.onclick = () => { AppState.selectedId = Number(button.dataset.discovery); $('discoveryDialog').close(); render(); });
        $('discoveryDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    async function loadStorefrontCatalog() {
      const source = $('storefrontSource').value;
      try {
        const result = await api(`/api/storefront/catalog?source=${encodeURIComponent(source)}`);
        $('storefrontCatalog').innerHTML = result.catalog.length ? result.catalog.slice(0, 200).map(item => `<div class="storefront-row"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.source)} · ${item.installed ? 'Installed' : 'Not installed'}</small></div><span>${escapeHtml(item.id)}</span></div>`).join('') : '<p class="description">No storefront entries were found.</p>';
      } catch(error) { notify(error.message); }
    }
    async function importStorefrontCatalog(uninstalledOnly) {
      const source = $('storefrontSource').value;
      try {
        const result = await api('/api/storefront/import',{method:'POST',body:JSON.stringify({source,uninstalled_only:uninstalledOnly,installed_only:!uninstalledOnly})});
        await refresh();
        notify(`${result.added} ${uninstalledOnly ? 'uninstalled catalog' : 'installed'} entr${result.added === 1 ? 'y' : 'ies'} added`);
      } catch(error) { notify(error.message); }
    }
    function openStorefronts() {
      const settings = AppState.appSettings.storefront_auto_import || {};
      $('storefrontAutoImportSteam').checked = Boolean(settings.steam);
      $('storefrontAutoImportHeroic').checked = Boolean(settings.heroic);
      $('storefrontAutoImportLutris').checked = Boolean(settings.lutris);
      if ($('storefrontAutoImportGameyfin')) $('storefrontAutoImportGameyfin').checked = Boolean(settings.gameyfin);
      if ($('storefrontGameyfinUrl')) $('storefrontGameyfinUrl').value = AppState.appSettings.gameyfin_url || '';
      if ($('storefrontGameyfinUsername')) $('storefrontGameyfinUsername').value = AppState.appSettings.gameyfin_username || '';
      if ($('storefrontGameyfinPassword')) $('storefrontGameyfinPassword').value = '';
      if ($('storefrontGameyfinInstallDir')) $('storefrontGameyfinInstallDir').value = AppState.appSettings.gameyfin_install_dir || '';
      if ($('storefrontGameyfinStatus')) $('storefrontGameyfinStatus').textContent = AppState.appSettings.gameyfin_url ? `Configured · ${AppState.appSettings.gameyfin_url}` : '';
      $('storefrontCatalog').innerHTML = '';
      $('storefrontDialog').showModal();
    }
    async function saveStorefrontSettings() {
      AppState.appSettings = await api('/api/settings',{method:'POST',body:JSON.stringify(collectStorefrontSettings())});
      notify('Storefront settings saved');
    }
    function collectStorefrontSettings() {
      return {
        storefront_auto_import:{
          steam:$('storefrontAutoImportSteam').checked,
          heroic:$('storefrontAutoImportHeroic').checked,
          lutris:$('storefrontAutoImportLutris').checked,
          gameyfin:Boolean($('storefrontAutoImportGameyfin')?.checked),
        },
        gameyfin_url:$('storefrontGameyfinUrl')?.value.trim() || '',
        gameyfin_username:$('storefrontGameyfinUsername')?.value.trim() || '',
        gameyfin_password:$('storefrontGameyfinPassword')?.value || '',
        gameyfin_install_dir:$('storefrontGameyfinInstallDir')?.value.trim() || '',
      };
    }
    async function watchGameyfinInstall(gameyfinId, attempts = 1200) {
      const job = await api(`/api/gameyfin/install/status?gameyfin_id=${encodeURIComponent(gameyfinId)}`);
      if (job.state === 'installing') {
        if (attempts <= 0) throw new Error('Gameyfin install timed out');
        await new Promise(resolve => setTimeout(resolve, 1500));
        return watchGameyfinInstall(gameyfinId, attempts - 1);
      }
      if (job.state === 'error') throw new Error(job.error || 'Gameyfin install failed');
      if (job.state !== 'done') throw new Error('Gameyfin install did not complete');
    }
    async function installGameyfin(game) {
      try {
        notify(`Installing ${game.name} from Gameyfin...`);
        await api('/api/gameyfin/install',{method:'POST',body:JSON.stringify({gameyfin_id:game.gameyfin_id,library_id:game.id})});
        await watchGameyfinInstall(game.gameyfin_id);
        await refresh();
        AppState.bigBoxGames = filteredBigBoxGames();
        if (!$('bigBox').hidden) renderBigBox();
        else renderDetails();
        notify(`${game.name} installed`);
      } catch(error) { notify(error.message); }
    }
    async function uninstallGameyfin(game) {
      const { confirmAction } = await import('./dialogs.js');
      const ok = await confirmAction({
        title: 'Uninstall Gameyfin game',
        message: `Remove the local Gameyfin install for ${game.name}?`,
        consequence: 'The server copy stays intact.',
      });
      if (!ok) return;
      try {
        await api('/api/gameyfin/uninstall',{method:'POST',body:JSON.stringify({id:game.id})});
        await refresh();
        AppState.bigBoxGames = filteredBigBoxGames();
        if (!$('bigBox').hidden) renderBigBox();
        else renderDetails();
        notify(`${game.name} uninstalled locally`);
      } catch(error) { notify(error.message); }
    }
    async function ludusaviAction(id, action) {
      try {
        const result = await api('/api/save-tools/ludusavi',{method:'POST',body:JSON.stringify({id,action})});
        notify(`Ludusavi ${action} ${result.ok ? 'finished' : 'reported issues'}`);
      } catch(error) { notify(error.message); }
    }
    async function hoardAction(id, action) {
      try {
        await api('/api/save-tools/hoard',{method:'POST',body:JSON.stringify({id,action})});
        notify(`Hoard ${action} finished`);
      } catch(error) { notify(error.message); }
    }

export { openDiscovery, loadStorefrontCatalog, importStorefrontCatalog, openStorefronts, saveStorefrontSettings, collectStorefrontSettings, watchGameyfinInstall, installGameyfin, uninstallGameyfin, ludusaviAction, hoardAction };
