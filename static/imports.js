import './setup.js';
import { $, escapeHtml } from './util.js';
import { api, notify, nativePickFolder, nativePickFile, AppState } from './state.js';
import { refresh } from './library.js';
import { promptChoice, promptInput } from './dialogs.js';
import { t } from './i18n.js';

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

let launchBoxPreview = null;

function invalidateLaunchBoxPreview() {
  launchBoxPreview = null;
  const apply = $('applyLaunchBox');
  if (apply) { apply.hidden = true; apply.disabled = true; }
  const report = $('launchboxReport');
  if (report) { report.hidden = true; report.textContent = ''; }
  const status = $('launchboxStatus');
  if (status) status.textContent = '';
}

function collectLaunchBoxOptions() {
  const sourceRoot = $('launchboxSourceRoot')?.value.trim() || '';
  const destinationRoot = $('launchboxDestinationRoot')?.value.trim() || '';
  if (Boolean(sourceRoot) !== Boolean(destinationRoot)) {
    throw new Error(t('metadata.launchbox_mapping_pair'));
  }
  const options = {};
  if (sourceRoot) options.path_mappings = [{source: sourceRoot, destination: destinationRoot}];
  const profileMap = {};
  const mappingText = $('launchboxEmulatorMap')?.value || '';
  for (const [index, line] of mappingText.split('\n').map(value => value.trim()).filter(Boolean).entries()) {
    const separator = line.indexOf('=');
    if (separator <= 0 || !line.slice(separator + 1).trim()) {
      throw new Error(t('metadata.launchbox_invalid_mapping', {line: index + 1}));
    }
    profileMap[line.slice(0, separator).trim()] = line.slice(separator + 1).trim();
  }
  if (Object.keys(profileMap).length) options.emulator_profile_map = profileMap;
  if ($('launchboxOverwrite')?.checked) options.overwrite_fields = true;
  return options;
}

function launchBoxActionLabel(action) {
  const keys = {
    add: 'metadata.launchbox_add', merge: 'metadata.launchbox_merge',
    skip: 'metadata.launchbox_skip', exclude: 'metadata.launchbox_exclude',
  };
  return t(keys[action] || 'metadata.launchbox_skip');
}

function renderLaunchBoxReport(report) {
  const reportElement = $('launchboxReport');
  const status = $('launchboxStatus');
  const plan = report?.plan || report;
  const counts = report?.counts || report?.plan?.counts || report || {};
  const operations = Array.isArray(plan?.operations) ? plan.operations : [];
  if (!reportElement || !status) return;
  const summary = [
    t('metadata.launchbox_found', {count: counts.total_in_xml ?? counts.found ?? 0}),
    t('metadata.launchbox_add_count', {count: counts.added ?? 0}),
    t('metadata.launchbox_merge_count', {count: counts.merged ?? 0}),
    t('metadata.launchbox_skip_count', {count: (counts.skipped ?? 0) + (counts.duplicates ?? 0) + (counts.excluded ?? 0)}),
  ].join(' · ');
  const rows = operations.slice(0, 50).map(operation => {
    const game = operation.game || {};
    const details = [];
    if (game.path) details.push(`${t('metadata.launchbox_path')}: ${game.path}`);
    if (operation.review_fields?.length) details.push(`${t('metadata.launchbox_review')}: ${operation.review_fields.join(', ')}`);
    return `<div class="detail-card"><strong>${escapeHtml(launchBoxActionLabel(operation.action))}</strong> ${escapeHtml(game.name || operation.source_id || t('metadata.launchbox_unnamed'))}${details.length ? `<p class="description">${escapeHtml(details.join(' · '))}</p>` : ''}</div>`;
  }).join('');
  const warnings = [];
  if (plan?.emulator_ids?.length) warnings.push(`${t('metadata.launchbox_emulators')}: ${plan.emulator_ids.join(', ')}`);
  if (plan?.errors?.length) warnings.push(`${t('metadata.launchbox_errors')}: ${plan.errors.join(' · ')}`);
  if (plan?.unsupported_fields?.length) warnings.push(`${t('metadata.launchbox_unsupported')}: ${plan.unsupported_fields.join(', ')}`);
  reportElement.innerHTML = `<div class="detail-card"><strong>${escapeHtml(summary)}</strong>${warnings.length ? `<p class="description">${escapeHtml(warnings.join(' · '))}</p>` : ''}</div>${rows || `<p class="description">${escapeHtml(t('metadata.launchbox_no_operations'))}</p>`}`;
  reportElement.hidden = false;
  status.textContent = t('metadata.launchbox_preview_ready');
  const apply = $('applyLaunchBox');
  if (apply) {
    apply.hidden = false;
    apply.disabled = !report?.plan || !report?.preview_token;
  }
}

async function previewLaunchBox() {
  const xmlPath = $('launchboxXmlPath')?.value.trim() || '';
  if (!xmlPath) return notify(t('metadata.launchbox_source_required'));
  const button = $('previewLaunchBox');
  try {
    const options = collectLaunchBoxOptions();
    if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
    const report = await api('/api/v2/import/launchbox/preview', {
      method: 'POST', body: JSON.stringify({xml_path: xmlPath, options}),
    });
    launchBoxPreview = report;
    renderLaunchBoxReport(report);
  } catch (error) {
    launchBoxPreview = null;
    const apply = $('applyLaunchBox');
    if (apply) { apply.hidden = true; apply.disabled = true; }
    notify(error.message);
  } finally {
    if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
  }
}

async function applyLaunchBox() {
  const xmlPath = $('launchboxXmlPath')?.value.trim() || '';
  if (!xmlPath || !launchBoxPreview?.plan) return notify(t('metadata.launchbox_preview_required'));
  const button = $('applyLaunchBox');
  try {
    const options = collectLaunchBoxOptions();
    if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
    const result = await api('/api/v2/import/launchbox/apply', {
      method: 'POST',
      body: JSON.stringify({
        xml_path: xmlPath,
        plan: launchBoxPreview.plan,
        preview_token: launchBoxPreview.preview_token,
        options,
      }),
    });
    launchBoxPreview = null;
    if (button) button.hidden = true;
    $('launchboxStatus').textContent = t('metadata.launchbox_applied', {added: result.added ?? 0, merged: result.merged ?? 0});
    await refresh();
  } catch (error) {
    notify(error.message);
  } finally {
    if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
  }
}

function bindLaunchBoxMigration() {
  const browse = $('browseLaunchBoxXml');
  if (browse) browse.onclick = async () => {
    const path = await nativePickFile(t('metadata.launchbox_select_file'));
    if (path) { $('launchboxXmlPath').value = path; invalidateLaunchBoxPreview(); }
  };
  const preview = $('previewLaunchBox');
  if (preview) preview.onclick = previewLaunchBox;
  const apply = $('applyLaunchBox');
  if (apply) apply.onclick = applyLaunchBox;
  for (const id of ['launchboxXmlPath', 'launchboxSourceRoot', 'launchboxDestinationRoot', 'launchboxEmulatorMap', 'launchboxOverwrite']) {
    const control = $(id);
    control?.addEventListener(control.type === 'checkbox' ? 'change' : 'input', invalidateLaunchBoxPreview);
  }
}

export { importFolder, importSteam, importHeroic, importLutris, importArcade, importDroppedFolder, runStartupStorefrontImports, bindLaunchBoxMigration };
