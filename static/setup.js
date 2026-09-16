import { $, escapeHtml } from './util.js';
import { api, notify, AppState, nativePickFolder, nativePickFile, resetQuery } from './state.js';
import { promptChoice, promptInput, openDialog, closeDialog } from './dialogs.js';
import { openMatchReview } from './metadata.js';
import { openActivity } from './activity.js';
import { refresh } from './library.js';
import { t, onLocaleChange } from './i18n.js';
import { waitForJob as waitForJobSse } from './events.js';

const SUMMARY_KEYS = [
  'library_count', 'source_coverage', 'metadata_match_percent', 'media_gaps',
  'duplicate_count', 'missing_paths', 'emulator_readiness', 'active_operations', 'next_action',
];
const STEPS = [
  {id: 1, labelKey: 'setup.step_overview', helpKey: 'setup.step_overview_help'},
  {id: 2, labelKey: 'setup.step_sources', helpKey: 'setup.step_sources_help'},
  {id: 3, labelKey: 'setup.step_scan', helpKey: 'setup.step_scan_help'},
  {id: 4, labelKey: 'setup.step_decisions', helpKey: 'setup.step_decisions_help'},
  {id: 5, labelKey: 'setup.step_readiness', helpKey: 'setup.step_readiness_help'},
  {id: 6, labelKey: 'setup.step_options', helpKey: 'setup.step_options_help'},
  {id: 7, labelKey: 'setup.step_confirm', helpKey: 'setup.step_confirm_help'},
  {id: 8, labelKey: 'setup.step_finish', helpKey: 'setup.step_finish_help'},
];
const PRIMARY_SOURCES = [
  {type: 'folder', labelKey: 'setup.source_folder', icon: '📁'},
  {type: 'steam', labelKey: 'setup.source_steam', icon: '🎮'},
  {type: 'heroic', labelKey: 'setup.source_heroic', icon: '🦸'},
  {type: 'lutris', labelKey: 'setup.source_lutris', icon: '🎯'},
  {type: 'gameyfin', labelKey: 'setup.source_gameyfin', icon: '📚'},
];
const MORE_SOURCES = [
  {type: 'faugus', labelKey: 'setup.source_faugus', icon: '🕹️'},
  {type: 'xbox360', labelKey: 'setup.source_xbox360', icon: '🟢'},
  {type: 'arcade', labelKey: 'setup.source_arcade', icon: '👾'},
  {type: 'scummvm', labelKey: 'setup.source_scummvm', icon: '🧭'},
  {type: 'rpcs3', labelKey: 'setup.source_rpcs3', icon: '🎲'},
  {type: 'vita3k', labelKey: 'setup.source_vita3k', icon: '📱'},
];
const MEDIA_TYPES = [
  'cover', 'background', 'screenshots', 'box_back', 'box_spine', 'box_3d',
  'clear_logo', 'fanart', 'banner', 'icon', 'title_screen', 'manual',
];

/** @type {ReturnType<typeof blankState>} */
let state = blankState();
let initialized = false;

function blankState() {
  return {
    step: 1,
    summary: null,
    checklists: null,
    sources: [],
    options: {
      include_owned_uninstalled: false,
      watch_folders: false,
      metadata_sync: false,
      media_types: ['cover', 'background'],
      region_preference: 'world',
      download_limit: 0,
      replace_existing: false,
    },
    previewId: '',
    revision: 0,
    previewDoc: null,
    previewItems: [],
    previewCursor: null,
    decisions: new Map(),
    emulatorChoices: new Map(),
    preflight: null,
    importBatchId: '',
    commitCounts: null,
    finishCounts: null,
    importedGameIds: [],
    stale: false,
    busy: false,
    runningJobId: '',
  };
}

function ensureSetupShell() {
  const body = $('setupCenterBody');
  if (!body) return;
  if (!body.querySelector('.setup-stepper')) {
    body.innerHTML = `
      <nav class="setup-stepper" aria-label="${escapeHtml(t('setup.steps_aria'))}">
        <ol class="setup-step-list" id="setupStepList"></ol>
      </nav>
      <p class="setup-help description" id="setupHelp"></p>
      <div class="setup-panel" id="setupPanel" role="region" aria-live="polite"></div>
      <div class="setup-actions dialog-actions" id="setupActions">
        <button type="button" id="setupBack">${escapeHtml(t('setup.back'))}</button>
        <button type="button" id="setupSaveClose">${escapeHtml(t('setup.save_close'))}</button>
        <button type="button" class="primary" id="setupContinue">${escapeHtml(t('setup.continue'))}</button>
      </div>
    `;
  }
  $('setupBack').onclick = () => goBack();
  $('setupContinue').onclick = () => continueStep();
  $('setupSaveClose').onclick = () => saveAndClose();
  const closeBtn = $('closeSetupCenter');
  if (closeBtn) closeBtn.onclick = () => saveAndClose();
}

function renderStepList() {
  const list = $('setupStepList');
  if (!list) return;
  const progressByStep = {
    3: state.previewDoc?.message || (state.previewDoc?.scanned_entries ? t('setup.progress_found_games', {count: state.previewDoc.scanned_entries}) : ''),
    4: state.previewItems.length ? t('setup.progress_need_review', {count: state.previewItems.filter(i => (state.decisions.get(i.candidate_id)?.action || i.intended_action) === 'review').length}) : '',
    5: state.preflight?.totals ? t('setup.progress_ready_blocked', {ready: state.preflight.totals.ready ?? 0, blocked: state.preflight.totals.blocked ?? 0}) : '',
    7: state.previewDoc?.message || '',
  };
  list.innerHTML = STEPS.map(step => {
    const progress = progressByStep[step.id] || '';
    return `
    <li class="setup-step-item${step.id === state.step ? ' active' : ''}${step.id < state.step ? ' done' : ''}" data-setup-step="${step.id}">
      <span class="setup-step-num">${step.id}</span>
      <span class="setup-step-label">${escapeHtml(t(step.labelKey))}</span>
      ${progress ? `<span class="setup-step-progress">${escapeHtml(progress)}</span>` : ''}
    </li>
  `;
  }).join('');
  const help = $('setupHelp');
  if (help) {
    const current = STEPS.find(s => s.id === state.step);
    help.textContent = current ? t(current.helpKey) : '';
  }
}

function waitForJob(jobId, {timeoutMs = 120000} = {}) {
  // SSE-driven wait (events.js) with the same deadline semantics callers had.
  if (!jobId) return Promise.resolve(null);
  return Promise.race([
    waitForJobSse(jobId, {
      fetch: async () => {
        const page = await api('/api/v2/jobs?limit=100');
        return (page.jobs || []).find(entry => entry.job_id === jobId) || null;
      },
    }),
    new Promise((resolve, reject) => setTimeout(() => reject(new Error(t('setup.job_timeout', {job: jobId}))), timeoutMs)),
  ]);
}

function emulatorChoicePayload() {
  return [...state.emulatorChoices.entries()].map(([candidate_id, choice]) => ({
    candidate_id,
    emulator_id: choice.emulator_id ?? null,
    adapter_id: choice.adapter_id ?? null,
    launch_setup: choice.launch_setup ?? null,
  }));
}

function decisionPayload(batch = state.previewItems) {
  return batch.map(item => {
    const stored = state.decisions.get(item.candidate_id);
    const emulator = state.emulatorChoices.get(item.candidate_id);
    const action = stored?.action || item.intended_action || 'import';
    const body = {
      candidate_id: item.candidate_id,
      action: action === 'review' ? 'import' : action,
    };
    if (action === 'merge' && (stored?.merge_target || item.existing_game_target?.game_id)) {
      body.merge_target = stored?.merge_target || item.existing_game_target?.game_id;
    }
    if (emulator) {
      body.emulator_id = emulator.emulator_id ?? null;
      body.adapter_id = emulator.adapter_id ?? null;
      body.launch_setup = emulator.launch_setup ?? null;
    }
    return body;
  });
}

async function loadSummary() {
  state.summary = await api('/api/v2/setup/summary');
  return state.summary;
}

async function loadChecklists() {
  try {
    state.checklists = await api('/api/v2/setup/checklists');
  } catch {
    state.checklists = null;
  }
  return state.checklists;
}

function checklistCheckLabel(check) {
  const detail = String(check?.detail || '');
  return detail || t('setup.check_unknown');
}

const CHECK_LABEL_KEYS = {
  bios: 'setup.check_bios',
  emulator: 'setup.check_emulator',
  launch: 'setup.check_launch',
  artwork: 'setup.check_artwork',
};

function renderChecklists() {
  const payload = state.checklists;
  if (!payload) return '';
  const cards = (payload.platforms || []).map(card => {
    const checks = card.checks || {};
    const rows = ['bios', 'emulator', 'launch', 'artwork'].map(id => {
      const check = checks[id];
      if (!check) return '';
      const stateName = String(check.state || 'skip');
      return `<li class="setup-check-row" data-check-id="${escapeHtml(id)}" data-check-state="${escapeHtml(stateName)}"><span class="setup-check-dot setup-check-${escapeHtml(stateName)}" aria-hidden="true"></span><span class="setup-check-name">${escapeHtml(t(CHECK_LABEL_KEYS[id]))}</span><span class="setup-check-detail">${escapeHtml(checklistCheckLabel(check))}</span></li>`;
    }).join('');
    return `<article class="setup-check-card" data-platform="${escapeHtml(card.platform)}" data-status="${escapeHtml(card.status)}">
      <header class="setup-check-head"><strong>${escapeHtml(card.platform)}</strong><span class="setup-check-badge setup-check-badge-${escapeHtml(card.status)}">${escapeHtml(t(`setup.status_${card.status}`))}</span><span class="description">${escapeHtml(t('setup.check_games', {count: card.games ?? 0}))}</span></header>
      <ul class="setup-check-list">${rows}</ul>
    </article>`;
  }).join('');
  if (!cards) return `<section class="setup-checklists" data-setup-panel="checklists"><h3 class="setup-section-title">${escapeHtml(t('setup.checklists_title'))}</h3><p class="description setup-empty">${escapeHtml(t('setup.checklists_empty'))}</p></section>`;
  const summary = payload.summary || {};
  return `<section class="setup-checklists" data-setup-panel="checklists"><h3 class="setup-section-title">${escapeHtml(t('setup.checklists_title'))}</h3><p class="description">${escapeHtml(t('setup.checklists_summary', {green: summary.green ?? 0, yellow: summary.yellow ?? 0, red: summary.red ?? 0}))}</p><div class="setup-check-grid">${cards}</div></section>`;
}

async function loadPreviewDocument() {
  if (!state.previewId) return null;
  state.previewDoc = await api(`/api/v2/setup/preview?preview_id=${encodeURIComponent(state.previewId)}`);
  state.revision = state.previewDoc.revision || state.revision;
  return state.previewDoc;
}

async function loadPreviewItems({append = false} = {}) {
  if (!state.previewId) return [];
  const cursorPart = state.previewCursor ? `&cursor=${encodeURIComponent(state.previewCursor)}` : '';
  const page = await api(`/api/v2/setup/preview/items?preview_id=${encodeURIComponent(state.previewId)}&limit=200${cursorPart}`);
  const items = page.items || [];
  state.previewItems = append ? [...state.previewItems, ...items] : items;
  state.previewCursor = page.next_cursor || null;
  state.revision = page.revision || state.revision;
  return items;
}

async function postDecisions(items) {
  if (!items.length) return;
  await api('/api/v2/setup/preview/decisions', {
    method: 'POST',
    body: JSON.stringify({preview_id: state.previewId, items}),
  });
}

async function runPreflightBatch(candidates) {
  const items = candidates.map(item => {
    const emulator = state.emulatorChoices.get(item.candidate_id) || {};
    const path = item.source?.path || '';
    return {
      game_id: null,
      candidate: {
        candidate_id: item.candidate_id,
        preview_id: state.previewId,
        path,
        platform: item.detected_platform,
        emulator_id: emulator.emulator_id ?? item.selected_emulator_id ?? null,
        adapter_id: emulator.adapter_id ?? item.selected_adapter_id ?? null,
        archive_member: null,
      },
    };
  });
  state.preflight = await api('/api/v2/launch/preflight/batch', {
    method: 'POST',
    body: JSON.stringify({items, fail_on_blocked: false}),
  });
  return state.preflight;
}

function selectedImportCandidates() {
  return state.previewItems.filter(item => {
    const action = state.decisions.get(item.candidate_id)?.action || item.intended_action;
    return action === 'import' || action === 'merge';
  });
}

function renderOverview() {
  const summary = state.summary || {};
  const next = summary.next_action || {};
  const readiness = summary.emulator_readiness || {};
  const coverage = (summary.source_coverage || []).map(row => `${escapeHtml(row.label || row.source_id)}: ${row.game_count ?? 0}`).join(' · ') || t('common.none');
  return `
    <div class="setup-overview" data-setup-panel="overview">
      <div class="setup-summary-grid">
        <div class="setup-stat" data-summary-key="library_count"><span class="setup-stat-label">${escapeHtml(t('setup.stat_library_games'))}</span><strong>${summary.library_count ?? 0}</strong></div>
        <div class="setup-stat" data-summary-key="metadata_match_percent"><span class="setup-stat-label">${escapeHtml(t('setup.stat_metadata_matched'))}</span><strong>${summary.metadata_match_percent ?? 0}%</strong></div>
        <div class="setup-stat" data-summary-key="media_gaps"><span class="setup-stat-label">${escapeHtml(t('setup.stat_media_gaps'))}</span><strong>${summary.media_gaps ?? 0}</strong></div>
        <div class="setup-stat" data-summary-key="duplicate_count"><span class="setup-stat-label">${escapeHtml(t('setup.stat_duplicates'))}</span><strong>${summary.duplicate_count ?? 0}</strong></div>
        <div class="setup-stat" data-summary-key="missing_paths"><span class="setup-stat-label">${escapeHtml(t('setup.stat_missing_paths'))}</span><strong>${summary.missing_paths ?? 0}</strong></div>
        <div class="setup-stat" data-summary-key="active_operations"><span class="setup-stat-label">${escapeHtml(t('setup.stat_active_operations'))}</span><strong>${summary.active_operations ?? 0}</strong></div>
      </div>
      <div class="setup-stat wide" data-summary-key="source_coverage">
        <span class="setup-stat-label">${escapeHtml(t('setup.stat_source_coverage'))}</span><strong>${coverage}</strong>
      </div>
      <div class="setup-stat wide" data-summary-key="emulator_readiness">
        <span class="setup-stat-label">${escapeHtml(t('setup.stat_emulator_readiness'))}</span>
        <strong>${escapeHtml(t('setup.readiness_counts', {ready: readiness.ready ?? 0, warning: readiness.warning ?? 0, blocked: readiness.blocked ?? 0}))}</strong>
      </div>
      <div class="setup-next-action" data-summary-key="next_action">
        <p class="description">${escapeHtml(t('setup.recommended_next'))}</p>
        <button type="button" class="primary setup-next-action-btn" data-next-step="${next.step || 2}">${escapeHtml(next.label || t('setup.continue_setup'))}</button>
      </div>
    </div>
    ${renderChecklists()}
  `;
}

function sourceLabel(source) {
  return source.label || (source.labelKey ? t(source.labelKey) : source.type);
}

function renderSources() {
  const selected = new Set(state.sources.map(s => s._key));
  const renderCard = source => `
    <button type="button" class="setup-source-card${selected.has(source._key) ? ' selected' : ''}" data-source-key="${escapeHtml(source._key)}">
      <span class="setup-source-icon">${source.icon || '📦'}</span>
      <span class="setup-source-label">${escapeHtml(sourceLabel(source))}</span>
    </button>
  `;
  const primary = PRIMARY_SOURCES.map(s => ({...s, _key: s.type}));
  const more = MORE_SOURCES.map(s => ({...s, _key: s.type}));
  const selectedList = state.sources.length
    ? `<ul class="setup-source-selected">${state.sources.map(s => `<li>${escapeHtml(sourceLabel(s))}${s.path ? `: ${escapeHtml(s.path)}` : ''}</li>`).join('')}</ul>`
    : `<p class="description setup-empty">${escapeHtml(t('setup.no_sources'))}</p>`;
  return `
    <div class="setup-sources" data-setup-panel="sources">
      <h3 class="setup-section-title">${escapeHtml(t('setup.detected_sources'))}</h3>
      <div class="setup-source-grid">${primary.map(renderCard).join('')}</div>
      <details class="setup-more-sources">
        <summary>${escapeHtml(t('setup.more_sources'))}</summary>
        <div class="setup-source-grid">${more.map(renderCard).join('')}</div>
      </details>
      <h3 class="setup-section-title">${escapeHtml(t('setup.selected_sources'))}</h3>
      ${selectedList}
      <label class="field setup-checkbox"><input type="checkbox" id="setupIncludeUninstalled" ${state.options.include_owned_uninstalled ? 'checked' : ''}> ${escapeHtml(t('setup.include_uninstalled'))}</label>
      <label class="field setup-checkbox"><input type="checkbox" id="setupWatchFolders" ${state.options.watch_folders ? 'checked' : ''}> ${escapeHtml(t('setup.watch_folders'))}</label>
    </div>
  `;
}

function renderPreviewRow(item) {
  const warnings = (item.warnings || []).map(w => escapeHtml(w.message || w.code || '')).join('; ');
  const choices = (item.emulator_choices || []).map(c => escapeHtml(c.label || c.adapter_id || '')).join(', ');
  const target = item.existing_game_target
    ? `${escapeHtml(item.existing_game_target.title || '')} (${escapeHtml(item.existing_game_target.game_id || '')})`
    : '—';
  const mergeDiff = (item.merge_diff || []).map(row => `
    <div class="setup-merge-field" data-merge-field="${escapeHtml(row.field)}">
      <span class="setup-merge-label">${escapeHtml(row.field)}</span>
      <span class="setup-merge-current">${escapeHtml(String(row.current ?? '—'))}</span>
      <span class="setup-merge-proposed">${escapeHtml(String(row.proposed ?? '—'))}</span>
      <span class="setup-merge-effect">${escapeHtml(row.effect || '')}</span>
    </div>
  `).join('');
  return `
    <article class="setup-preview-row" data-candidate-id="${escapeHtml(item.candidate_id)}" data-intended-action="${escapeHtml(item.intended_action || '')}">
      <div class="setup-preview-head">
        <strong>${escapeHtml(item.detected_title || t('setup.untitled'))}</strong>
        <span class="setup-preview-platform">${escapeHtml(item.detected_platform || t('common.unknown'))}</span>
        <span class="setup-preview-action">${escapeHtml(item.intended_action || '')}</span>
      </div>
      <div class="setup-preview-meta">
        <span data-preview-key="source">${escapeHtml(item.source?.path || item.source?.label || item.source?.type || '')}</span>
        <span data-preview-key="target">${target}</span>
      </div>
      ${warnings ? `<p class="setup-preview-warnings" data-preview-key="warnings">${warnings}</p>` : ''}
      ${choices ? `<p class="setup-preview-choices" data-preview-key="emulator_choices">${choices}</p>` : ''}
      ${mergeDiff ? `<div class="setup-merge-diff" data-preview-key="merge_diff">${mergeDiff}</div>` : ''}
    </article>
  `;
}

function renderPreview() {
  const doc = state.previewDoc || {};
  const counts = doc.counts || {};
  const countText = Object.entries(counts).map(([k, v]) => `${escapeHtml(k)}: ${v}`).join(' · ') || t('setup.no_counts');
  const humanMessage = doc.message ? `<p class="setup-preview-message" data-preview-message>${escapeHtml(doc.message)}</p>` : '';
  const progressCopy = doc.scanned_entries
    ? `<p class="setup-progress-copy">${escapeHtml(t('setup.progress_found_games', {count: doc.scanned_entries}))}${counts.ambiguities ? ` — ${escapeHtml(t('setup.progress_need_pick', {count: counts.ambiguities}))}` : ''}${counts.unsupported ? ` · ${escapeHtml(t('setup.progress_unsupported', {count: counts.unsupported}))}` : ''}</p>`
    : '';
  const rows = state.previewItems.map(item => {
    const chips = (item.emulator_choices || []).filter(c => c.flatpak_app_id).map(choice => `
      <button type="button" class="setup-install-chip" data-install-chip="${escapeHtml(choice.flatpak_app_id)}" data-candidate-id="${escapeHtml(item.candidate_id)}" data-adapter-id="${escapeHtml(choice.adapter_id)}" data-emulator-id="${escapeHtml(choice.emulator_id)}">${escapeHtml(t('setup.install_emulator', {name: choice.label || choice.adapter_id}))} →</button>
    `).join('');
    return renderPreviewRow(item) + (chips ? `<div class="setup-preview-chips">${chips}</div>` : '');
  }).join('');
  const status = state.busy ? `<p class="setup-status">${escapeHtml(t('setup.scan_in_progress'))}</p>` : '';
  return `
    <div class="setup-preview" data-setup-panel="preview">
      ${status}
      ${humanMessage}
      ${progressCopy}
      <p class="setup-preview-counts">${countText}</p>
      <div class="setup-preview-list">${rows || `<p class="description setup-empty">${escapeHtml(t('setup.no_preview_items'))}</p>`}</div>
      ${state.previewCursor ? `<button type="button" class="setup-load-more" id="setupLoadMorePreview">${escapeHtml(t('setup.load_more'))}</button>` : ''}
    </div>
  `;
}

function renderDecisions() {
  const rows = state.previewItems.map(item => {
    const current = state.decisions.get(item.candidate_id)?.action || item.intended_action || 'import';
    const options = ['import', 'merge', 'skip', 'exclude'];
    return `
      <div class="setup-decision-row" data-candidate-id="${escapeHtml(item.candidate_id)}">
        <div class="setup-decision-head">
          <strong>${escapeHtml(item.detected_title || '')}</strong>
          <span>${escapeHtml(item.detected_platform || '')}</span>
        </div>
        <label class="field">${escapeHtml(t('setup.action'))}
          <select class="setup-decision-action" data-candidate-id="${escapeHtml(item.candidate_id)}">
            ${options.map(opt => `<option value="${opt}"${opt === current ? ' selected' : ''}>${escapeHtml(t(`setup.action_${opt}`))}</option>`).join('')}
          </select>
        </label>
        ${(item.merge_diff || []).length ? `<div class="setup-merge-diff compact">${(item.merge_diff || []).map(row => `<div class="setup-merge-field"><span>${escapeHtml(row.field)}</span><span>${escapeHtml(String(row.current ?? ''))}</span><span>→</span><span>${escapeHtml(String(row.proposed ?? ''))}</span></div>`).join('')}</div>` : ''}
      </div>
    `;
  }).join('');
  return `
    <div class="setup-decisions" data-setup-panel="decisions">
      <p class="description">${escapeHtml(t('setup.decisions_hint'))}</p>
      <div class="setup-decision-list">${rows || `<p class="description setup-empty">${escapeHtml(t('setup.no_decisions'))}</p>`}</div>
    </div>
  `;
}

function renderReadiness() {
  const preflight = state.preflight || {};
  const totals = preflight.totals || {};
  const byPlatform = (preflight.by_platform || []).map(row => `${escapeHtml(row.platform || t('common.unknown'))}: ${row.ready ?? 0}/${row.total ?? 0}`).join(' · ');
  const progressCopy = state.previewDoc?.message ? `<p class="setup-readiness-progress">${escapeHtml(state.previewDoc.message)}</p>` : '';
  const results = (preflight.results || []).map(result => {
    const checks = (result.checks || []).map(check => `
      <li class="setup-check" data-check-code="${escapeHtml(check.code || '')}">
        <strong>${escapeHtml(check.code || '')}</strong> — ${escapeHtml(check.message || '')}
        ${(check.remediations || []).map(r => `<span class="setup-remediation">${escapeHtml(r.label || r.id || '')}</span>`).join('')}
      </li>
    `).join('');
    const item = state.previewItems.find(row => row.candidate_id === result.candidate_id);
    const choiceOptions = (item?.emulator_choices || []).map((choice, index) => {
      const selected = state.emulatorChoices.get(result.candidate_id);
      const isSelected = selected?.adapter_id === choice.adapter_id && selected?.launch_setup === 'adapter';
      return `<option value="adapter:${index}"${isSelected ? ' selected' : ''}>${escapeHtml(t('setup.use_adapter', {name: choice.label || choice.adapter_id || 'adapter'}))}</option>`;
    }).join('');
    const flatpakChoice = (item?.emulator_choices || []).find(c => c.flatpak_app_id);
    const installSelected = state.emulatorChoices.get(result.candidate_id)?.launch_setup === 'install_flatpak';
    const recommendChips = (item?.emulator_choices || []).map((choice, index) => `
      <button type="button" class="setup-emulator-chip" data-emulator-chip="${escapeHtml(choice.adapter_id)}" data-candidate-id="${escapeHtml(result.candidate_id || '')}" data-choice-index="${index}">${escapeHtml(t('setup.install_emulator', {name: choice.label || choice.adapter_id}))}</button>
    `).join('');
    return `
      <article class="setup-readiness-row" data-candidate-id="${escapeHtml(result.candidate_id || '')}" data-preflight-status="${escapeHtml(result.status || '')}">
        <div class="setup-readiness-head">
          <strong>${escapeHtml(result.candidate_id || result.game_id || t('setup.candidate'))}</strong>
          <span class="setup-readiness-status">${escapeHtml(result.status || '')}</span>
        </div>
        <ul class="setup-check-list">${checks || `<li>${escapeHtml(t('setup.no_checks'))}</li>`}</ul>
        ${recommendChips ? `<div class="setup-recommend-chips">${recommendChips}</div>` : ''}
        <label class="field">${escapeHtml(t('setup.emulator_choice'))}
          <select class="setup-emulator-choice" data-candidate-id="${escapeHtml(result.candidate_id || '')}">
            <option value="">—</option>
            ${choiceOptions}
            ${flatpakChoice ? `<option value="install_flatpak"${installSelected ? ' selected' : ''}>${escapeHtml(t('setup.install_flatpak', {id: flatpakChoice.flatpak_app_id || ''}))}</option>` : ''}
            <option value="keep_custom">${escapeHtml(t('setup.keep_custom'))}</option>
            <option value="incomplete">${escapeHtml(t('setup.incomplete'))}</option>
          </select>
        </label>
      </article>
    `;
  }).join('');
  return `
    <div class="setup-readiness" data-setup-panel="readiness">
      ${progressCopy}
      <div class="setup-preflight-totals">
        <span>${escapeHtml(t('setup.ready_count', {count: totals.ready ?? 0}))}</span>
        <span>${escapeHtml(t('setup.warning_count', {count: totals.warning ?? 0}))}</span>
        <span>${escapeHtml(t('setup.blocked_count', {count: totals.blocked ?? 0}))}</span>
      </div>
      <p class="setup-by-platform">${byPlatform || escapeHtml(t('setup.no_platform_breakdown'))}</p>
      <div class="setup-readiness-list">${results || `<p class="description setup-empty">${escapeHtml(t('setup.run_preflight_hint'))}</p>`}</div>
      <button type="button" class="setup-run-preflight" id="setupRunPreflight">${escapeHtml(t('setup.run_preflight'))}</button>
    </div>
  `;
}

const MEDIA_LABEL_KEYS = {
  cover: 'metadata.box_front', background: 'metadata.background', screenshots: 'metadata.screenshots',
  box_back: 'metadata.box_back', box_spine: 'metadata.box_spine', box_3d: 'metadata.box_3d',
  clear_logo: 'metadata.clear_logo', fanart: 'metadata.fanart', banner: 'metadata.banner',
  icon: 'metadata.icon', title_screen: 'metadata.title_screen', manual: 'metadata.manual',
};

function renderOptions() {
  const mediaChecks = MEDIA_TYPES.map(type => `
    <label class="setup-media-type"><input type="checkbox" data-media-type="${type}" ${state.options.media_types.includes(type) ? 'checked' : ''}> ${escapeHtml(t(MEDIA_LABEL_KEYS[type] || type))}</label>
  `).join('');
  return `
    <div class="setup-options" data-setup-panel="options">
      <label class="field setup-checkbox"><input type="checkbox" id="setupMetadataSync" ${state.options.metadata_sync ? 'checked' : ''}> ${escapeHtml(t('setup.metadata_sync'))}</label>
      <fieldset class="setup-fieldset">
        <legend>${escapeHtml(t('setup.media_types_legend'))}</legend>
        <div class="setup-media-grid">${mediaChecks}</div>
      </fieldset>
      <label class="field">${escapeHtml(t('setup.region_preference'))}
        <select id="setupRegionPreference">
          <option value="world"${state.options.region_preference === 'world' ? ' selected' : ''}>${escapeHtml(t('setup.region_world'))}</option>
          <option value="us"${state.options.region_preference === 'us' ? ' selected' : ''}>${escapeHtml(t('setup.region_us'))}</option>
          <option value="eu"${state.options.region_preference === 'eu' ? ' selected' : ''}>${escapeHtml(t('setup.region_eu'))}</option>
          <option value="jp"${state.options.region_preference === 'jp' ? ' selected' : ''}>${escapeHtml(t('setup.region_jp'))}</option>
        </select>
      </label>
      <label class="field">${escapeHtml(t('setup.download_limit'))}
        <input type="number" id="setupDownloadLimit" min="0" value="${state.options.download_limit || 0}">
      </label>
      <label class="field setup-checkbox"><input type="checkbox" id="setupReplaceExisting" ${state.options.replace_existing ? 'checked' : ''}> ${escapeHtml(t('setup.replace_existing'))}</label>
    </div>
  `;
}

function renderConfirm() {
  const doc = state.previewDoc || {};
  const humanMessage = doc.message ? `<p class="setup-confirm-message">${escapeHtml(doc.message)}</p>` : '';
  const staleNudge = state.stale
    ? `<p class="setup-stale" role="alert">${escapeHtml(t('setup.stale_alert'))}</p><p class="description setup-revalidate-nudge">${escapeHtml(t('setup.stale_nudge'))}</p>`
    : `<p class="description setup-revalidate-hint">${escapeHtml(t('setup.revalidate_hint'))}</p>`;
  return `
    <div class="setup-confirm" data-setup-panel="confirm">
      ${humanMessage}
      ${staleNudge}
      <p class="description">${escapeHtml(t('setup.revalidate_description'))}</p>
      <div class="setup-confirm-summary">
        <p>${escapeHtml(t('setup.preview_label'))} <strong>${escapeHtml(state.previewId || '—')}</strong> ${escapeHtml(t('setup.revision_label'))} <strong>${doc.revision ?? state.revision}</strong></p>
        <p>${escapeHtml(t('setup.candidates_label'))} <strong>${state.previewItems.length}</strong></p>
        <p>${escapeHtml(t('setup.import_actions_label'))} <strong>${selectedImportCandidates().length}</strong></p>
      </div>
      ${state.stale ? `<button type="button" class="primary setup-revalidate" id="setupRevalidate">${escapeHtml(t('setup.revalidate'))}</button>` : `<button type="button" class="icon-button setup-revalidate" id="setupRevalidate">${escapeHtml(t('setup.revalidate'))}</button>`}
      ${state.busy ? `<p class="setup-status">${escapeHtml(t('setup.operation_in_progress'))}</p>` : ''}
    </div>
  `;
}

function renderFinish() {
  const counts = state.finishCounts || state.commitCounts || {};
  const keys = ['added', 'merged', 'skipped', 'unmatched', 'media_complete', 'launch_ready', 'warning', 'failed'];
  const countCards = keys.map(key => `
    <div class="setup-finish-stat" data-finish-key="${key}">
      <span class="setup-stat-label">${escapeHtml(t(`setup.finish_${key}`))}</span>
      <strong>${counts[key] ?? 0}</strong>
    </div>
  `).join('');
  const tryThese = [
    {id: 'pick-game', labelKey: 'setup.try_pick_game', hintKey: 'setup.try_pick_game_hint'},
    {id: 'radio', labelKey: 'setup.try_radio', hintKey: 'setup.try_radio_hint'},
    {id: 'arcade', labelKey: 'setup.try_arcade', hintKey: 'setup.try_arcade_hint'},
    {id: 'save-folder', labelKey: 'setup.try_save_folder', hintKey: 'setup.try_save_folder_hint'},
  ].map(item => `
    <button type="button" class="setup-try-card" data-setup-try="${item.id}">
      <strong>${escapeHtml(t(item.labelKey))}</strong>
      <span class="description">${escapeHtml(t(item.hintKey))}</span>
    </button>
  `).join('');
  return `
    <div class="setup-finish" data-setup-panel="finish">
      <div class="setup-finish-counts">${countCards}</div>
      <section class="setup-try-these" data-setup-panel="try-these">
        <h3 class="setup-section-title">${escapeHtml(t('setup.try_these_title'))}</h3>
        <div class="setup-try-grid">${tryThese}</div>
      </section>
      <div class="setup-finish-actions">
        <button type="button" class="primary" id="setupViewImported">${escapeHtml(t('setup.view_imported'))}</button>
        <button type="button" id="setupReviewMetadata">${escapeHtml(t('setup.review_unmatched'))}</button>
        <button type="button" id="setupFixLaunch">${escapeHtml(t('setup.fix_launch_blockers'))}</button>
        <button type="button" id="setupRetryWork">${escapeHtml(t('setup.retry_failed'))}</button>
        <button type="button" id="setupOpenActivity">${escapeHtml(t('setup.open_activity'))}</button>
      </div>
    </div>
  `;
}

async function runTryThese(id) {
  if (id === 'pick-game') {
    saveAndClose();
    document.dispatchEvent(new CustomEvent('app:palette-surprise'));
    return;
  }
  if (id === 'radio') {
    try {
      await api('/api/v2/insights/radio/refresh', {method: 'POST', body: '{}'});
      notify('success', t('setup.try_radio_done'));
    } catch (error) {
      notify('error', error.message);
    }
    return;
  }
  if (id === 'arcade') {
    saveAndClose();
    document.dispatchEvent(new CustomEvent('app:palette-open-arcade-room'));
    return;
  }
  if (id === 'save-folder') {
    saveAndClose();
    document.dispatchEvent(new CustomEvent('app:palette-open-settings'));
    return;
  }
}

function renderPanel() {
  renderStepList();
  const panel = $('setupPanel');
  if (!panel) return;
  const renderers = {
    1: renderOverview,
    2: renderSources,
    3: renderPreview,
    4: renderDecisions,
    5: renderReadiness,
    6: renderOptions,
    7: renderConfirm,
    8: renderFinish,
  };
  panel.innerHTML = renderers[state.step]?.() || '';
  bindPanelEvents();
  const back = $('setupBack');
  const cont = $('setupContinue');
  if (back) {
    back.disabled = state.step <= 1 || state.busy;
    back.textContent = t('setup.back');
  }
  const saveClose = $('setupSaveClose');
  if (saveClose) saveClose.textContent = t('setup.save_close');
  if (cont) {
    cont.disabled = state.busy;
    cont.textContent = state.step >= 8 ? t('common.done') : t('setup.continue');
  }
}

function bindPanelEvents() {
  document.querySelectorAll('[data-setup-try]').forEach(btn => {
    btn.onclick = () => runTryThese(btn.dataset.setupTry);
  });
  document.querySelectorAll('.setup-next-action-btn').forEach(btn => {
    btn.onclick = () => {
      const step = Number(btn.dataset.nextStep || 2);
      state.step = step;
      renderPanel();
    };
  });
  document.querySelectorAll('.setup-source-card').forEach(btn => {
    btn.onclick = () => addSource(btn.dataset.sourceKey);
  });
  const includeUninstalled = $('setupIncludeUninstalled');
  if (includeUninstalled) includeUninstalled.onchange = () => { state.options.include_owned_uninstalled = includeUninstalled.checked; };
  const watchFolders = $('setupWatchFolders');
  if (watchFolders) watchFolders.onchange = () => { state.options.watch_folders = watchFolders.checked; };
  const loadMore = $('setupLoadMorePreview');
  if (loadMore) loadMore.onclick = async () => { await loadPreviewItems({append: true}); renderPanel(); };
  document.querySelectorAll('.setup-decision-action').forEach(select => {
    select.onchange = () => {
      const id = select.dataset.candidateId;
      const existing = state.decisions.get(id) || {};
      state.decisions.set(id, {...existing, action: select.value});
    };
  });
  const runPreflight = $('setupRunPreflight');
  if (runPreflight) runPreflight.onclick = () => runReadinessStep({manual: true});
  document.querySelectorAll('.setup-emulator-choice').forEach(select => {
    select.onchange = () => applyEmulatorChoice(select.dataset.candidateId, select.value);
  });
  document.querySelectorAll('[data-install-chip]').forEach(btn => {
    btn.onclick = () => applyEmulatorChoice(btn.dataset.candidateId, `adapter:${[...(state.previewItems.find(i => i.candidate_id === btn.dataset.candidateId)?.emulator_choices || [])].findIndex(c => c.adapter_id === btn.dataset.adapterId)}`);
  });
  document.querySelectorAll('[data-emulator-chip]').forEach(btn => {
    btn.onclick = () => applyEmulatorChoice(btn.dataset.candidateId, `adapter:${btn.dataset.choiceIndex}`);
  });
  const metadataSync = $('setupMetadataSync');
  if (metadataSync) metadataSync.onchange = () => { state.options.metadata_sync = metadataSync.checked; };
  const replaceExisting = $('setupReplaceExisting');
  if (replaceExisting) replaceExisting.onchange = () => { state.options.replace_existing = replaceExisting.checked; };
  const region = $('setupRegionPreference');
  if (region) region.onchange = () => { state.options.region_preference = region.value; };
  const limit = $('setupDownloadLimit');
  if (limit) limit.onchange = () => { state.options.download_limit = Number(limit.value) || 0; };
  document.querySelectorAll('[data-media-type]').forEach(input => {
    input.onchange = () => {
      const types = new Set(state.options.media_types);
      if (input.checked) types.add(input.dataset.mediaType);
      else types.delete(input.dataset.mediaType);
      state.options.media_types = [...types];
    };
  });
  const revalidate = $('setupRevalidate');
  if (revalidate) revalidate.onclick = () => revalidatePreview();
  const viewImported = $('setupViewImported');
  if (viewImported) viewImported.onclick = () => viewImportedGames();
  const reviewMeta = $('setupReviewMetadata');
  if (reviewMeta) reviewMeta.onclick = () => { openMatchReview({import_batch_id: state.importBatchId}); };
  const fixLaunch = $('setupFixLaunch');
  if (fixLaunch) fixLaunch.onclick = () => { state.step = 5; renderPanel(); };
  const retryWork = $('setupRetryWork');
  if (retryWork) retryWork.onclick = () => openActivity();
  const openAct = $('setupOpenActivity');
  if (openAct) openAct.onclick = () => openActivity();
}

async function addSource(type) {
  const def = [...PRIMARY_SOURCES, ...MORE_SOURCES].find(s => s.type === type);
  if (!def) return;
  if (type === 'folder') {
    const path = await nativePickFolder(t('setup.pick_folder_path'));
    if (!path) return;
    const recursive = await promptChoice({
      title: t('setup.folder_recursion'),
      message: t('setup.folder_recursion_message'),
      choices: [{value: 'yes', label: t('setup.yes_recurse')}, {value: 'no', label: t('setup.top_level_only')}],
      defaultValue: 'yes',
    });
    state.sources.push({type: 'folder', id: path, path, labelKey: def.labelKey, recursive: recursive !== 'no'});
    renderPanel();
    return;
  }
  if (type === 'xbox360') {
    const path = await nativePickFolder(t('setup.pick_xbox_path'));
    if (!path) return;
    state.sources.push({type: 'xbox360', id: path, path, labelKey: def.labelKey});
    renderPanel();
    return;
  }
  if (type === 'arcade') {
    const path = await nativePickFolder(t('setup.pick_arcade_path'));
    if (!path) return;
    const setType = await promptChoice({
      title: t('setup.arcade_set_type'),
      message: t('setup.arcade_set_type_message'),
      choices: [{value: 'MAME', label: 'MAME'}, {value: 'FinalBurn Neo', label: 'FinalBurn Neo'}],
      defaultValue: 'MAME',
    });
    if (!setType) return;
    const dat = (await nativePickFile(t('setup.pick_dat_path'))) ?? '';
    const command = (await promptInput({
      title: t('setup.launch_command'),
      message: t('setup.launch_command_message'),
      defaultValue: '',
    })) ?? '';
    state.sources.push({
      type: 'arcade', id: path, path, labelKey: def.labelKey,
      set_type: setType, dat_path: dat, command,
      adapter_id: setType === 'FinalBurn Neo' ? 'fbneo' : 'mame',
    });
    renderPanel();
    return;
  }
  if (['scummvm', 'rpcs3', 'vita3k'].includes(type)) {
    const path = await nativePickFolder(t('setup.pick_source_path', {source: sourceLabel(def)}));
    if (!path) return;
    state.sources.push({type, id: path, path, labelKey: def.labelKey});
    renderPanel();
    return;
  }
  if (type === 'faugus') {
    state.sources.push({type: 'faugus', id: 'faugus', labelKey: def.labelKey});
    renderPanel();
    return;
  }
  state.sources.push({type, id: type, labelKey: def.labelKey});
  renderPanel();
}

async function applyEmulatorChoice(candidateId, value) {
  const item = state.previewItems.find(row => row.candidate_id === candidateId);
  if (!item) return;
  if (!value) {
    state.emulatorChoices.delete(candidateId);
    return;
  }
  if (value === 'keep_custom') {
    state.emulatorChoices.set(candidateId, {emulator_id: null, adapter_id: null, launch_setup: 'keep_custom'});
  } else if (value === 'incomplete') {
    state.emulatorChoices.set(candidateId, {emulator_id: null, adapter_id: null, launch_setup: 'incomplete'});
  } else if (value === 'install_flatpak') {
    const choice = (item.emulator_choices || []).find(c => c.flatpak_app_id);
    state.emulatorChoices.set(candidateId, {
      emulator_id: choice?.emulator_id ?? null,
      adapter_id: choice?.adapter_id ?? null,
      launch_setup: 'install_flatpak',
      flatpak_app_id: choice?.flatpak_app_id ?? null,
    });
    await installFlatpakIfNeeded(candidateId);
  } else if (value.startsWith('adapter:')) {
    const index = Number(value.split(':')[1]);
    const choice = (item.emulator_choices || [])[index];
    if (choice) {
      state.emulatorChoices.set(candidateId, {
        emulator_id: choice.emulator_id,
        adapter_id: choice.adapter_id,
        launch_setup: 'adapter',
      });
    }
  }
  await postDecisions([{
    candidate_id: candidateId,
    action: state.decisions.get(candidateId)?.action || item.intended_action || 'import',
    emulator_id: state.emulatorChoices.get(candidateId)?.emulator_id ?? null,
    adapter_id: state.emulatorChoices.get(candidateId)?.adapter_id ?? null,
    launch_setup: state.emulatorChoices.get(candidateId)?.launch_setup ?? null,
  }]);
  await runPreflightBatch(selectedImportCandidates());
  renderPanel();
}

async function installFlatpakIfNeeded(candidateId) {
  const choice = state.emulatorChoices.get(candidateId);
  if (!choice || choice.launch_setup !== 'install_flatpak' || !choice.flatpak_app_id) return;
  try {
    const result = await api('/api/emulators/install', {
      method: 'POST',
      body: JSON.stringify({app_id: choice.flatpak_app_id}),
    });
    if (result.job_id) await waitForJob(result.job_id);
  } catch (error) {
    notify('warning', t('setup.flatpak_failed', {message: error.message}));
  }
}

async function startPreviewScan() {
  if (!state.sources.length) throw new Error(t('setup.add_source_first'));
  state.busy = true;
  state.runningJobId = '';
  renderPanel();
  const payload = {
    sources: state.sources.map(source => {
      const copy = {...source};
      delete copy.label;
      delete copy.labelKey;
      delete copy._key;
      if (copy.type === 'folder') {
        copy.recursive = copy.recursive !== false;
      }
      return copy;
    }),
    options: {
      include_owned_uninstalled: state.options.include_owned_uninstalled,
    },
  };
  const accepted = await api('/api/v2/setup/preview', {method: 'POST', body: JSON.stringify(payload)});
  state.previewId = accepted.preview_id;
  state.revision = accepted.revision;
  state.runningJobId = accepted.job_id || '';
  if (accepted.job_id) await waitForJob(accepted.job_id);
  await loadPreviewDocument();
  await loadPreviewItems();
  state.busy = false;
  state.runningJobId = '';
}

async function saveDecisions() {
  const batches = [];
  for (let i = 0; i < state.previewItems.length; i += 200) {
    batches.push(state.previewItems.slice(i, i + 200));
  }
  for (const batch of batches) await postDecisions(decisionPayload(batch));
}

async function runReadinessStep({manual = false} = {}) {
  if (!state.previewId) return;
  if (!manual && !selectedImportCandidates().length) return;
  state.busy = true;
  renderPanel();
  try {
    await saveDecisions();
    await runPreflightBatch(selectedImportCandidates());
  } finally {
    state.busy = false;
    renderPanel();
  }
}

async function revalidatePreview() {
  state.busy = true;
  state.stale = false;
  renderPanel();
  try {
    const accepted = await api('/api/v2/setup/preview/revalidate', {
      method: 'POST',
      body: JSON.stringify({preview_id: state.previewId}),
    });
    if (accepted.job_id) await waitForJob(accepted.job_id);
    await loadPreviewDocument();
    if (!state.previewDoc?.revalidated) throw new Error(t('setup.not_revalidated'));
  } catch (error) {
    if (String(error.message || '').includes('PREVIEW_STALE') || String(error.message || '').includes('PREVIEW_LIBRARY_CHANGED')) {
      state.stale = true;
      notify('warning', error.message);
    } else throw error;
  } finally {
    state.busy = false;
    renderPanel();
  }
}

async function commitPreview() {
  state.busy = true;
  renderPanel();
  try {
    await saveDecisions();
    const accepted = await api('/api/v2/setup/commit', {
      method: 'POST',
      body: JSON.stringify({
        preview_id: state.previewId,
        revision: state.revision,
        options: {
          watch_folders: state.options.watch_folders,
          replace_existing: state.options.replace_existing,
          region_preference: state.options.region_preference,
          download_limit: state.options.download_limit,
        },
        emulator_choices: emulatorChoicePayload(),
      }),
    });
    state.importBatchId = accepted.import_batch_id || '';
    if (accepted.job_id) {
      state.runningJobId = accepted.job_id;
      const job = await waitForJob(accepted.job_id);
      state.commitCounts = job?.result || {};
    }
    await runFinishPipeline();
  } catch (error) {
    if (String(error.message || '').includes('PREVIEW_STALE') || String(error.message || '').includes('PREVIEW_LIBRARY_CHANGED')) {
      state.stale = true;
      notify('warning', error.message);
      state.step = 7;
    } else {
      notify('error', error.message);
    }
  } finally {
    state.busy = false;
    state.runningJobId = '';
    renderPanel();
  }
}

async function runFinishPipeline() {
  const finishCounts = {...(state.commitCounts || {})};
  if (state.options.metadata_sync) {
    try {
      const sync = await api('/api/metadata/sync', {method: 'POST', body: '{}'});
      if (sync.job_id) await waitForJob(sync.job_id);
    } catch (error) {
      notify('warning', t('setup.metadata_sync_failed', {message: error.message}));
    }
  }
  if (state.importBatchId) {
    try {
      const preview = await api('/api/v2/metadata/matches/preview', {
        method: 'POST',
        body: JSON.stringify({game_ids: null, import_batch_id: state.importBatchId}),
      });
      if (preview.job_id) await waitForJob(preview.job_id);
      const doc = await api(`/api/v2/metadata/matches/preview?preview_id=${encodeURIComponent(preview.preview_id)}`);
      finishCounts.unmatched = (doc.counts?.unmatched ?? 0) + (doc.counts?.exact_review ?? 0) + (doc.counts?.likely ?? 0) + (doc.counts?.possible ?? 0);
      const needsReview = (doc.counts?.exact_review || 0) + (doc.counts?.likely || 0) + (doc.counts?.possible || 0) + (doc.counts?.unmatched || 0);
      if (needsReview > 0) {
        state._openMatchReview = true;
      }
    } catch (error) {
      notify('warning', t('setup.metadata_preview_failed', {message: error.message}));
    }
  }
  if (state.options.media_types.length) {
    try {
      await refresh();
      const gameIds = AppState.games
        .filter(game => game.import_batch_id === state.importBatchId)
        .map(game => String(game.game_id || game.id))
        .filter(Boolean);
      state.importedGameIds = gameIds;
      if (gameIds.length) {
        const bulk = await api('/api/media/bulk', {
          method: 'POST',
          body: JSON.stringify({
            game_ids: gameIds,
            media: state.options.media_types,
            overwrite: state.options.replace_existing,
          }),
        });
        if (bulk.job_id) await waitForJob(bulk.job_id);
        finishCounts.media_complete = gameIds.length;
      }
    } catch (error) {
      notify('warning', t('setup.media_failed', {message: error.message}));
    }
  }
  try {
    const summary = await api('/api/v2/setup/summary');
    state.summary = summary;
    await refresh();
    const imported = AppState.games.filter(game => game.import_batch_id === state.importBatchId);
    state.importedGameIds = imported.map(game => String(game.game_id || game.id)).filter(Boolean);
    if (state.importedGameIds.length) {
      const batch = await api('/api/v2/launch/preflight/batch', {
        method: 'POST',
        body: JSON.stringify({
          items: state.importedGameIds.map(game_id => ({game_id, candidate: null})),
          fail_on_blocked: false,
        }),
      });
      finishCounts.launch_ready = batch.totals?.ready ?? 0;
      finishCounts.warning = batch.totals?.warning ?? 0;
      finishCounts.failed = batch.totals?.blocked ?? 0;
    }
  } catch (error) {
    notify('warning', t('setup.summary_failed', {message: error.message}));
  }
  state.finishCounts = finishCounts;
  try {
    AppState.appSettings = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({...AppState.appSettings, welcome_completed: true}),
    });
  } catch (error) {
    notify('warning', t('setup.welcome_save_failed', {message: error.message}));
  }
  state.step = 8;
}

function viewImportedGames() {
  resetQuery();
  AppState.importBatchId = state.importBatchId || '';
  refresh();
  saveAndClose();
}

async function continueStep() {
  if (state.busy) return;
  try {
    if (state.step === 1) {
      await loadSummary();
      state.step = Number(state.summary?.next_action?.step || 2);
    } else if (state.step === 2) {
      if (!state.sources.length) throw new Error(t('setup.select_source'));
      state.step = 3;
      await startPreviewScan();
    } else if (state.step === 3) {
      state.step = 4;
    } else if (state.step === 4) {
      await saveDecisions();
      state.step = 5;
      await runReadinessStep();
    } else if (state.step === 5) {
      state.step = 6;
    } else if (state.step === 6) {
      state.step = 7;
    } else if (state.step === 7) {
      if (state.stale) {
        await revalidatePreview();
        return;
      }
      await revalidatePreview();
      await commitPreview();
      if (state._openMatchReview) {
        openMatchReview({import_batch_id: state.importBatchId});
        state._openMatchReview = false;
      }
    } else if (state.step === 8) {
      saveAndClose();
      return;
    }
    renderPanel();
  } catch (error) {
    notify('error', error.message);
    state.busy = false;
    renderPanel();
  }
}

function goBack() {
  if (state.step > 1 && !state.busy) {
    state.step -= 1;
    renderPanel();
  }
}

function saveAndClose() {
  const dialog = $('setupCenter');
  if (dialog?.open) closeDialog(dialog);
}

export async function openSetupCenter({step = 1} = {}) {
  ensureSetupShell();
  if (!state.summary) {
    try { await loadSummary(); } catch { /* offline overview */ }
  }
  if (!state.checklists) await loadChecklists();
  state.step = step;
  renderPanel();
  const dialog = $('setupCenter');
  if (dialog && !dialog.open) openDialog(dialog, document.getElementById('setupLibraryButton') || document.activeElement);
}

function initSetupCenter() {
  if (initialized) return;
  initialized = true;
  ensureSetupShell();
  const dialog = $('setupCenter');
  if (dialog) {
    dialog.addEventListener('close', () => { /* close does not cancel operations */ });
  }
  renderPanel();
}

// P8: the setup center is entirely JS-rendered, so a locale change only
// becomes visible after a re-render. It is cheap and keeps step state.
onLocaleChange(() => {
  if ($('setupCenter')?.open) renderPanel();
});

queueMicrotask(() => { initSetupCenter(); });

export { SUMMARY_KEYS, STEPS, waitForJob, emulatorChoicePayload, decisionPayload };
