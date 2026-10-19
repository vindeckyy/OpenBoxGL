import { $, escapeHtml } from './util.js';
import { api, notify, AppState } from './state.js';
import { refresh, render, renderDetails } from './library.js';
import { filteredBigBoxGames, renderBigBox } from './bigbox.js';
import { t } from './i18n.js';
import { renderPluginSettings } from './plugin_form.js';



    async function openDiscovery() {
      try {
        const result = await api('/api/discovery');
        const labels = {recently_added:'Recently added',never_played:'Never played',continue_playing:'Continue playing',highly_rated:'Highly rated',achievements:'Games with achievements',random_picks:'Random picks',short_sessions:'Short sessions', ...(result.discovery_names || {})};
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
        const result = await api('/api/storefront/import',{method:'POST',body:JSON.stringify({source,uninstalled_only:uninstalledOnly,installed_only:!uninstalledOnly,only_new:Boolean($('storefrontOnlyNew')?.checked)})});
        await refresh();
        notify(`${result.added} ${uninstalledOnly ? 'uninstalled catalog' : 'installed'} entr${result.added === 1 ? 'y' : 'ies'} added`);
      } catch(error) { notify(error.message); }
    }
    function storefrontConfig(settings, key) {
      const value = settings?.[key];
      if (typeof value === 'boolean') return { enabled: value, platform: '', include_uninstalled: false };
      return value && typeof value === 'object' ? value : {};
    }
    function renderStorefrontStatus(sources = []) {
      const target = $('storefrontStatusList');
      if (!target) return;
      target.innerHTML = sources.length ? sources.map(item => `<div class="storefront-row"><div><strong>${escapeHtml(item.name || item.id)}</strong><small>${item.status === 'ok' ? 'Scanned' : escapeHtml(item.status || '')}${item.games ? ` · ${item.games} game${item.games === 1 ? '' : 's'}` : ''}${item.last_scan ? ` · ${escapeHtml(item.last_scan.replace('T', ' '))}` : ''}</small></div><span>${item.error ? escapeHtml(item.error) : item.installed ? 'Available' : 'Not installed'}</span></div>`).join('') : '<p class="description">No storefront status recorded yet.</p>';
    }
    async function loadStorefrontStatus() {
      try {
        const result = await api('/api/v2/storefront/status');
        renderStorefrontStatus(result.sources || []);
      } catch (error) { renderStorefrontStatus([]); }
    }
    async function scanStorefronts() {
      const button = $('scanStorefronts');
      if (button) button.disabled = true;
      if ($('storefrontScanStatus')) $('storefrontScanStatus').textContent = 'Scanning storefronts…';
      try {
        await api('/api/v2/storefront/scan', { method: 'POST', body: JSON.stringify({ include_uninstalled: true }) });
        for (let attempt = 0; attempt < 35; attempt += 1) {
          await new Promise(resolve => setTimeout(resolve, 1000));
          await loadStorefrontStatus();
          const result = await api('/api/v2/storefront/status');
          if ((result.sources || []).every(item => !['pending', 'running'].includes(item.status))) {
            if ($('storefrontScanStatus')) $('storefrontScanStatus').textContent = 'Storefront scan finished.';
            return;
          }
        }
        if ($('storefrontScanStatus')) $('storefrontScanStatus').textContent = 'Storefront scan is still running.';
      } catch (error) {
        if ($('storefrontScanStatus')) $('storefrontScanStatus').textContent = error.message;
        notify(error.message);
      } finally {
        if (button) button.disabled = false;
      }
    }
    async function loadPluginStorefronts() {
      const target = $('storefrontPlugins');
      if (!target) return;
      try {
        const result = await api('/api/v2/storefronts');
        const plugins = (result.storefronts || []).filter(item => item.kind === 'plugin');
        target.innerHTML = plugins.length ? plugins.map((item, index) => {
          const key = `plugin:${item.plugin_id}`;
          const config = AppState.appSettings.storefront_auto_import?.[key] || {};
          return `<div class="storefront-row" data-plugin-store="${escapeHtml(item.plugin_id)}"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.platform || 'Plugin storefront')}${item.permission_granted ? '' : ' · Network access not granted'}</small></div><span><label class="check"><input type="checkbox" data-plugin-auto-import="${escapeHtml(item.plugin_id)}"${config.enabled ? ' checked' : ''}> Auto-import</label> <input class="plugin-store-platform" data-plugin-platform="${escapeHtml(item.plugin_id)}" value="${escapeHtml(config.platform || '')}" placeholder="Platform" aria-label="Import platform"> <label class="check"><input type="checkbox" data-plugin-uninstalled="${escapeHtml(item.plugin_id)}"${config.include_uninstalled ? ' checked' : ''}> Include uninstalled</label> <button type="button" class="icon-button" data-plugin-catalog="${escapeHtml(item.plugin_id)}">Browse</button> <button type="button" class="icon-button" data-plugin-import="${escapeHtml(item.plugin_id)}" data-plugin-import-installed="1">Import installed</button> <button type="button" class="icon-button" data-plugin-import="${escapeHtml(item.plugin_id)}">Import owned</button></span><details class="plugin-store-settings"><summary>Store settings</summary><div data-plugin-settings-host="${index}"></div></details></div>`;
        }).join('') : '<p class="description">No storefront plugins are installed.</p>';
        plugins.forEach((item, index) => {
          const host = target.querySelector(`[data-plugin-settings-host="${index}"]`);
          if (!host) return;
          renderPluginSettings(host, item.settings_schema || {}, item.settings_values || {}, {
            pluginName: item.name,
            idPrefix: item.plugin_id,
            saveLabel: t('plugins.settings_save'),
            onSave: async (values, container) => {
              try {
                const clear_secrets = [...container.querySelectorAll('[data-plugin-clear-secret]:checked')].map(input => input.dataset.pluginClearSecret);
                await api('/api/v2/plugins/settings', { method: 'POST', body: JSON.stringify({ id: item.plugin_id, values, clear_secrets }) });
                notify(t('plugins.settings_saved'));
                await loadPluginStorefronts();
              } catch (error) { notify(error.message); }
            },
          });
        });
        target.querySelectorAll('[data-plugin-import]').forEach(button => button.onclick = async () => {
          try {
            const installedOnly = button.dataset.pluginImportInstalled === '1';
            const imported = await api('/api/v2/storefronts/plugin/import', { method: 'POST', body: JSON.stringify({ plugin_id: button.dataset.pluginImport, installed_only: installedOnly }) });
            await refresh();
            notify(`${imported.added} plugin game${imported.added === 1 ? '' : 's'} added`);
          } catch (error) { notify(error.message); }
        });
        target.querySelectorAll('[data-plugin-catalog]').forEach(button => button.onclick = async () => {
          try {
            const catalog = await api('/api/v2/storefronts/plugin/catalog', { method: 'POST', body: JSON.stringify({ plugin_id: button.dataset.pluginCatalog }) });
            $('storefrontCatalog').innerHTML = (catalog.entries || []).slice(0, 200).map(item => `<div class="storefront-row"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.source || '')} · ${item.installed ? 'Installed' : 'Not installed'}</small></div><span>${escapeHtml(item.id)}</span></div>`).join('') || '<p class="description">No entries.</p>';
          } catch (error) { notify(error.message); }
        });
      } catch (error) {
        target.innerHTML = `<p class="description">${escapeHtml(error.message)}</p>`;
      }
    }
    function openStorefronts() {
      const settings = AppState.appSettings.storefront_auto_import || {};
      const steam = storefrontConfig(settings, 'steam');
      const heroic = storefrontConfig(settings, 'heroic');
      const lutris = storefrontConfig(settings, 'lutris');
      const gameyfin = storefrontConfig(settings, 'gameyfin');
      $('storefrontAutoImportSteam').checked = Boolean(steam.enabled);
      $('storefrontSteamPlatform').value = steam.platform || '';
      $('storefrontSteamUninstalled').checked = Boolean(steam.include_uninstalled);
      $('storefrontAutoImportHeroic').checked = Boolean(heroic.enabled);
      $('storefrontHeroicPlatform').value = heroic.platform || '';
      $('storefrontHeroicUninstalled').checked = Boolean(heroic.include_uninstalled);
      if ($('storefrontAutoImportEpic')) $('storefrontAutoImportEpic').checked = Boolean(storefrontConfig(settings, 'epic').enabled);
      if ($('storefrontAutoImportGog')) $('storefrontAutoImportGog').checked = Boolean(storefrontConfig(settings, 'gog').enabled);
      if ($('storefrontAutoImportEa')) $('storefrontAutoImportEa').checked = Boolean(storefrontConfig(settings, 'ea').enabled);
      if ($('storefrontAutoImportUbisoft')) $('storefrontAutoImportUbisoft').checked = Boolean(storefrontConfig(settings, 'ubisoft').enabled);
      if ($('storefrontAutoImportEpic')) $('storefrontAutoImportEpic').checked = Boolean(storefrontConfig(settings, 'epic').enabled);
      $('storefrontAutoImportLutris').checked = Boolean(lutris.enabled);
      $('storefrontLutrisPlatform').value = lutris.platform || '';
      $('storefrontLutrisUninstalled').checked = Boolean(lutris.include_uninstalled);
      if ($('storefrontAutoImportGameyfin')) $('storefrontAutoImportGameyfin').checked = Boolean(gameyfin.enabled);
      $('storefrontGameyfinPlatform').value = gameyfin.platform || '';
      $('storefrontGameyfinUninstalled').checked = Boolean(gameyfin.include_uninstalled);
      if ($('autoImportExcludedPlatforms')) $('autoImportExcludedPlatforms').value = (AppState.appSettings.auto_import_excluded_platforms || []).join(', ');
      if ($('storefrontOnlyNew')) $('storefrontOnlyNew').checked = false;
      if ($('storefrontGameyfinUrl')) $('storefrontGameyfinUrl').value = AppState.appSettings.gameyfin_url || '';
      if ($('storefrontGameyfinUsername')) $('storefrontGameyfinUsername').value = AppState.appSettings.gameyfin_username || '';
      if ($('storefrontGameyfinPassword')) $('storefrontGameyfinPassword').value = '';
      if ($('storefrontGameyfinInstallDir')) $('storefrontGameyfinInstallDir').value = AppState.appSettings.gameyfin_install_dir || '';
      if ($('storefrontGameyfinStatus')) $('storefrontGameyfinStatus').textContent = AppState.appSettings.gameyfin_url ? `Configured · ${AppState.appSettings.gameyfin_url}` : '';
      $('storefrontCatalog').innerHTML = '';
      loadPluginStorefronts();
      loadStorefrontStatus();
      $('storefrontDialog').showModal();
    }
    async function saveStorefrontSettings() {
      AppState.appSettings = await api('/api/settings',{method:'POST',body:JSON.stringify(collectStorefrontSettings())});
      notify(t('notify.storefront.settings_saved'));
    }
    function collectStorefrontSettings() {
      const autoImport = { ...(AppState.appSettings.storefront_auto_import || {}) };
      autoImport.steam = { enabled:$('storefrontAutoImportSteam').checked, platform:$('storefrontSteamPlatform')?.value.trim() || '', include_uninstalled:Boolean($('storefrontSteamUninstalled')?.checked) };
      autoImport.heroic = { enabled:$('storefrontAutoImportHeroic').checked, platform:$('storefrontHeroicPlatform')?.value.trim() || '', include_uninstalled:Boolean($('storefrontHeroicUninstalled')?.checked) };
      autoImport.epic = { enabled:Boolean($('storefrontAutoImportEpic')?.checked), platform:'', include_uninstalled:false };
      autoImport.lutris = { enabled:$('storefrontAutoImportLutris').checked, platform:$('storefrontLutrisPlatform')?.value.trim() || '', include_uninstalled:Boolean($('storefrontLutrisUninstalled')?.checked) };
      autoImport.gameyfin = { enabled:Boolean($('storefrontAutoImportGameyfin')?.checked), platform:$('storefrontGameyfinPlatform')?.value.trim() || '', include_uninstalled:Boolean($('storefrontGameyfinUninstalled')?.checked) };
      for (const [id, checkbox] of [['gog', 'storefrontAutoImportGog'], ['ea', 'storefrontAutoImportEa'], ['ubisoft', 'storefrontAutoImportUbisoft']]) {
        if ($(checkbox)) autoImport[id] = { enabled:$(checkbox).checked, platform:'', include_uninstalled:false };
      }
      document.querySelectorAll('[data-plugin-auto-import]').forEach(input => {
        const key = `plugin:${input.dataset.pluginAutoImport}`;
        const platform = document.querySelector(`[data-plugin-platform="${input.dataset.pluginAutoImport}"]`)?.value.trim() || '';
        const uninstalled = document.querySelector(`[data-plugin-uninstalled="${input.dataset.pluginAutoImport}"]`)?.checked || false;
        autoImport[key] = { ...(autoImport[key] || {}), enabled: input.checked, platform, include_uninstalled: uninstalled };
      });
      return {
        storefront_auto_import:autoImport,
        auto_import_excluded_platforms:($('autoImportExcludedPlatforms')?.value || '').split(',').map(value => value.trim()).filter(Boolean),
        gameyfin_url:$('storefrontGameyfinUrl')?.value.trim() || '',
        gameyfin_username:$('storefrontGameyfinUsername')?.value.trim() || '',
        gameyfin_password:$('storefrontGameyfinPassword')?.value || '',
        gameyfin_install_dir:$('storefrontGameyfinInstallDir')?.value.trim() || '',
      };
    }
    // S53: this polled for up to 30 minutes with no way to stop. One watcher
    // per Gameyfin id now: starting a new install of the same game aborts the
    // previous watch, and the signal stops the poll between requests. The
    // install itself runs server-side and is unaffected by either.
    const gameyfinWatches = new Map();
    function sleepUnlessAborted(ms, signal) {
      return new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, ms);
        signal?.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); }, { once: true });
      });
    }
    async function watchGameyfinInstall(gameyfinId, { signal, attempts = 1200 } = {}) {
      for (let remaining = attempts; ; remaining -= 1) {
        const job = await api(`/api/gameyfin/install/status?gameyfin_id=${encodeURIComponent(gameyfinId)}`, { signal });
        if (job.state === 'error') throw new Error(job.error || 'Gameyfin install failed');
        if (job.state === 'done') return;
        if (job.state !== 'installing') throw new Error('Gameyfin install did not complete');
        if (remaining <= 0) throw new Error('Gameyfin install timed out');
        await sleepUnlessAborted(1500, signal);
      }
    }
    async function installGameyfin(game) {
      gameyfinWatches.get(game.gameyfin_id)?.abort();
      const controller = new AbortController();
      gameyfinWatches.set(game.gameyfin_id, controller);
      try {
        notify(t('notify.storefront.installing', {name: game.name}));
        await api('/api/gameyfin/install',{method:'POST',body:JSON.stringify({gameyfin_id:game.gameyfin_id,library_id:game.id})});
        await watchGameyfinInstall(game.gameyfin_id, { signal: controller.signal });
        await refresh();
        AppState.bigBoxGames = filteredBigBoxGames();
        if (!$('bigBox').hidden) renderBigBox();
        else renderDetails();
        notify(`${game.name} installed`);
      } catch(error) {
        if (error?.name !== 'AbortError') notify(error.message);
      } finally {
        if (gameyfinWatches.get(game.gameyfin_id) === controller) gameyfinWatches.delete(game.gameyfin_id);
      }
    }
    async function uninstallGameyfin(game) {
      gameyfinWatches.get(game.gameyfin_id)?.abort();
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
        notify((result.ok ? t('notify.storefront.ludusavi_finished', {action}) : t('notify.storefront.ludusavi_issues', {action})));
      } catch(error) { notify(error.message); }
    }
    async function hoardAction(id, action) {
      try {
        await api('/api/save-tools/hoard',{method:'POST',body:JSON.stringify({id,action})});
        notify(t('notify.storefront.hoard_finished', {action}));
      } catch(error) { notify(error.message); }
    }

export { openDiscovery, loadStorefrontCatalog, importStorefrontCatalog, openStorefronts, saveStorefrontSettings, collectStorefrontSettings, watchGameyfinInstall, installGameyfin, uninstallGameyfin, ludusaviAction, hoardAction, scanStorefronts, loadStorefrontStatus };
