import { $, escapeHtml, formatBytes, defaultBadges, defaultControllerMap, fact } from './util.js';
import { AppState, api, notify, token, selectedIds, playlistFor, applySidebarVisibility, nativePickFile, nativePickFolder } from './state.js';
import { refresh, render, renderGrid } from './library.js';
import { applyLibraryMusic } from './bigbox.js';
import { confirmAction, promptInput } from './dialogs.js';



    let settingsCategory = 'library';

    function applySettingsVisibility() {
      const query = $('settingsSearch').value.toLowerCase().trim();
      document.querySelectorAll('.settings-field').forEach(field => {
        const panels = (field.dataset.settingsPanel || '').split(/\s+/).filter(Boolean);
        const inCategory = !panels.length || panels.includes(settingsCategory);
        const haystack = `${field.dataset.setting || ''} ${field.textContent || ''}`.toLowerCase();
        const matchesSearch = !query || haystack.includes(query);
        field.hidden = !inCategory || !matchesSearch;
      });
    }

    function showSettingsCategory(category) {
      settingsCategory = category;
      document.querySelectorAll('.settings-nav-item').forEach(button => {
        button.classList.toggle('active', button.dataset.settingsCategory === category);
      });
      applySettingsVisibility();
    }

    function bindSettingsBrowse() {
      const dialog = $('settingsDialog');
      if (!dialog) return;
      dialog.querySelectorAll('button.path-browse').forEach(button => {
        button.onclick = async () => {
          const fieldId = button.dataset.browseFor;
          const kind = button.dataset.browseKind || 'file';
          const field = $(fieldId);
          if (!field) return;
          const title = `Choose ${fieldId.replace(/([A-Z])/g, ' $1').trim()}`;
          const path = kind === 'folder' ? await nativePickFolder(title) : await nativePickFile(title);
          if (!path) return;
          if (field.tagName === 'TEXTAREA') {
            const current = field.value.trim();
            field.value = current ? `${current}\n${path}` : path;
          } else field.value = path;
        };
      });
    }

    function filterSettings() {
      applySettingsVisibility();
    }
    async function completeWelcome() {
      try {
        AppState.appSettings = await api('/api/settings',{method:'POST',body:JSON.stringify({...collectSettings(),welcome_completed:true})});
        $('setupCenter').close();
      } catch(error) { notify(error.message); }
    }
    function maybeShowWelcome() {
      if (!AppState.appSettings.welcome_completed && !AppState.games.length) $('setupCenter').showModal();
    }
    function collectSettings() {
      return {
        watch_folders:$('watchFolders').value.split('\n').map(value => value.trim()).filter(Boolean),
        cloud_folder:$('cloudFolder').value.trim(),
        screensaver_seconds:Number($('screensaverSeconds').value),
        startup_commands:$('startupCommands').value.split('\n').map(value => value.trim()).filter(Boolean),
        shutdown_commands:$('shutdownCommands').value.split('\n').map(value => value.trim()).filter(Boolean),
        track_session_history:$('trackSessionHistory').checked,
        backup_on_close:$('backupOnClose').checked,
        save_backup_limit:Number($('saveBackupLimit').value),
        media_download_limit:Number($('mediaDownloadLimit').value),
        auto_import_media_types:$('autoImportMediaTypes').value.split(',').map(value => value.trim()).filter(Boolean),
        region_priority:$('regionPriority').value.split(',').map(value => value.trim()).filter(Boolean),
        library_music:$('libraryMusic').value.trim(),
        video_bgm_mix:$('videoBgmMix').checked,
        bigbox_mode:$('bigBoxMode').value,
        show_playlist_actions:$('showPlaylistActions').checked,
        dynamic_play_button:$('dynamicPlayButton').checked,
        hidden_sidebar_sections:$('hiddenSidebarSections').value.split(',').map(value => value.trim()).filter(Boolean),
        obs_auto_attach:$('obsAutoAttach').checked,
        obs_recording_path:$('obsRecordingPath').value.trim(),
        progress_automation_enabled:$('progressAutomationEnabled').checked,
        progress_automation_play_minutes:Number($('progressAutomationMinutes').value),
        progress_automation_idle_days:Number($('progressAutomationIdleDays').value),
        progress_on_first_play:$('progressOnFirstPlay').value,
        tracking_mode:$('trackingMode').value,
        tracking_delay:Number($('trackingDelay').value),
        tracking_frequency:Number($('trackingFrequency').value),
        apply_perf:$('applyPerf').value,
        auto_close_store_clients:$('autoCloseStoreClients').checked,
        image_group:$('defaultImageGroup').value,
        badge_visibility:[...document.querySelectorAll('[data-badge-setting]:checked')].map(input => input.dataset.badgeSetting),
        controller_map:Object.fromEntries([...document.querySelectorAll('[data-controller]')].map(input => [input.dataset.controller,Number(input.value)])),
        welcome_completed:AppState.appSettings.welcome_completed,
        locale:$('localeSetting').value,
        library_view:$('libraryViewSetting').value,
        cover_grouping:$('groupingSetting').value,
        custom_field_defs:($('customFieldDefs').value || '').split('\n').map(line => line.trim()).filter(Boolean).map(line => { const [name,...rest] = line.split('|'); return {name:(name || '').trim(), options:rest.join('|').split(',').map(value => value.trim()).filter(Boolean)}; }).filter(item => item.name),
        bigbox_startup_video:$('bigboxStartupVideo').value.trim(),
        bigbox_shutdown_commands:$('bigboxShutdownCommands').value.split('\n').map(value => value.trim()).filter(Boolean),
        attract_mode_seconds:Number($('attractModeSeconds').value || $('screensaverSeconds').value || 90),
        tray_enabled:$('trayEnabled').checked,
        minimize_to_tray:$('minimizeToTray').checked,
        ludusavi_backup_path:$('ludusaviBackupPath')?.value.trim() || '',
      };
    }
    async function saveEmumoviesSettings() {
      const username = $('emumoviesUsername').value.trim();
      const password = $('emumoviesPassword').value;
      if (!username || !password) return;
      await api('/api/emumovies/settings',{method:'POST',body:JSON.stringify({username,password})});
    }
    let shuttingDown = false;
    async function gracefulShutdown(force = false) {
      if (shuttingDown) return;
      shuttingDown = true;
      $('shutdownOverlay').hidden = false;
      $('shutdownMessage').textContent = force ? 'Force closing running games...' : 'Closing running games before exit...';
      try { await api('/api/shutdown',{method:'POST',body:JSON.stringify({force})}); } catch(error) {}
      $('shutdownMessage').textContent = 'Running shutdown applications...';
      window.close();
    }
    async function openSettings() {
      try {
        AppState.appSettings = await api('/api/settings');
        $('watchFolders').value = AppState.appSettings.watch_folders.join('\n');
        $('cloudFolder').value = AppState.appSettings.cloud_folder || '';
        const cloudBeta = AppState.appSettings.cloud_sync_beta ? ' (beta)' : ' (beta)';
        const cloudLast = AppState.appSettings.last_cloud_sync
          ? `Last synced ${AppState.appSettings.last_cloud_sync.replace('T', ' ')}`
          : (AppState.appSettings.last_cloud_sync_error ? `Last sync failed: ${AppState.appSettings.last_cloud_sync_error}` : 'Not synced yet');
        $('cloudStatus').textContent = `${cloudBeta} · ${cloudLast}`;
        $('screensaverSeconds').value = AppState.appSettings.screensaver_seconds;
        $('startupCommands').value = (AppState.appSettings.startup_commands || []).join('\n');
        $('shutdownCommands').value = (AppState.appSettings.shutdown_commands || []).join('\n');
        $('defaultImageGroup').value = AppState.appSettings.image_group || 'cover';
        const visibleBadges = new Set(AppState.appSettings.badge_visibility || defaultBadges);
        document.querySelectorAll('[data-badge-setting]').forEach(input => input.checked = visibleBadges.has(input.dataset.badgeSetting));
        $('trackSessionHistory').checked = AppState.appSettings.track_session_history !== false;
        $('backupOnClose').checked = Boolean(AppState.appSettings.backup_on_close);
        $('progressAutomationEnabled').checked = Boolean(AppState.appSettings.progress_automation_enabled);
        $('progressAutomationMinutes').value = AppState.appSettings.progress_automation_play_minutes ?? 30;
        $('progressAutomationIdleDays').value = AppState.appSettings.progress_automation_idle_days ?? 30;
        if ($('progressOnFirstPlay')) $('progressOnFirstPlay').value = AppState.appSettings.progress_on_first_play ?? 'Playing';
        if ($('trackingMode')) $('trackingMode').value = AppState.appSettings.tracking_mode || 'default';
        if ($('trackingDelay')) $('trackingDelay').value = AppState.appSettings.tracking_delay ?? 0;
        if ($('trackingFrequency')) $('trackingFrequency').value = AppState.appSettings.tracking_frequency ?? 2;
        if ($('applyPerf')) $('applyPerf').value = AppState.appSettings.apply_perf || 'auto';
        if ($('autoCloseStoreClients')) $('autoCloseStoreClients').checked = Boolean(AppState.appSettings.auto_close_store_clients);
        $('saveBackupLimit').value = AppState.appSettings.save_backup_limit ?? 10;
        $('mediaDownloadLimit').value = AppState.appSettings.media_download_limit ?? 0;
        $('autoImportMediaTypes').value = (AppState.appSettings.auto_import_media_types || ['cover','background','screenshots']).join(', ');
        $('regionPriority').value = (AppState.appSettings.region_priority || ['North America','World','Europe','Japan']).join(', ');
        $('libraryMusic').value = AppState.appSettings.library_music || '';
        $('videoBgmMix').checked = Boolean(AppState.appSettings.video_bgm_mix);
        $('bigBoxMode').value = AppState.appSettings.bigbox_mode || 'stage';
        $('showPlaylistActions').checked = AppState.appSettings.show_playlist_actions !== false;
        $('dynamicPlayButton').checked = AppState.appSettings.dynamic_play_button !== false;
        $('hiddenSidebarSections').value = (AppState.appSettings.hidden_sidebar_sections || []).join(', ');
        $('obsAutoAttach').checked = AppState.appSettings.obs_auto_attach !== false;
        $('obsRecordingPath').value = AppState.appSettings.obs_recording_path || '';
        $('localeSetting').value = AppState.appSettings.locale || 'en';
        $('libraryViewSetting').value = AppState.appSettings.library_view || 'grid';
        if ($('groupingSetting')) $('groupingSetting').value = AppState.appSettings.cover_grouping || 'shape';
        $('customFieldDefs').value = (AppState.appSettings.custom_field_defs || []).map(item => `${item.name}|${(item.options || []).join(',')}`).join('\n');
        $('bigboxStartupVideo').value = AppState.appSettings.bigbox_startup_video || '';
        $('bigboxShutdownCommands').value = (AppState.appSettings.bigbox_shutdown_commands || []).join('\n');
        $('attractModeSeconds').value = AppState.appSettings.attract_mode_seconds ?? AppState.appSettings.screensaver_seconds ?? 90;
        $('trayEnabled').checked = Boolean(AppState.appSettings.tray_enabled);
        $('minimizeToTray').checked = Boolean(AppState.appSettings.minimize_to_tray);
        if ($('ludusaviBackupPath')) $('ludusaviBackupPath').value = AppState.appSettings.ludusavi_backup_path || '';
        $('mediaPackStatus').textContent = (AppState.appSettings.media_packs || []).filter(item => item.active).map(item => item.name).join(', ');
        $('viewToggleButton').textContent = (AppState.appSettings.library_view || 'grid') === 'list' ? 'Grid view' : 'List view';
        $('settingsSearch').value = '';
        showSettingsCategory('library');
        bindSettingsBrowse();
        document.querySelectorAll('.settings-nav-item').forEach(button => {
          button.onclick = () => showSettingsCategory(button.dataset.settingsCategory);
        });
        filterSettings();
        const mapping = {...defaultControllerMap,...AppState.appSettings.controller_map};
        document.querySelectorAll('[data-controller]').forEach(input => input.value = mapping[input.dataset.controller]);
        $('updateStatus').textContent = `OpenBox ${AppState.appSettings.version}${AppState.appSettings.appimage ? ' · AppImage' : ' · source checkout'}`;
        $('installUpdate').hidden = true;
        $('installDesktop').disabled = !AppState.appSettings.appimage;
        if (!$('settingsDialog').open) $('settingsDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    async function openProfiles() {
      try {
        const [result,emulators,perf] = await Promise.all([api('/api/profiles'),api('/api/emulators'),api('/api/perf_profiles')]);
        $('profilesText').value = Object.entries(result.profiles).sort().map(([platform,command]) => `${platform} = ${command}`).join('\n');
        const detected = Object.entries(result.detected).filter(([platform]) => !result.profiles[platform]);
        $('detectedProfiles').innerHTML = detected.length ? `<button type="button" class="icon-button" id="useDetected">Add ${detected.length} detected profiles</button>` : '';
        if ($('useDetected')) $('useDetected').onclick = () => {
          const lines = Object.entries(result.detected).map(([platform,command]) => `${platform} = ${command}`).join('\n');
          $('profilesText').value = [$('profilesText').value, lines].filter(Boolean).join('\n');
        };
        perfDraft = Object.fromEntries(Object.entries(perf.perf_profiles || {}).map(([name,value]) => [name,{...value}]));
        const names = Object.keys(result.profiles).sort();
        $('perfProfiles').innerHTML = names.length
          ? names.map(name => {
              const entry = perfDraft[name] || {enabled:false,tdp_w:0,restore_tdp_w:0};
              return `<div class="perf-row wide"><span class="perf-name">${escapeHtml(name)}</span><label class="check"><input type="checkbox" data-perf-enabled="${escapeHtml(name)}" ${entry.enabled ? 'checked' : ''}> Enable TDP</label><label class="field"><span>Limit (W)</span><input type="number" min="0" max="60" step="0.5" data-perf-tdp="${escapeHtml(name)}" value="${entry.tdp_w || ''}"></label><label class="field"><span>Restore (W)</span><input type="number" min="0" max="60" step="0.5" data-perf-restore="${escapeHtml(name)}" value="${entry.restore_tdp_w || ''}"></label></div>`;
            }).join('')
          : '<p class="description wide">No emulator profiles yet. Add one above to set a TDP limit.</p>';
        document.querySelectorAll('[data-perf-enabled]').forEach(input => input.onchange = () => { const entry = perfDraft[input.dataset.perfEnabled] ||= {enabled:false,tdp_w:0,restore_tdp_w:0}; entry.enabled = input.checked; });
        document.querySelectorAll('[data-perf-tdp]').forEach(input => input.oninput = () => { const entry = perfDraft[input.dataset.perfTdp] ||= {enabled:false,tdp_w:0,restore_tdp_w:0}; entry.tdp_w = Number(input.value) || 0; });
        document.querySelectorAll('[data-perf-restore]').forEach(input => input.oninput = () => { const entry = perfDraft[input.dataset.perfRestore] ||= {enabled:false,tdp_w:0,restore_tdp_w:0}; entry.restore_tdp_w = Number(input.value) || 0; });
        renderEmulators(emulators.emulators);
        // Health pass: BIOS SHA1 / firmware / core drift via ?health=1
        try {
          const health = await api('/api/v2/emulators/registry?health=1');
          renderRegistryHealth(health.adapters || []);
        } catch (e) { /* health best-effort */ }
        $('profilesDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    function renderRegistryHealth(adapters) {
      const catalog = $('emulatorCatalog');
      if (!catalog) return;
      let container = document.getElementById('emulatorHealth');
      if (!container) {
        container = document.createElement('div');
        container.id = 'emulatorHealth';
        container.className = 'emulator-health';
        catalog.parentNode.insertBefore(container, catalog.nextSibling);
      }
      if (!adapters.length) { container.innerHTML = ''; return; }
      const rows = adapters.slice(0, 8).map(a => {
        const bios = a.bios_ok ? '<span style="color:var(--brand)">BIOS ✓</span>' : '<span style="color:var(--danger)">BIOS ✗</span>';
        const firm = a.firmware_ok ? '<span style="color:var(--brand)">FW ✓</span>' : '<span style="color:var(--danger)">FW ✗</span>';
        const core = a.core_ok ? '<span style="color:var(--brand)">Core ✓</span>' : '<span style="color:var(--danger)">Core ✗</span>';
        // fix-action buttons inline with tokens --focus/--brand
        let fixBtn = '';
        if (!a.bios_ok && a.bios_path) {
          fixBtn = `<button type="button" class="icon-button" data-bios-path="${escapeHtml(a.bios_path)}" style="border:1px solid var(--focus);color:var(--focus)">Show BIOS folder</button>`;
        } else if (!a.core_ok && a.core_path) {
          fixBtn = `<button type="button" class="icon-button" data-core-path="${escapeHtml(a.core_path)}" style="border:1px solid var(--brand);color:var(--brand)">Choose core</button>`;
        } else if (!a.firmware_ok) {
          fixBtn = `<button type="button" class="icon-button" style="border:1px solid var(--focus);color:var(--focus)">Firmware help</button>`;
        }
        return `<div class="emulator-health-row" style="border:1px solid var(--border-card);padding:0.5rem;margin:0.3rem 0;display:flex;justify-content:space-between;align-items:center"><div><strong>${escapeHtml(a.label)}</strong> <small>${escapeHtml(a.platform)}</small><div class="health-badges" style="display:flex;gap:0.5rem;font-size:0.85rem">${bios} ${firm} ${core}</div></div><div>${fixBtn}</div></div>`;
      }).join('');
      container.innerHTML = `<div class="section-title">Emulator Health <small style="color:var(--muted)">BIOS SHA1 / firmware / core drift</small></div>${rows}`;
      container.querySelectorAll('[data-bios-path]').forEach(btn => btn.onclick = () => { const p = btn.dataset.biosPath; if (p) { try { notify('BIOS expected at ' + p); } catch (e) {} } });
      container.querySelectorAll('[data-core-path]').forEach(btn => btn.onclick = () => { const p = btn.dataset.corePath; notify('Core missing: ' + p + ' — pick alternative core'); });
    }
    function renderEmulators(emulators) {
      $('emulatorCatalog').innerHTML = `<div class="extras"><button type="button" class="primary" id="installAllEmulators">Install all available emulators</button><button type="button" class="icon-button" id="updateAllEmulators">Update installed emulators</button></div>` + emulators.map(emulator => {
        const state = emulator.job.state || (emulator.installed ? 'installed' : 'available');
        const label = state === 'installing' ? 'Installing...' : emulator.installed ? 'Add profiles' : state === 'error' ? 'Retry' : 'Install';
        const openButton = emulator.installed ? `<button type="button" class="icon-button" data-open-emulator="${escapeHtml(emulator.app_id)}">Open</button>` : '';
        const updateButton = emulator.installed && emulator.mode === 'flatpak' ? `<button type="button" class="icon-button" data-update-emulator="${escapeHtml(emulator.app_id)}">Update</button>` : '';
        return `<div class="emulator-item" title="${escapeHtml(emulator.job.error || '')}"><div><strong>${escapeHtml(emulator.name)}</strong><small>${escapeHtml(emulator.platforms.join(' · '))} · ${escapeHtml(state)}</small></div><div class="emulator-actions">${openButton}${updateButton}<button type="button" class="icon-button" ${state === 'installing' || (!emulator.installed && !emulator.can_install) ? 'disabled' : ''} data-emulator="${escapeHtml(emulator.app_id)}" data-installed="${emulator.installed}">${label}</button></div></div>`;
      }).join('');
      $('installAllEmulators').onclick = async () => {
        try {
          await api('/api/emulators/install-all',{method:'POST',body:'{}'});
          notify('Installing all available emulators');
          watchInstallAll();
        } catch(error) { notify(error.message); }
      };
      $('updateAllEmulators').onclick = async () => {
        try {
          await api('/api/emulators/update-all',{method:'POST',body:'{}'});
          notify('Updating installed emulators');
          watchInstallAll();
        } catch(error) { notify(error.message); }
      };
      document.querySelectorAll('[data-update-emulator]').forEach(button => button.onclick = async () => {
        try { await api('/api/emulators/update',{method:'POST',body:JSON.stringify({app_id:button.dataset.updateEmulator})}); notify('Updating emulator'); watchEmulator(button.dataset.updateEmulator); } catch(error) { notify(error.message); }
      });
      document.querySelectorAll('[data-open-emulator]').forEach(button => button.onclick = async () => {
        try { await api('/api/emulators/open',{method:'POST',body:JSON.stringify({app_id:button.dataset.openEmulator})}); notify('Emulator launched'); } catch(error) { notify(error.message); }
      });
      document.querySelectorAll('[data-emulator]').forEach(button => button.onclick = async () => {
        const emulator = emulators.find(item => item.app_id === button.dataset.emulator);
        if (emulator.installed) {
          const current = new Set($('profilesText').value.split('\n').map(line => line.split('=',1)[0].trim()));
          const lines = Object.entries(emulator.profiles).filter(([platform]) => !current.has(platform)).map(([platform,command]) => `${platform} = ${command}`);
          $('profilesText').value = [$('profilesText').value,...lines].filter(Boolean).join('\n');
          notify(lines.length ? 'Profiles added. Save to apply them.' : 'Those profiles are already configured');
          return;
        }
        try {
          await api('/api/emulators/install',{method:'POST',body:JSON.stringify({app_id:emulator.app_id})});
          notify(`Installing ${emulator.name}`);
          watchEmulator(emulator.app_id);
        } catch(error) { notify(error.message); }
      });
    }
    async function watchEmulator(appId) {
      try {
        const result = await api('/api/emulators');
        renderEmulators(result.emulators);
        const emulator = result.emulators.find(item => item.app_id === appId);
        if (emulator?.job.state === 'installing') return setTimeout(() => watchEmulator(appId), 1500);
        if (emulator?.job.state === 'done') {
          const profiles = await api('/api/profiles');
          $('profilesText').value = Object.entries(profiles.profiles).sort().map(([platform,command]) => `${platform} = ${command}`).join('\n');
          notify(`${emulator.name} installed and configured`);
        } else if (emulator?.job.state === 'error') notify(emulator.job.error);
      } catch(error) { notify(error.message); }
    }
    async function watchInstallAll() {
      try {
        const result = await api('/api/emulators');
        renderEmulators(result.emulators);
        if (result.install_all?.state === 'installing') return setTimeout(watchInstallAll, 2000);
        if (result.install_all?.state === 'done') {
          const profiles = await api('/api/profiles');
          $('profilesText').value = Object.entries(profiles.profiles).sort().map(([platform,command]) => `${platform} = ${command}`).join('\n');
          notify(`Installed ${result.install_all.installed?.length || 0} emulator${result.install_all.installed?.length === 1 ? '' : 's'}`);
        } else if (result.install_all?.state === 'error') notify(result.install_all.error || 'Install all failed');
      } catch(error) { notify(error.message); }
    }
    let perfDraft = {};
    async function loadTheme() {
      const result = await api(`/api/themes?platform=${encodeURIComponent(AppState.platform === 'all' ? '' : AppState.platform)}`);
      $('themeStylesheet').href = result.selected ? `/api/theme.css?name=${encodeURIComponent(result.selected)}&token=${encodeURIComponent(token)}` : '';
      return result;
    }
    async function openThemes() {
      try {
        const result = await loadTheme();
        const platforms = [...new Set(AppState.games.map(game => game.platform).filter(Boolean))].sort();
        $('themeScope').innerHTML = '<option value="">All platforms</option>' + platforms.map(name => `<option>${escapeHtml(name)}</option>`).join('');
        $('themeScope').value = AppState.platform === 'all' ? '' : AppState.platform;
        $('themeSelect').innerHTML = '<option value="">Default</option>' + result.themes.map(name => `<option>${escapeHtml(name)}</option>`).join('');
        $('themeSelect').value = result.selected;
        $('themeScope').onchange = async () => {
          const scoped = await api(`/api/themes?platform=${encodeURIComponent($('themeScope').value)}`);
          $('themeSelect').value = scoped.selected;
        };
        if (!$('themesDialog').open) $('themesDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    async function openAchievements() {
      try {
        const result = await api('/api/ra/settings');
        $('raUsername').value = result.username || '';
        $('raApiKey').value = '';
        $('raApiKey').required = !result.configured;
        $('raProfile').textContent = result.configured ? `${result.username} · ${result.points} points${result.motto ? ` · ${result.motto}` : ''}` : 'Get your web API key from your RetroAchievements settings.';
        $('achievementsDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    async function openPlugins() {
      try {
        const result = await api('/api/plugins');
        $('pluginList').innerHTML = result.plugins.length ? result.plugins.map(plugin => `<div class="emulator-item"><div><strong>${escapeHtml(plugin.name)} · ${escapeHtml(plugin.version)}</strong><small>${escapeHtml(plugin.id)} · ${escapeHtml(plugin.hooks.join(', ') || 'no hooks')}</small></div><button type="button" class="icon-button" data-toggle-plugin="${escapeHtml(plugin.id)}" data-enabled="${plugin.enabled}">${plugin.enabled ? 'Disable' : 'Enable'}</button><button type="button" class="playlist-delete" data-remove-plugin="${escapeHtml(plugin.id)}" aria-label="Remove ${escapeHtml(plugin.name)}">×</button></div>`).join('') : '<p class="description">No plugins installed.</p>';
        document.querySelectorAll('[data-toggle-plugin]').forEach(button => button.onclick = async () => {
          try {
            await api('/api/plugins/toggle',{method:'POST',body:JSON.stringify({id:button.dataset.togglePlugin,enabled:button.dataset.enabled !== 'true'})});
            await openPlugins();
          } catch(error) { notify(error.message); }
        });
        document.querySelectorAll('[data-remove-plugin]').forEach(button => button.onclick = async () => {
          const ok = await confirmAction({
            title: 'Remove plugin',
            message: `Remove ${button.dataset.removePlugin}?`,
            consequence: 'A recoverable copy will be retained.',
          });
          if (!ok) return;
          try { await api('/api/plugins/remove',{method:'POST',body:JSON.stringify({id:button.dataset.removePlugin})}); await openPlugins(); notify('Plugin removed'); } catch(error) { notify(error.message); }
        });
        if (!$('pluginsDialog').open) $('pluginsDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    async function renderJobsPanel() {
      try {
        const result = await api('/api/jobs');
        const active = Object.entries(result.jobs || {}).filter(([, job]) => job.state === 'running' || job.state === 'queued');
        const finished = (result.history || []).slice(0, 5);
        if (!active.length && !finished.length) { $('jobsPanel').hidden = true; return; }
        const stateClass = state => state === 'error' ? 'danger' : (state === 'running' || state === 'queued') ? 'active' : '';
        const rows = [
          ...active.map(([name, job]) => `<div class="job-row"><strong>${escapeHtml(name)}</strong><span class="badge ${stateClass(job.state)}">${escapeHtml(job.state)}</span>${job.error ? `<small>${escapeHtml(job.error)}</small>` : ''}</div>`),
          ...finished.map(job => `<div class="job-row"><strong>${escapeHtml(job.name)}</strong><span class="badge ${stateClass(job.state)}">${escapeHtml(job.state)}</span><small>${job.duration_seconds}s ago-finish</small>${job.error ? `<small>${escapeHtml(job.error)}</small>` : ''}</div>`),
        ];
        $('jobsPanel').innerHTML = `<h3>Background jobs</h3><div class="facts">${rows.join('')}</div>`;
        $('jobsPanel').hidden = false;
      } catch (error) { /* jobs view is best-effort; never block the audit */ }
    }
    async function health() {
      try {
        const result = await api('/api/health',{method:'POST',body:'{}'});
        const cloudLine = AppState.appSettings.last_cloud_sync
          ? `Last cloud sync: ${AppState.appSettings.last_cloud_sync.replace('T', ' ')}`
          : (AppState.appSettings.last_cloud_sync_error ? `Last cloud sync failed: ${AppState.appSettings.last_cloud_sync_error}` : 'Cloud sync (beta): not synced yet');
        $('healthSummary').innerHTML = `<h3>Audit summary</h3><div class="facts">${fact('Games',result.games)}${fact('Missing games',result.missing)}${fact('Duplicates',result.duplicates)}${fact('Missing box fronts',result.missing_media)}${fact('Cloud sync (beta)',cloudLine)}</div>`;
        $('healthIssues').innerHTML = result.issues.length ? result.issues.map(issue => `<button type="button" class="metadata-result icon-button" data-audit-game="${issue.id}"><div><strong>${escapeHtml(issue.game)}</strong><small>${escapeHtml(issue.type)} · ${escapeHtml(issue.detail)}</small></div></button>`).join('') : '<p class="description">No library issues found.</p>';
        document.querySelectorAll('[data-audit-game]').forEach(button => button.onclick = () => { AppState.selectedId = Number(button.dataset.auditGame); $('healthDialog').close(); render(); });
        $('dedupeButton').disabled = !result.duplicates;
        renderJobsPanel();
        if (!$('healthDialog').open) $('healthDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    async function savePreset() {
      const name = await promptInput('Filter preset name', AppState.activeFilterPreset || AppState.activePlaylist);
      if (!name?.trim()) return;
      const bigbox_quick = await confirmAction({
        title: 'Pin to Big Box',
        message: 'Pin this preset to Big Box quick-switch?',
      });
      const rules = {
        platform: AppState.platform,
        platform_category: AppState.platformCategory,
        view: $('view').value,
        query: $('sidebarSearch').value.trim(),
        esrb: $('esrbFilter')?.value || '',
        progress: AppState.explorerRules.progress || ($('view').value === 'playing' ? 'Playing' : ''),
      };
      try {
        await api('/api/filter-presets',{method:'POST',body:JSON.stringify({name:name.trim(),rules,bigbox_quick})});
        AppState.activeFilterPreset = name.trim();
        AppState.activePlaylist = '';
        await refresh();
        notify('Filter preset saved');
      } catch(error) { notify(error.message); }
    }
    async function saveFilter() {
      const name = await promptInput('Playlist name', AppState.activePlaylist);
      if (!name?.trim()) return;
      const rules = {platform: AppState.platform,platform_category:AppState.platformCategory,view:$('view').value,query:$('sidebarSearch').value.trim(),esrb:$('esrbFilter')?.value || '',progress:AppState.explorerRules.progress || ''};
      try {
        await api('/api/playlists',{method:'POST',body:JSON.stringify({name:name.trim(),type:'filter',rules})});
        AppState.activePlaylist = name.trim();
        await refresh();
        notify('Playlist saved');
      } catch(error) { notify(error.message); }
    }
    async function saveManualPlaylist(name, members, parent = '', notes = '') {
      const cleanName = String(name || '').trim();
      if (!cleanName) return false;
      try {
        await api('/api/playlists',{method:'POST',body:JSON.stringify({name:cleanName,type:'manual',rules:{},members,parent,notes})});
        await refresh();
        notify('Manual playlist saved');
        return true;
      } catch(error) { notify(error.message); return false; }
    }
    async function createManualPlaylist(seedId = null) {
      const name = await promptInput('Manual playlist name');
      if (!name?.trim()) return;
      const parent = await promptInput('Parent playlist, optional', '') || '';
      await saveManualPlaylist(name, seedId === null ? [] : [seedId], parent);
    }
    async function addGamesToPlaylist(name, ids) {
      const playlist = playlistFor(name);
      if (!playlist) return;
      if (playlist.type !== 'manual') return notify('Filter playlists manage membership from their rules.');
      const members = [...new Set([...(playlist.members || []), ...ids.map(id => AppState.games.find(game => game.id === id)?.game_id || id)])];
      await saveManualPlaylist(name, members, playlist.parent, playlist.notes);
    }
    async function updateManualPlaylist(playlist, members) {
      await saveManualPlaylist(playlist.name, members, playlist.parent, playlist.notes);
      openPlaylists();
    }
    async function openPlaylists() {
      $('playlistManagerList').innerHTML = AppState.playlists.length ? AppState.playlists.map(item => {
        const members = (item.members || []).map(value => AppState.games.find(game => String(game.game_id) === String(value))).filter(Boolean);
        const memberRows = item.type === 'manual' ? `<div class="member-list">${members.map((game,index) => `<div class="member-row"><span>${escapeHtml(game.name)}</span><button type="button" class="icon-button" data-playlist-name="${escapeHtml(item.name)}" data-playlist-index="${index}" data-playlist-direction="up">↑</button><button type="button" class="icon-button" data-playlist-name="${escapeHtml(item.name)}" data-playlist-index="${index}" data-playlist-direction="down">↓</button></div>`).join('') || '<span class="description">No games yet.</span>'}</div>` : '';
        return `<div class="playlist-manager-item"><div><strong>${escapeHtml(item.name)}</strong><small>${item.type === 'manual' ? `${members.length} ordered games` : 'Filter playlist'}${item.parent ? ` · under ${escapeHtml(item.parent)}` : ''}</small>${memberRows}</div><div class="playlist-manager-actions"><button type="button" class="icon-button" data-edit-playlist="${escapeHtml(item.name)}">Edit</button><button type="button" class="playlist-delete" data-manager-delete-playlist="${escapeHtml(item.name)}" aria-label="Delete ${escapeHtml(item.name)}">×</button></div></div>`;
      }).join('') : '<p class="description">No playlists yet.</p>';
      document.querySelectorAll('[data-manager-delete-playlist]').forEach(button => button.onclick = () => deletePlaylist(button.dataset.managerDeletePlaylist).then(openPlaylists));
      document.querySelectorAll('[data-edit-playlist]').forEach(button => button.onclick = async () => {
        const item = playlistFor(button.dataset.editPlaylist);
        if (!item) return;
        const notes = await promptInput('Playlist notes', item.notes || '');
        if (notes === null) return;
        if (item.type === 'manual') updateManualPlaylist(item, item.members || []);
        else api('/api/playlists',{method:'POST',body:JSON.stringify({...item,notes})}).then(refresh).then(openPlaylists).catch(error => notify(error.message));
      });
      document.querySelectorAll('[data-playlist-index]').forEach(button => button.onclick = () => {
        const item = playlistFor(button.dataset.playlistName);
        const index = Number(button.dataset.playlistIndex);
        if (!item || item.type !== 'manual') return;
        const members = [...item.members];
        const target = index + (button.dataset.playlistDirection === 'up' ? -1 : 1);
        if (target < 0 || target >= members.length) return;
        [members[index],members[target]] = [members[target],members[index]];
        updateManualPlaylist(item, members);
      });
      if (!$('playlistsDialog').open) $('playlistsDialog').showModal();
    }
    async function createFilterPlaylist() { await saveFilter(); openPlaylists(); }
    async function openBackups() {
      try {
        const result = await api('/api/backups');
        $('backupList').innerHTML = result.backups.length ? result.backups.map(item => `<div class="backup-item"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.created || 'Unknown date')} · ${formatBytes(item.size)} · ${(item.items || []).join(', ') || 'Unknown contents'}</small></div><button type="button" class="icon-button" data-restore-backup="${escapeHtml(item.path)}" ${item.invalid ? 'disabled' : ''}>Restore</button></div>`).join('') : '<p class="description">No backups have been created yet.</p>';
        document.querySelectorAll('[data-restore-backup]').forEach(button => button.onclick = async () => {
          const ok = await confirmAction({
            title: 'Restore backup',
            message: 'Restore this backup?',
            consequence: 'A safety copy of the current library will be created first.',
          });
          if (!ok) return;
          try { const result = await api('/api/backup/restore',{method:'POST',body:JSON.stringify({path:button.dataset.restoreBackup})}); await refresh(); notify(`Restored ${result.restored.join(', ')}`); } catch(error) { notify(error.message); }
        });
        if (!$('backupDialog').open) $('backupDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    async function createNamedBackup() {
      try { const result = await api('/api/backup/create',{method:'POST',body:JSON.stringify({items:['library','settings','media','plugins','themes'],keep:7})}); notify(`Backup saved to ${result.name}`); openBackups(); } catch(error) { notify(error.message); }
    }
    async function deletePlaylist(name) {
      const ok = await confirmAction({
        title: 'Delete playlist',
        message: `Delete playlist "${name}"?`,
      });
      if (!ok) return;
      try {
        await api('/api/playlists/delete',{method:'POST',body:JSON.stringify({name})});
        if (AppState.activePlaylist === name) AppState.activePlaylist = '';
        await refresh();
        notify('Playlist deleted');
      } catch(error) { notify(error.message); }
    }
    async function backup() {
      try {
        const result = await api('/api/backup/create',{method:'POST',body:JSON.stringify({items:['library','settings','media','plugins','themes'],keep:7})});
        notify(`Backup saved to ${result.name}`);
      } catch(error) { notify(error.message); }
    }
    function bulkAction() {
      if (!AppState.bulkMode) {
        AppState.bulkMode = true;
        selectedIds.clear();
        renderGrid();
        notify('Select games, then choose Edit Selected');
      } else if (selectedIds.size) {
        $('bulkForm').reset();
        $('bulkCount').textContent = `${selectedIds.size} game${selectedIds.size === 1 ? '' : 's'} selected. Only supplied values will change.`;
        $('bulkDialog').showModal();
      } else {
        AppState.bulkMode = false;
        renderGrid();
      }
    }
    $('settingsForm').onsubmit = async event => {
      event.preventDefault();
      try {
        await saveEmumoviesSettings().catch(() => {});
        AppState.appSettings = await api('/api/settings',{method:'POST',body:JSON.stringify(collectSettings())});
        $('settingsDialog').close();
        notify('Settings saved');
        applyLibraryMusic();
        applySidebarVisibility();
        renderGrid();
      } catch(error) { notify(error.message); }
    };
    $('scanWatched').onclick = async () => {
      try {
        AppState.appSettings = await api('/api/settings',{method:'POST',body:JSON.stringify(collectSettings())});
        const result = await api('/api/import/watch',{method:'POST',body:'{}'});
        await refresh();
        notify(`${result.added} games imported from watched folders${result.errors.length ? ` · ${result.errors.length} errors` : ''}`);
      } catch(error) { notify(error.message); }
    };
    $('removeSteamGames').onclick = async () => {
      const ok = await confirmAction({
        title: 'Remove Steam games',
        message: 'Remove every imported Steam game from OpenBox?',
        consequence: 'Game files and media will stay on disk.',
      });
      if (!ok) return;
      try {
        const result = await api('/api/games/delete-steam',{method:'POST',body:'{}'});
        AppState.selectedId = null;
        await refresh();
        notify(`${result.removed} imported Steam game${result.removed === 1 ? '' : 's'} removed from library`);
      } catch(error) { notify(error.message); }
    };
    $('copyDiagnosticLog').onclick = async () => {
      try {
        const preview = await api('/api/diagnostic');
        const text = JSON.stringify(preview, null, 2);
        const ok = await confirmAction({
          title: 'Copy diagnostic summary',
          message: 'Review this redacted preview before copying. It excludes secrets and the full log.',
          consequence: text.length > 1200 ? `${text.slice(0, 1200)}\n…` : text,
        });
        if (!ok) return;
        const copy = document.createElement('textarea');
        copy.value = text;
        document.body.append(copy);
        copy.select();
        document.execCommand('copy');
        copy.remove();
        notify('Diagnostic summary copied. Review it before sharing.');
      } catch(error) { notify(error.message); }
    };
    $('syncCloud').onclick = async () => {
      try {
        AppState.appSettings = await api('/api/settings',{method:'POST',body:JSON.stringify(collectSettings())});
        const result = await api('/api/cloud/sync',{method:'POST',body:'{}'});
        $('cloudStatus').textContent = ` (beta) · Synced ${result.games} games, merged ${result.merged} remote changes`;
        await refresh();
      } catch(error) {
        $('cloudStatus').textContent = ` (beta) · Sync failed: ${error.message}`;
        notify(error.message);
      }
    };
    $('checkUpdate').onclick = async () => {
      try {
        $('updateStatus').textContent = 'Checking the verified GitHub release channel...';
        AppState.pendingUpdate = await api('/api/update');
        $('updateStatus').textContent = AppState.pendingUpdate.available ? `OpenBox ${AppState.pendingUpdate.latest} is available. ${AppState.pendingUpdate.notes || ''}` : `OpenBox ${AppState.pendingUpdate.current} is current.`;
        $('installUpdate').hidden = !AppState.pendingUpdate.available;
      } catch(error) { $('updateStatus').textContent = error.message; }
    };
    $('installUpdate').onclick = async () => {
      if (!AppState.pendingUpdate?.available) return;
      const ok = await confirmAction({
        title: 'Install update',
        message: `Install OpenBox ${AppState.pendingUpdate.latest}?`,
        consequence: 'The current AppImage will be retained as a backup.',
      });
      if (!ok) return;
      try {
        $('installUpdate').disabled = true;
        const result = await api('/api/update/install',{method:'POST',body:'{}'});
        $('updateStatus').textContent = `OpenBox ${result.installed} installed. Restart OpenBox to use it. Backup: ${result.backup}`;
        $('installUpdate').hidden = true;
      } catch(error) { $('updateStatus').textContent = error.message; }
      finally { $('installUpdate').disabled = false; }
    };
    $('installDesktop').onclick = async () => {
      try {
        const result = await api('/api/desktop/install',{method:'POST',body:'{}'});
        notify(`Desktop shortcut installed at ${result.desktop}`);
      } catch(error) { notify(error.message); }
    };
    $('profilesForm').onsubmit = async event => {
      event.preventDefault();
      const profiles = {};
      $('profilesText').value.split('\n').forEach(line => {
        const split = line.indexOf('=');
        if (split > 0) profiles[line.slice(0,split).trim()] = line.slice(split + 1).trim();
      });
      try {
        const result = await api('/api/profiles',{method:'POST',body:JSON.stringify({profiles})});
        try { await api('/api/perf_profiles',{method:'POST',body:JSON.stringify({perf_profiles:perfDraft})}); } catch(error) { notify(error.message); }
        $('profilesDialog').close();
        notify(`${result.saved} emulator profiles saved`);
      } catch(error) { notify(error.message); }
    };
    $('themesForm').onsubmit = async event => {
      event.preventDefault();
      try { await api('/api/themes/select',{method:'POST',body:JSON.stringify({name:$('themeSelect').value,platform:$('themeScope').value})}); $('themesDialog').close(); await loadTheme(); notify('Theme applied'); } catch(error) { notify(error.message); }
    };
    $('achievementsForm').onsubmit = async event => {
      event.preventDefault();
      try {
        const result = await api('/api/ra/settings',{method:'POST',body:JSON.stringify({username:$('raUsername').value,api_key:$('raApiKey').value})});
        $('achievementsDialog').close();
        await refresh();
        notify(`Connected ${result.username}`);
      } catch(error) { notify(error.message); }
    };
    $('browsePluginCatalog').onclick = async () => {
      try {
        const result = await api('/api/plugins/catalog');
        const entry = result.catalog?.[0];
        if (!entry) return notify('No community plugins are listed yet.');
        if (entry.local_only) return notify(`${entry.name}: install local plugin packages manually.`);
        await api('/api/plugins/catalog/install',{method:'POST',body:JSON.stringify({id:entry.id})});
        await openPlugins();
        notify(`${entry.name} installed from catalog`);
      } catch(error) { notify(error.message); }
    };
    $('installPlugin').onclick = async () => {
      const path = await nativePickFile('Absolute path of the plugin directory or ZIP package');
      if (!path) return;
      try {
        const result = await api('/api/plugins/install',{method:'POST',body:JSON.stringify({path})});
        await openPlugins();
        notify(`${result.plugin.name} ${result.plugin.updated ? 'updated' : 'installed'}`);
      } catch(error) { notify(error.message); }
    };
    $('importTheme').onclick = async () => {
      const path = await nativePickFile('Enter the absolute path of a CSS theme file.');
      if (!path) return;
      try { await api('/api/themes/import',{method:'POST',body:JSON.stringify({path})}); await openThemes(); notify('Theme imported'); } catch(error) { notify(error.message); }
    };
    $('dedupeButton').onclick = async () => {
      const ok = await confirmAction({
        title: 'Remove duplicates',
        message: 'Remove duplicate library entries?',
        consequence: 'Game files will not be deleted.',
      });
      if (!ok) return;
      try { const result = await api('/api/health/dedupe',{method:'POST',body:'{}'}); await refresh(); await health(); notify(`${result.removed.length} duplicate entries removed`); } catch(error) { notify(error.message); }
    };
    let featureMode = '';
    async function openFeature(mode) {
      featureMode = mode;
      $('featureDialogTitle').textContent = mode === 'queue' ? 'Play Queue' : mode === 'tags' ? 'Tags' : mode === 'notifications' ? 'Notifications' : 'Webhook Settings';
      const content = $('featureContent');
      try {
        if (mode === 'queue') {
          const result = await api('/api/queue');
          content.innerHTML = `<div class="extras"><button class="primary" type="button" id="queueAdvance">Play next</button><button class="icon-button" type="button" id="queueAdd">Add selected game</button></div><div class="emulator-list">${(result.queue || []).map(item => `<div class="detail-card"><strong>${escapeHtml(item.name)}</strong><p class="description">${escapeHtml(item.platform || '')}${item.note ? ` · ${escapeHtml(item.note)}` : ''}</p><button type="button" class="icon-button" data-queue-remove="${escapeHtml(item.game_id)}">Remove</button></div>`).join('') || '<p class="description">Queue is empty.</p>'}</div>`;
          $('queueAdvance').onclick = async () => { try { await api('/api/queue',{method:'POST',body:JSON.stringify({action:'advance'})}); notify('Queue advanced'); openFeature('queue'); } catch (error) { notify(error.message); } };
          $('queueAdd').onclick = async () => { if (AppState.selectedId === null) return notify('Select a game first.'); const game = AppState.games.find(item => item.id === AppState.selectedId); if (!game) return notify('Selected game not found.'); try { await api('/api/queue',{method:'POST',body:JSON.stringify({action:'enqueue',game_ids:[game.game_id]})}); notify('Game added to queue'); openFeature('queue'); } catch (error) { notify(error.message); } };
          document.querySelectorAll('[data-queue-remove]').forEach(button => button.onclick = async () => { await api('/api/queue',{method:'POST',body:JSON.stringify({action:'remove',game_ids:[button.dataset.queueRemove]})}); openFeature('queue'); });
        } else if (mode === 'tags') {
          const result = await api('/api/tags');
          content.innerHTML = `<p class="description">Select a game, then add or replace its tags.</p><div class="platforms">${(result.tags || []).map(item => `<button type="button" class="platform" data-tag-filter="${escapeHtml(item.tag)}">${escapeHtml(item.tag)} (${item.count})</button>`).join('') || '<span class="description">No tags yet.</span>'}</div><div class="extras"><input id="featureTagInput" placeholder="comma-separated tags"><button type="button" class="primary" id="saveFeatureTags">Save tags on selected game</button></div>`;
          document.querySelectorAll('[data-tag-filter]').forEach(button => button.onclick = () => { $('sidebarSearch').value = `tag:${button.dataset.tagFilter}`; $('featureDialog').close(); render(); });
          $('saveFeatureTags').onclick = async () => { if (AppState.selectedId === null) return notify('Select a game first.'); const game = AppState.games.find(item => item.id === AppState.selectedId); if (!game) return notify('Selected game not found.'); const tags = $('featureTagInput').value.split(',').map(value => value.trim()).filter(Boolean); try { await api('/api/tags',{method:'POST',body:JSON.stringify({ids:[game.game_id],tags})}); await refresh(); openFeature('tags'); notify('Tags saved'); } catch (error) { notify(error.message); } };
        } else if (mode === 'notifications') {
          const result = await api('/api/notifications');
          content.innerHTML = `<div class="extras"><button type="button" class="primary" id="readNotifications">Mark all read</button><button type="button" class="icon-button" id="clearNotifications">Clear</button></div>${(result.notifications || []).map(item => `<div class="detail-card"><strong>${escapeHtml(item.title)}</strong><p class="description">${escapeHtml(item.body)}<br>${escapeHtml(item.created_at)}</p></div>`).join('') || '<p class="description">No notifications.</p>'}`;
          $('readNotifications').onclick = async () => { await api('/api/notifications',{method:'POST',body:JSON.stringify({action:'read'})}); openFeature('notifications'); };
          $('clearNotifications').onclick = async () => { await api('/api/notifications',{method:'POST',body:JSON.stringify({action:'clear'})}); openFeature('notifications'); };
        } else {
          const result = await api('/api/webhooks');
          const current = (result.webhooks || [])[0] || {};
          content.innerHTML = `<label class="field wide"><span>Webhook URL</span><input id="webhookUrl" value="${escapeHtml(current.url || '')}" placeholder="https://example.com/webhook"></label><label class="field"><span>Secret</span><input id="webhookSecret" type="password" value="${escapeHtml(current.secret || '')}" placeholder="Optional signature secret"></label><label class="field wide"><span>Events (comma separated)</span><input id="webhookEvents" value="${escapeHtml((current.events || ['session.started','session.stopped']).join(', '))}" placeholder="session.started, session.stopped"></label><div class="extras"><button type="button" class="primary" id="saveWebhook">Save webhook</button><button type="button" class="icon-button" id="testWebhook">Test webhook</button></div>`;
          $('saveWebhook').onclick = async () => { try { await api('/api/webhooks',{method:'POST',body:JSON.stringify({webhooks:[{...current,url:$('webhookUrl').value.trim(),secret:$('webhookSecret').value,events:$('webhookEvents').value.split(',').map(value => value.trim()).filter(Boolean),enabled:true}]})}); notify('Webhook saved'); openFeature('webhooks'); } catch (error) { notify(error.message); } };
          $('testWebhook').onclick = async () => { try { const testResult = await api('/api/webhooks/test',{method:'POST',body:JSON.stringify({url:$('webhookUrl').value.trim(),secret:$('webhookSecret').value,events:['session.started']})}); notify(testResult.ok ? 'Webhook test succeeded' : testResult.error); } catch (error) { notify(error.message); } };
        }
        if (!$('featureDialog').open) $('featureDialog').showModal();
      } catch (error) { notify(error.message); }
    }

export { filterSettings, completeWelcome, maybeShowWelcome, collectSettings, saveEmumoviesSettings, shuttingDown, gracefulShutdown, openSettings, openProfiles, renderEmulators, watchEmulator, watchInstallAll, perfDraft, loadTheme, openThemes, openAchievements, openPlugins, renderJobsPanel, health, savePreset, saveFilter, saveManualPlaylist, createManualPlaylist, addGamesToPlaylist, updateManualPlaylist, openPlaylists, createFilterPlaylist, openBackups, createNamedBackup, deletePlaylist, backup, bulkAction, featureMode, openFeature };
