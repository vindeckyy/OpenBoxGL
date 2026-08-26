import './setup.js';
import { $ } from './util.js';
import { api, notify, nativePickFolder, nativePickFile, AppState } from './state.js';
import { refresh } from './library.js';
import { promptChoice, promptInput } from './dialogs.js';

async function pickEmulatorForPlatform(platform, items) {
  if (!items?.length) return null;
  if (items.length === 1) return items[0].app_id;
  const choice = await promptChoice({
    title: `Emulators for ${platform}`,
    message: 'Choose an emulator to install, or cancel to skip this platform.',
    choices: items.map((item, index) => ({
      value: String(index),
      label: item.name || item.app_id || `Option ${index + 1}`,
    })),
    defaultValue: '0',
  });
  if (choice == null || choice === '') return null;
  const index = Number(choice);
  return items[index]?.app_id || null;
}

    async function importFolder() {
      const folder = await nativePickFolder('Enter the absolute path of the folder to import.');
      if (!folder) return;
      try {
        const preview = await api('/api/import',{method:'POST',body:JSON.stringify({folder,recommend:true})});
        const chosen = {};
        for (const [platform, items] of Object.entries(preview.recommendations || {})) {
          if (!items?.length) continue;
          const appId = await pickEmulatorForPlatform(platform, items);
          if (appId) chosen[platform] = appId;
        }
        const result = Object.keys(chosen).length
          ? await api('/api/import/wizard',{method:'POST',body:JSON.stringify({folder,chosen_emulators:chosen})})
          : preview;
        await refresh();
        notify(`${result.added} games imported${result.installed?.length ? ` · installed ${result.installed.length} emulator(s)` : ''}`);
      } catch(error) { notify(error.message); }
    }
    async function importSteam() {
      try {
        const result = await api('/api/import/steam',{method:'POST',body:'{}'});
        await refresh();
        notify(`${result.added} Steam games imported · ${result.found} installed`);
      } catch(error) { notify(error.message); }
    }
    async function importHeroic() {
      try {
        const result = await api('/api/import/heroic',{method:'POST',body:'{}'});
        await refresh();
        notify(`${result.added} Heroic games imported · ${result.found} installed`);
      } catch(error) { notify(error.message); }
    }
    async function importLutris() {
      try {
        const result = await api('/api/import/lutris',{method:'POST',body:'{}'});
        await refresh();
        notify(`${result.added} Lutris games imported · ${result.found} installed`);
      } catch(error) { notify(error.message); }
    }
    async function importArcade() {
      const folder = await nativePickFolder('Absolute path of the arcade ROM folder');
      if (!folder) return;
      const source = await promptChoice({
        title: 'Arcade set type',
        message: 'Choose the arcade set type.',
        choices: [{value: 'MAME', label: 'MAME'}, {value: 'FinalBurn Neo', label: 'FinalBurn Neo'}],
        defaultValue: 'MAME',
      });
      if (!source) return;
      const dat = (await nativePickFile('Absolute DAT/XML path. Leave blank to use installed MAME metadata.')) ?? '';
      const command = (await promptInput({
        title: 'Launch command',
        message: 'Leave blank for the detected emulator. You can use {rom_name} and {path}.',
        defaultValue: '',
      })) ?? '';
      try {
        const result = await api('/api/import/arcade',{method:'POST',body:JSON.stringify({folder,source,dat,command})});
        await refresh();
        notify(`${result.added} arcade games imported · ${result.found} matched`);
      } catch(error) { notify(error.message); }
    }
    async function importDroppedFolder(folder) {
      try {
        const preview = await api('/api/import',{method:'POST',body:JSON.stringify({folder,recommend:true})});
        const chosen = {};
        for (const [platformName, items] of Object.entries(preview.recommendations || {})) {
          if (!items?.length) continue;
          const appId = await pickEmulatorForPlatform(platformName, items);
          if (appId) chosen[platformName] = appId;
        }
        const result = Object.keys(chosen).length
          ? await api('/api/import/wizard',{method:'POST',body:JSON.stringify({folder,chosen_emulators:chosen})})
          : preview;
        await refresh();
        notify(`${result.added} games imported${result.installed?.length ? ` · installed ${result.installed.length} emulator(s)` : ''}`);
      } catch(error) { notify(error.message); }
    }
    async function runStartupStorefrontImports() {
      const settings = AppState.appSettings.storefront_auto_import || {};
      if (settings.steam) await api('/api/import/steam',{method:'POST',body:'{}'}).catch(() => {});
      if (settings.heroic) await api('/api/import/heroic',{method:'POST',body:'{}'}).catch(() => {});
      if (settings.lutris) await api('/api/import/lutris',{method:'POST',body:'{}'}).catch(() => {});
      if (settings.gameyfin) await api('/api/storefront/import',{method:'POST',body:JSON.stringify({source:'gameyfin'})}).catch(() => {});
    }

export { importFolder, importSteam, importHeroic, importLutris, importArcade, importDroppedFolder, runStartupStorefrontImports };
