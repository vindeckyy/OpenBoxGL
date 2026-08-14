import { $ } from './util.js';
import { api, notify, nativePickFolder, nativePickFile, AppState } from './state.js';
import { refresh } from './library.js';



    async function importFolder() {
      const folder = await nativePickFolder('Enter the absolute path of the folder to import.');
      if (!folder) return;
      try {
        const preview = await api('/api/import',{method:'POST',body:JSON.stringify({folder,recommend:true})});
        const chosen = {};
        for (const [platform, items] of Object.entries(preview.recommendations || {})) {
          if (!items?.length) continue;
          if (items.length === 1) { chosen[platform] = items[0].app_id; continue; }
          const labels = items.map((item,index) => `${index + 1}. ${item.name}`).join('\n');
          const pick = prompt(`Multiple emulators for ${platform}:\n${labels}\nEnter number to install, or leave blank to skip`, '1');
          const index = Number(pick) - 1;
          if (pick && items[index]) chosen[platform] = items[index].app_id;
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
      const source = prompt('Set type: MAME or FinalBurn Neo', 'MAME');
      if (!source) return;
      const dat = (await nativePickFile('Absolute DAT/XML path. Leave blank to use installed MAME metadata.')) ?? '';
      const command = prompt('Launch command. Leave blank for the detected emulator. You can use {rom_name} and {path}.', '') ?? '';
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
          if (items.length === 1) { chosen[platformName] = items[0].app_id; continue; }
          const labels = items.map((item,index) => `${index + 1}. ${item.name}`).join('\n');
          const pick = prompt(`Multiple emulators for ${platformName}:\n${labels}\nEnter number to install, or leave blank to skip`, '1');
          const index = Number(pick) - 1;
          if (pick && items[index]) chosen[platformName] = items[index].app_id;
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
