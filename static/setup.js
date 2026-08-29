import { $, escapeHtml } from './util.js';
import { api, notify, AppState, nativePickFolder, nativePickFile, resetQuery } from './state.js';
import { promptChoice, promptInput } from './dialogs.js';
import { openMatchReview } from './metadata.js';
import { openActivity } from './activity.js';
import { refresh } from './library.js';

const SUMMARY_KEYS = [
  'library_count', 'source_coverage', 'metadata_match_percent', 'media_gaps',
  'duplicate_count', 'missing_paths', 'emulator_readiness', 'active_operations', 'next_action',
];
const STEPS = [
  {id: 1, label: 'Overview', help: 'Review library health and the recommended next step.'},
  {id: 2, label: 'Sources', help: 'Add folders, storefronts, and specialized sources to scan.'},
  {id: 3, label: 'Scan', help: 'Run a side-effect-free preview scan via the v2 setup API.'},
  {id: 4, label: 'Decisions', help: 'Resolve ambiguities, merges, and skips before import.'},
  {id: 5, label: 'Readiness', help: 'Launch Doctor preflight for emulator and path readiness.'},
  {id: 6, label: 'Options', help: 'Metadata, media, region, and import behavior.'},
  {id: 7, label: 'Confirm', help: 'Revalidate the preview and commit to your library.'},
  {id: 8, label: 'Finish', help: 'Enrich imported games and review completion counts.'},
];
const PRIMARY_SOURCES = [
  {type: 'folder', label: 'Folder', icon: '📁'},
  {type: 'steam', label: 'Steam', icon: '🎮'},
  {type: 'heroic', label: 'Heroic', icon: '🦸'},
  {type: 'lutris', label: 'Lutris', icon: '🎯'},
  {type: 'gameyfin', label: 'Gameyfin', icon: '📚'},
];
const MORE_SOURCES = [
  {type: 'faugus', label: 'Faugus', icon: '🕹️'},
  {type: 'xbox360', label: 'Xbox 360', icon: '🟢'},
  {type: 'arcade', label: 'Arcade', icon: '👾'},
  {type: 'scummvm', label: 'ScummVM', icon: '🧭'},
  {type: 'rpcs3', label: 'RPCS3', icon: '🎲'},
  {type: 'vita3k', label: 'Vita3K', icon: '📱'},
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
      <nav class="setup-stepper" aria-label="Setup steps">
        <ol class="setup-step-list" id="setupStepList"></ol>
      </nav>
      <p class="setup-help description" id="setupHelp"></p>
      <div class="setup-panel" id="setupPanel" role="region" aria-live="polite"></div>
      <div class="setup-actions dialog-actions" id="setupActions">
        <button type="button" id="setupBack">Back</button>
        <button type="button" id="setupSaveClose">Save and close</button>
        <button type="button" class="primary" id="setupContinue">Continue</button>
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
    3: state.previewDoc?.message || (state.previewDoc?.scanned_entries ? `Found ${state.previewDoc.scanned_entries} games` : ''),
    4: state.previewItems.length ? `${state.previewItems.filter(i => (state.decisions.get(i.candidate_id)?.action || i.intended_action) === 'review').length} need review` : '',
    5: state.preflight?.totals ? `Ready ${state.preflight.totals.ready ?? 0} · Blocked ${state.preflight.totals.blocked ?? 0}` : '',
    7: state.previewDoc?.message || '',
  };
  list.innerHTML = STEPS.map(step => {
    const progress = progressByStep[step.id] || '';
    return `
    <li class="setup-step-item${step.id === state.step ? ' active' : ''}${step.id < state.step ? ' done' : ''}" data-setup-step="${step.id}">
      <span class="setup-step-num">${step.id}</span>
      <span class="setup-step-label">${escapeHtml(step.label)}</span>
      ${progress ? `<span class="setup-step-progress">${escapeHtml(progress)}</span>` : ''}
    </li>
  `;
  }).join('');
  const help = $('setupHelp');
  if (help) help.textContent = STEPS.find(s => s.id === state.step)?.help || '';
}

async function waitForJob(jobId, {timeoutMs = 120000} = {}) {
  if (!jobId) return null;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const page = await api('/api/v2/jobs?limit=100');
    const job = (page.jobs || []).find(entry => entry.job_id === jobId);
    if (job && !['queued', 'running', 'cancelling'].includes(job.state)) return job;
    await new Promise(r => setTimeout(r, 250));
  }
  throw new Error(`Timed out waiting for job ${jobId}`);
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
  const coverage = (summary.source_coverage || []).map(row => `${escapeHtml(row.label || row.source_id)}: ${row.game_count ?? 0}`).join(' · ') || 'None';
  return `
    <div class="setup-overview" data-setup-panel="overview">
      <div class="setup-summary-grid">
        <div class="setup-stat" data-summary-key="library_count"><span class="setup-stat-label">Library games</span><strong>${summary.library_count ?? 0}</strong></div>
        <div class="setup-stat" data-summary-key="metadata_match_percent"><span class="setup-stat-label">Metadata matched</span><strong>${summary.metadata_match_percent ?? 0}%</strong></div>
        <div class="setup-stat" data-summary-key="media_gaps"><span class="setup-stat-label">Media gaps</span><strong>${summary.media_gaps ?? 0}</strong></div>
        <div class="setup-stat" data-summary-key="duplicate_count"><span class="setup-stat-label">Duplicates</span><strong>${summary.duplicate_count ?? 0}</strong></div>
        <div class="setup-stat" data-summary-key="missing_paths"><span class="setup-stat-label">Missing paths</span><strong>${summary.missing_paths ?? 0}</strong></div>
        <div class="setup-stat" data-summary-key="active_operations"><span class="setup-stat-label">Active operations</span><strong>${summary.active_operations ?? 0}</strong></div>
      </div>
      <div class="setup-stat wide" data-summary-key="source_coverage">
        <span class="setup-stat-label">Source coverage</span><strong>${coverage}</strong>
      </div>
      <div class="setup-stat wide" data-summary-key="emulator_readiness">
        <span class="setup-stat-label">Emulator readiness</span>
        <strong>Ready ${readiness.ready ?? 0} · Warning ${readiness.warning ?? 0} · Blocked ${readiness.blocked ?? 0}</strong>
      </div>
      <div class="setup-next-action" data-summary-key="next_action">
        <p class="description">Recommended next step</p>
        <button type="button" class="primary setup-next-action-btn" data-next-step="${next.step || 2}">${escapeHtml(next.label || 'Continue setup')}</button>
      </div>
    </div>
  `;
}

function renderSources() {
  const selected = new Set(state.sources.map(s => s._key));
  const renderCard = source => `
    <button type="button" class="setup-source-card${selected.has(source._key) ? ' selected' : ''}" data-source-key="${escapeHtml(source._key)}">
      <span class="setup-source-icon">${source.icon || '📦'}</span>
      <span class="setup-source-label">${escapeHtml(source.label)}</span>
    </button>
  `;
  const primary = PRIMARY_SOURCES.map(s => ({...s, _key: s.type}));
  const more = MORE_SOURCES.map(s => ({...s, _key: s.type}));
  const selectedList = state.sources.length
    ? `<ul class="setup-source-selected">${state.sources.map(s => `<li>${escapeHtml(s.label || s.type)}${s.path ? `: ${escapeHtml(s.path)}` : ''}</li>`).join('')}</ul>`
    : '<p class="description setup-empty">No sources selected yet.</p>';
  return `
    <div class="setup-sources" data-setup-panel="sources">
      <h3 class="setup-section-title">Detected &amp; common sources</h3>
      <div class="setup-source-grid">${primary.map(renderCard).join('')}</div>
      <details class="setup-more-sources">
        <summary>More sources</summary>
        <div class="setup-source-grid">${more.map(renderCard).join('')}</div>
      </details>
      <h3 class="setup-section-title">Selected sources</h3>
      ${selectedList}
      <label class="field setup-checkbox"><input type="checkbox" id="setupIncludeUninstalled" ${state.options.include_owned_uninstalled ? 'checked' : ''}> Include owned but uninstalled storefront games</label>
      <label class="field setup-checkbox"><input type="checkbox" id="setupWatchFolders" ${state.options.watch_folders ? 'checked' : ''}> Add folder to watched folders after import</label>
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
        <strong>${escapeHtml(item.detected_title || 'Untitled')}</strong>
        <span class="setup-preview-platform">${escapeHtml(item.detected_platform || 'Unknown')}</span>
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
  const countText = Object.entries(counts).map(([k, v]) => `${escapeHtml(k)}: ${v}`).join(' · ') || 'No counts yet';
  const humanMessage = doc.message ? `<p class="setup-preview-message" data-preview-message>${escapeHtml(doc.message)}</p>` : '';
  const progressCopy = doc.scanned_entries
    ? `<p class="setup-progress-copy">Found ${doc.scanned_entries} games${counts.ambiguities ? ` — ${counts.ambiguities} need your pick` : ''}${counts.unsupported ? ` · ${counts.unsupported} unsupported` : ''}</p>`
    : '';
  const rows = state.previewItems.map(item => {
    const chips = (item.emulator_choices || []).filter(c => c.flatpak_app_id).map(choice => `
      <button type="button" class="setup-install-chip" data-install-chip="${escapeHtml(choice.flatpak_app_id)}" data-candidate-id="${escapeHtml(item.candidate_id)}" data-adapter-id="${escapeHtml(choice.adapter_id)}" data-emulator-id="${escapeHtml(choice.emulator_id)}">Install ${escapeHtml(choice.label || choice.adapter_id)} →</button>
    `).join('');
    return renderPreviewRow(item) + (chips ? `<div class="setup-preview-chips">${chips}</div>` : '');
  }).join('');
  const status = state.busy ? '<p class="setup-status">Scan in progress…</p>' : '';
  return `
    <div class="setup-preview" data-setup-panel="preview">
      ${status}
      ${humanMessage}
      ${progressCopy}
      <p class="setup-preview-counts">${countText}</p>
      <div class="setup-preview-list">${rows || '<p class="description setup-empty">No preview items yet. Continue to start a scan.</p>'}</div>
      ${state.previewCursor ? '<button type="button" class="setup-load-more" id="setupLoadMorePreview">Load more</button>' : ''}
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
        <label class="field">Action
          <select class="setup-decision-action" data-candidate-id="${escapeHtml(item.candidate_id)}">
            ${options.map(opt => `<option value="${opt}"${opt === current ? ' selected' : ''}>${opt}</option>`).join('')}
          </select>
        </label>
        ${(item.merge_diff || []).length ? `<div class="setup-merge-diff compact">${(item.merge_diff || []).map(row => `<div class="setup-merge-field"><span>${escapeHtml(row.field)}</span><span>${escapeHtml(String(row.current ?? ''))}</span><span>→</span><span>${escapeHtml(String(row.proposed ?? ''))}</span></div>`).join('')}</div>` : ''}
      </div>
    `;
  }).join('');
  return `
    <div class="setup-decisions" data-setup-panel="decisions">
      <p class="description">Default safe additions import automatically. Resolve ambiguities explicitly.</p>
      <div class="setup-decision-list">${rows || '<p class="description setup-empty">No decisions needed.</p>'}</div>
    </div>
  `;
}

function renderReadiness() {
  const preflight = state.preflight || {};
  const totals = preflight.totals || {};
  const byPlatform = (preflight.by_platform || []).map(row => `${escapeHtml(row.platform || 'Unknown')}: ${row.ready ?? 0}/${row.total ?? 0}`).join(' · ');
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
      return `<option value="adapter:${index}"${isSelected ? ' selected' : ''}>Use ${escapeHtml(choice.label || choice.adapter_id || 'adapter')}</option>`;
    }).join('');
    const flatpakChoice = (item?.emulator_choices || []).find(c => c.flatpak_app_id);
    const installSelected = state.emulatorChoices.get(result.candidate_id)?.launch_setup === 'install_flatpak';
    const recommendChips = (item?.emulator_choices || []).map((choice, index) => `
      <button type="button" class="setup-emulator-chip" data-emulator-chip="${escapeHtml(choice.adapter_id)}" data-candidate-id="${escapeHtml(result.candidate_id || '')}" data-choice-index="${index}">Install ${escapeHtml(choice.label || choice.adapter_id)}</button>
    `).join('');
    return `
      <article class="setup-readiness-row" data-candidate-id="${escapeHtml(result.candidate_id || '')}" data-preflight-status="${escapeHtml(result.status || '')}">
        <div class="setup-readiness-head">
          <strong>${escapeHtml(result.candidate_id || result.game_id || 'Candidate')}</strong>
          <span class="setup-readiness-status">${escapeHtml(result.status || '')}</span>
        </div>
        <ul class="setup-check-list">${checks || '<li>No checks</li>'}</ul>
        ${recommendChips ? `<div class="setup-recommend-chips">${recommendChips}</div>` : ''}
        <label class="field">Emulator choice
          <select class="setup-emulator-choice" data-candidate-id="${escapeHtml(result.candidate_id || '')}">
            <option value="">—</option>
            ${choiceOptions}
            ${flatpakChoice ? `<option value="install_flatpak"${installSelected ? ' selected' : ''}>Install Flatpak (${escapeHtml(flatpakChoice.flatpak_app_id || '')})</option>` : ''}
            <option value="keep_custom">Keep custom launch</option>
            <option value="incomplete">Import incomplete (not launch-ready)</option>
          </select>
        </label>
      </article>
    `;
  }).join('');
  return `
    <div class="setup-readiness" data-setup-panel="readiness">
      ${progressCopy}
      <div class="setup-preflight-totals">
        <span>Ready ${totals.ready ?? 0}</span>
        <span>Warning ${totals.warning ?? 0}</span>
        <span>Blocked ${totals.blocked ?? 0}</span>
      </div>
      <p class="setup-by-platform">${byPlatform || 'No platform breakdown yet.'}</p>
      <div class="setup-readiness-list">${results || '<p class="description setup-empty">Run preflight after selecting import candidates.</p>'}</div>
      <button type="button" class="setup-run-preflight" id="setupRunPreflight">Run preflight</button>
    </div>
  `;
}

function renderOptions() {
  const mediaChecks = MEDIA_TYPES.map(type => `
    <label class="setup-media-type"><input type="checkbox" data-media-type="${type}" ${state.options.media_types.includes(type) ? 'checked' : ''}> ${escapeHtml(type.replace(/_/g, ' '))}</label>
  `).join('');
  return `
    <div class="setup-options" data-setup-panel="options">
      <label class="field setup-checkbox"><input type="checkbox" id="setupMetadataSync" ${state.options.metadata_sync ? 'checked' : ''}> Download / update LaunchBox metadata database after import</label>
      <fieldset class="setup-fieldset">
        <legend>Media types to download (fill missing only by default)</legend>
        <div class="setup-media-grid">${mediaChecks}</div>
      </fieldset>
      <label class="field">Region preference
        <select id="setupRegionPreference">
          <option value="world"${state.options.region_preference === 'world' ? ' selected' : ''}>World</option>
          <option value="us"${state.options.region_preference === 'us' ? ' selected' : ''}>United States</option>
          <option value="eu"${state.options.region_preference === 'eu' ? ' selected' : ''}>Europe</option>
          <option value="jp"${state.options.region_preference === 'jp' ? ' selected' : ''}>Japan</option>
        </select>
      </label>
      <label class="field">Download limit (0 = no limit)
        <input type="number" id="setupDownloadLimit" min="0" value="${state.options.download_limit || 0}">
      </label>
      <label class="field setup-checkbox"><input type="checkbox" id="setupReplaceExisting" ${state.options.replace_existing ? 'checked' : ''}> Replace existing metadata/media (explicit opt-in)</label>
    </div>
  `;
}

function renderConfirm() {
  const doc = state.previewDoc || {};
  const humanMessage = doc.message ? `<p class="setup-confirm-message">${escapeHtml(doc.message)}</p>` : '';
  const staleNudge = state.stale
    ? '<p class="setup-stale" role="alert">Preview is stale — sources or library changed since scan. Revalidate to sync before committing.</p><p class="description setup-revalidate-nudge">Revalidating re-scans your sources and checks the library fingerprint. It takes a moment but prevents surprises.</p>'
    : '<p class="description setup-revalidate-hint">Tip: revalidate if you added files or changed your library since scanning.</p>';
  return `
    <div class="setup-confirm" data-setup-panel="confirm">
      ${humanMessage}
      ${staleNudge}
      <p class="description">Revalidate scans sources again, then commit writes imported games to your library.</p>
      <div class="setup-confirm-summary">
        <p>Preview <strong>${escapeHtml(state.previewId || '—')}</strong> revision <strong>${doc.revision ?? state.revision}</strong></p>
        <p>Candidates: <strong>${state.previewItems.length}</strong></p>
        <p>Import actions: <strong>${selectedImportCandidates().length}</strong></p>
      </div>
      ${state.stale ? '<button type="button" class="primary setup-revalidate" id="setupRevalidate">Revalidate preview</button>' : '<button type="button" class="icon-button setup-revalidate" id="setupRevalidate">Revalidate preview</button>'}
      ${state.busy ? '<p class="setup-status">Operation in progress…</p>' : ''}
    </div>
  `;
}

function renderFinish() {
  const counts = state.finishCounts || state.commitCounts || {};
  const keys = ['added', 'merged', 'skipped', 'unmatched', 'media_complete', 'launch_ready', 'warning', 'failed'];
  const countCards = keys.map(key => `
    <div class="setup-finish-stat" data-finish-key="${key}">
      <span class="setup-stat-label">${escapeHtml(key.replace(/_/g, ' '))}</span>
      <strong>${counts[key] ?? 0}</strong>
    </div>
  `).join('');
  return `
    <div class="setup-finish" data-setup-panel="finish">
      <div class="setup-finish-counts">${countCards}</div>
      <div class="setup-finish-actions">
        <button type="button" class="primary" id="setupViewImported">View imported games</button>
        <button type="button" id="setupReviewMetadata">Review unmatched metadata</button>
        <button type="button" id="setupFixLaunch">Fix launch blockers</button>
        <button type="button" id="setupRetryWork">Retry failed work</button>
        <button type="button" id="setupOpenActivity">Open Activity Center</button>
      </div>
    </div>
  `;
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
  if (back) back.disabled = state.step <= 1 || state.busy;
  if (cont) {
    cont.disabled = state.busy;
    cont.textContent = state.step >= 8 ? 'Done' : 'Continue';
  }
}

function bindPanelEvents() {
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
    const path = await nativePickFolder('Absolute path of the folder to import.');
    if (!path) return;
    const recursive = await promptChoice({
      title: 'Folder recursion',
      message: 'Scan subfolders recursively?',
      choices: [{value: 'yes', label: 'Yes, recurse'}, {value: 'no', label: 'Top level only'}],
      defaultValue: 'yes',
    });
    state.sources.push({type: 'folder', id: path, path, label: def.label, recursive: recursive !== 'no'});
    renderPanel();
    return;
  }
  if (type === 'xbox360') {
    const path = await nativePickFolder('Absolute path of the Xbox 360 content folder.');
    if (!path) return;
    state.sources.push({type: 'xbox360', id: path, path, label: def.label});
    renderPanel();
    return;
  }
  if (type === 'arcade') {
    const path = await nativePickFolder('Absolute path of the arcade ROM folder.');
    if (!path) return;
    const setType = await promptChoice({
      title: 'Arcade set type',
      message: 'Choose the arcade set type.',
      choices: [{value: 'MAME', label: 'MAME'}, {value: 'FinalBurn Neo', label: 'FinalBurn Neo'}],
      defaultValue: 'MAME',
    });
    if (!setType) return;
    const dat = (await nativePickFile('Absolute DAT/XML path. Leave blank to use installed MAME metadata.')) ?? '';
    const command = (await promptInput({
      title: 'Launch command',
      message: 'Optional launch command. Use {rom_name} and {path}.',
      defaultValue: '',
    })) ?? '';
    state.sources.push({
      type: 'arcade', id: path, path, label: def.label,
      set_type: setType, dat_path: dat, command,
      adapter_id: setType === 'FinalBurn Neo' ? 'fbneo' : 'mame',
    });
    renderPanel();
    return;
  }
  if (['scummvm', 'rpcs3', 'vita3k'].includes(type)) {
    const path = await nativePickFolder(`Absolute path for ${def.label} import.`);
    if (!path) return;
    state.sources.push({type, id: path, path, label: def.label});
    renderPanel();
    return;
  }
  if (type === 'faugus') {
    state.sources.push({type: 'faugus', id: 'faugus', label: def.label});
    renderPanel();
    return;
  }
  state.sources.push({type, id: type, label: def.label});
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
    notify('warning', `Flatpak install failed: ${error.message}`);
  }
}

async function startPreviewScan() {
  if (!state.sources.length) throw new Error('Add at least one source before scanning.');
  state.busy = true;
  state.runningJobId = '';
  renderPanel();
  const payload = {
    sources: state.sources.map(source => {
      const copy = {...source};
      delete copy.label;
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
    if (!state.previewDoc?.revalidated) throw new Error('Preview was not revalidated.');
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
      notify('warning', `Metadata sync failed: ${error.message}`);
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
      notify('warning', `Metadata match preview failed: ${error.message}`);
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
      notify('warning', `Media download failed: ${error.message}`);
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
    notify('warning', `Final summary failed: ${error.message}`);
  }
  state.finishCounts = finishCounts;
  try {
    AppState.appSettings = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({...AppState.appSettings, welcome_completed: true}),
    });
  } catch (error) {
    notify('warning', `Could not save welcome_completed: ${error.message}`);
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
      if (!state.sources.length) throw new Error('Select at least one source.');
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
  if (dialog?.open) dialog.close();
}

export async function openSetupCenter({step = 1} = {}) {
  ensureSetupShell();
  if (!state.summary) {
    try { await loadSummary(); } catch { /* offline overview */ }
  }
  state.step = step;
  renderPanel();
  const dialog = $('setupCenter');
  if (dialog && !dialog.open) dialog.showModal();
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

queueMicrotask(() => { initSetupCenter(); });

export { SUMMARY_KEYS, STEPS, waitForJob, emulatorChoicePayload, decisionPayload };
