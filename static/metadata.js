import { $, escapeHtml, fact } from './util.js';
import { api, notify, AppState, token } from './state.js';
import { refresh, renderDetails } from './library.js';
import { confirmAction, promptChoice, openDialog, closeDialog } from './dialogs.js';

const MATCH_FIELDS = ['title', 'platform', 'year', 'developer', 'publisher', 'genre', 'esrb', 'description', 'media_categories'];
const REVIEW_CLASSES = ['exact_review', 'likely', 'possible', 'unmatched'];
const FIELD_ALLOW_OPTIONS = ['title', 'platform', 'year', 'developer', 'publisher', 'genre', 'esrb', 'description'];
const MEDIA_ALLOW_OPTIONS = [
  ['cover', 'metadataCover'], ['background', 'metadataBackground'], ['screenshots', 'metadataScreenshots'],
  ['box_back', 'metadataBoxBack'], ['box_spine', 'metadataBoxSpine'], ['box_3d', 'metadataBox3d'],
  ['clear_logo', 'metadataClearLogo'], ['fanart', 'metadataFanart'], ['banner', 'metadataBanner'],
  ['icon', 'metadataIcon'], ['title_screen', 'metadataTitleScreen'], ['cart_front', 'metadataCartFront'],
  ['cart_back', 'metadataCartBack'], ['disc', 'metadataDisc'], ['advertisement', 'metadataAdvertisement'],
  ['manual', 'metadataManual'],
];

const reviewState = {
  previewId: '',
  revision: 0,
  counts: null,
  items: [],
  nextCursor: null,
  classFilter: '',
  loading: false,
  mode: 'review',
};

function ensureMatchReviewHosts() {
  const dialog = $('metadataDialog');
  if (!dialog || dialog.dataset.matchReviewHosts) return;
  dialog.dataset.matchReviewHosts = '1';
  const title = $('metadataDialogTitle');
  if (title) title.textContent = 'Metadata';
  const searchForm = $('metadataSearchForm');
  if (!searchForm) return;
  const tabs = document.createElement('div');
  tabs.className = 'match-review-tabs wide';
  tabs.innerHTML = `
    <button type="button" class="match-review-tab active" data-metadata-tab="review">Match review</button>
    <button type="button" class="match-review-tab" data-metadata-tab="search">Search database</button>
  `;
  searchForm.parentNode.insertBefore(tabs, searchForm);
  searchForm.classList.add('metadata-search-panel');
  const reviewPanel = document.createElement('div');
  reviewPanel.id = 'metadataReviewPanel';
  reviewPanel.className = 'match-review-panel wide';
  reviewPanel.innerHTML = `
    <div class="match-review-toolbar">
      <div class="match-review-counts" id="matchReviewCounts"></div>
      <div class="match-review-bulk">
        <button type="button" class="icon-button" id="matchReviewBulkExact">Accept exact review</button>
        <button type="button" class="icon-button" id="matchReviewBulkLikely">Accept likely</button>
        <button type="button" class="primary" id="matchReviewApply">Apply accepted</button>
      </div>
    </div>
    <div class="match-review-filters" id="matchReviewFilters"></div>
    <div class="match-review-apply-options" id="matchReviewApplyOptions">
      <p class="description">Apply uses fill-missing by default. Check fields or media below only when you intend to replace existing values.</p>
      <div class="match-review-allow-grid" id="matchReviewFieldAllow"></div>
      <label class="check"><input type="checkbox" id="matchReviewReplaceExisting"> Replace existing fields/media (creates safety backup first)</label>
    </div>
    <div class="match-review-list" id="matchReviewList"></div>
    <div class="match-review-pagination">
      <button type="button" class="icon-button" id="matchReviewLoadMore" hidden>Load more</button>
    </div>
  `;
  searchForm.parentNode.insertBefore(reviewPanel, searchForm);
  tabs.querySelectorAll('[data-metadata-tab]').forEach(button => {
    button.onclick = () => setMetadataTab(button.dataset.metadataTab);
  });
  $('matchReviewBulkExact').onclick = () => bulkAcceptClass('exact_review');
  $('matchReviewBulkLikely').onclick = () => bulkAcceptClass('likely');
  $('matchReviewApply').onclick = () => applyMatchReview();
  $('matchReviewLoadMore').onclick = () => loadMatchItems({append: true});
  const fieldAllow = $('matchReviewFieldAllow');
  fieldAllow.innerHTML = FIELD_ALLOW_OPTIONS.map(name => `<label class="check"><input type="checkbox" data-field-allow="${name}"> ${escapeHtml(name.replace('_', ' '))}</label>`).join('')
    + MEDIA_ALLOW_OPTIONS.map(([name, id]) => `<label class="check"><input type="checkbox" data-media-allow="${name}" id="matchReviewMedia_${name}"> ${escapeHtml(name.replace('_', ' '))}</label>`).join('');
}

function setMetadataTab(mode) {
  reviewState.mode = mode;
  document.querySelectorAll('.match-review-tab').forEach(button => {
    button.classList.toggle('active', button.dataset.metadataTab === mode);
  });
  const reviewPanel = $('metadataReviewPanel');
  const searchPanel = $('metadataSearchForm');
  if (reviewPanel) reviewPanel.hidden = mode !== 'review';
  if (searchPanel) searchPanel.hidden = mode !== 'search';
}

function formatFieldValue(key, value) {
  if (value == null || value === '') return '—';
  if (key === 'media_categories') return Array.isArray(value) && value.length ? value.join(', ') : '—';
  return String(value);
}

function renderScore(score = {}) {
  const reasons = Array.isArray(score.reasons) ? score.reasons : [];
  return `<div class="match-review-score">
    <span>Title ${Math.round((score.title_similarity || 0) * 100)}%</span>
    <span>Tokens ${Math.round((score.token_overlap || 0) * 100)}%</span>
    <span>Platform ${score.platform_exact ? 'exact' : 'different'}</span>
    ${reasons.length ? `<ul class="match-review-reasons">${reasons.map(reason => `<li>${escapeHtml(reason)}</li>`).join('')}</ul>` : ''}
  </div>`;
}

function renderFieldCompare(key, current, proposed) {
  const cur = formatFieldValue(key, current?.[key]);
  const next = proposed ? formatFieldValue(key, proposed?.[key]) : '—';
  const label = key === 'media_categories' ? 'Media' : key.replace('_', ' ');
  return `<div class="match-review-field" data-field="${escapeHtml(key)}">
    <span class="match-review-field-label">${escapeHtml(label)}</span>
    <div class="match-review-field-values">
      <span class="match-review-current" data-current="${escapeHtml(key)}">${escapeHtml(cur)}</span>
      <span class="match-review-proposed" data-proposed="${escapeHtml(key)}">${escapeHtml(next)}</span>
    </div>
  </div>`;
}

function renderMatchRow(item) {
  const proposed = item.proposed;
  const fields = MATCH_FIELDS.map(key => renderFieldCompare(key, item.current, proposed)).join('');
  const alternatives = Array.isArray(item.alternatives) ? item.alternatives : [];
  return `<article class="match-review-row" data-game-id="${escapeHtml(item.game_id)}" data-class="${escapeHtml(item.class)}">
    <header class="match-review-row-head">
      <span class="match-review-class">${escapeHtml(item.class.replace('_', ' '))}</span>
      ${renderScore(item.score)}
    </header>
    <div class="match-review-compare">${fields}</div>
    <div class="match-review-actions">
      ${proposed ? `<button type="button" class="primary" data-match-action="accept" data-database-id="${escapeHtml(proposed.database_id)}">Accept</button>` : ''}
      ${alternatives.length ? `<button type="button" class="icon-button" data-match-action="choose">Choose other</button>` : ''}
      <button type="button" class="icon-button" data-match-action="skip">Skip</button>
      <button type="button" class="icon-button" data-match-action="never">Never this ID</button>
    </div>
  </article>`;
}

function renderMatchCounts(counts = {}) {
  const parts = [
    ['auto_applied', 'Auto-applied'],
    ['exact_review', 'Exact review'],
    ['likely', 'Likely'],
    ['possible', 'Possible'],
    ['unmatched', 'Unmatched'],
  ];
  return parts.map(([key, label]) => `<span class="match-review-count" data-count="${key}"><strong>${Number(counts[key] || 0)}</strong> ${label}</span>`).join('');
}

function renderClassFilters() {
  const box = $('matchReviewFilters');
  if (!box) return;
  box.innerHTML = ['', ...REVIEW_CLASSES].map(value => {
    const label = value ? value.replace('_', ' ') : 'All classes';
    const active = reviewState.classFilter === value ? ' active' : '';
    return `<button type="button" class="match-review-filter${active}" data-class-filter="${escapeHtml(value)}">${escapeHtml(label)}</button>`;
  }).join('');
  box.querySelectorAll('[data-class-filter]').forEach(button => {
    button.onclick = () => {
      reviewState.classFilter = button.dataset.classFilter || '';
      loadMatchItems();
    };
  });
}

function bindMatchRowActions() {
  document.querySelectorAll('.match-review-row [data-match-action]').forEach(button => {
    button.onclick = async () => {
      const row = button.closest('.match-review-row');
      const gameId = row?.dataset.gameId;
      const action = button.dataset.matchAction;
      if (!gameId || !action) return;
      try {
        if (action === 'accept') {
          await postDecisions([{game_id: gameId, action: 'accept', database_id: button.dataset.databaseId || null}]);
        } else if (action === 'skip') {
          await postDecisions([{game_id: gameId, action: 'skip', database_id: null}]);
        } else if (action === 'never') {
          const ok = await confirmAction({
            title: 'Never this database ID',
            target: row.querySelector('[data-proposed="title"]')?.textContent?.trim() || gameId,
            consequence: 'This proposed database match will be rejected for this game in future previews.',
            confirmLabel: 'Never match',
          });
          if (!ok) return;
          await postDecisions([{game_id: gameId, action: 'never', database_id: null}]);
        } else if (action === 'choose') {
          const item = reviewState.items.find(entry => entry.game_id === gameId);
          const choices = (item?.alternatives || []).map(alt => ({
            value: alt.database_id,
            label: `${alt.title}${alt.platform ? ` · ${alt.platform}` : ''}`,
          }));
          const databaseId = await promptChoice({
            title: 'Choose database match',
            message: 'Select another candidate for this game.',
            label: 'Database match',
            choices,
          });
          if (!databaseId) return;
          await postDecisions([{game_id: gameId, action: 'choose', database_id: databaseId}]);
        }
        await refreshPreviewDocument();
        await loadMatchItems();
        notify('Decision saved');
      } catch (error) {
        notify(error.message);
      }
    };
  });
}

function renderMatchList() {
  const list = $('matchReviewList');
  if (!list) return;
  if (!reviewState.items.length) {
    list.innerHTML = '<p class="description">No matches in this queue. Unique exact title+platform matches were auto-applied during preview.</p>';
    return;
  }
  list.innerHTML = reviewState.items.map(renderMatchRow).join('');
  bindMatchRowActions();
}

async function refreshPreviewDocument() {
  if (!reviewState.previewId) return;
  const doc = await api(`/api/v2/metadata/matches/preview?preview_id=${encodeURIComponent(reviewState.previewId)}`);
  reviewState.revision = doc.revision;
  reviewState.counts = doc.counts || {};
  const countsBox = $('matchReviewCounts');
  if (countsBox) countsBox.innerHTML = renderMatchCounts(reviewState.counts);
}

async function waitForPreviewReady(previewId) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const doc = await api(`/api/v2/metadata/matches/preview?preview_id=${encodeURIComponent(previewId)}`);
    reviewState.revision = doc.revision;
    reviewState.counts = doc.counts || {};
    if (doc.state === 'ready') return doc;
    if (doc.state === 'expired') throw new Error('Match preview expired');
    $('metadataStatus').textContent = doc.state === 'running' ? 'Building match preview…' : 'Match preview queued…';
    await new Promise(resolve => setTimeout(resolve, 800));
  }
  throw new Error('Match preview timed out');
}

async function loadMatchItems({append = false} = {}) {
  if (!reviewState.previewId) return;
  reviewState.loading = true;
  const params = new URLSearchParams({
    preview_id: reviewState.previewId,
    limit: '50',
  });
  const requestCursor = append ? reviewState.nextCursor : null;
  if (requestCursor) params.set('cursor', requestCursor);
  if (reviewState.classFilter) params.set('class', reviewState.classFilter);
  const page = await api(`/api/v2/metadata/matches/items?${params}`);
  reviewState.revision = page.revision;
  reviewState.nextCursor = page.next_cursor || null;
  const items = Array.isArray(page.items) ? page.items : [];
  reviewState.items = append ? [...reviewState.items, ...items] : items;
  renderMatchList();
  const more = $('matchReviewLoadMore');
  if (more) more.hidden = !reviewState.nextCursor;
  reviewState.loading = false;
}

async function postDecisions(items) {
  return api('/api/v2/metadata/matches/decisions', {
    method: 'POST',
    body: JSON.stringify({preview_id: reviewState.previewId, items}),
  });
}

async function bulkAcceptClass(matchClass) {
  const params = new URLSearchParams({preview_id: reviewState.previewId, limit: '200', class: matchClass});
  const page = await api(`/api/v2/metadata/matches/items?${params}`);
  const items = (page.items || []).map(item => ({
    game_id: item.game_id,
    action: 'accept',
    database_id: item.proposed?.database_id || null,
  })).filter(entry => entry.database_id);
  if (!items.length) {
    notify(`No ${matchClass.replace('_', ' ')} items to accept`);
    return;
  }
  await postDecisions(items);
  await refreshPreviewDocument();
  await loadMatchItems();
  notify(`Accepted ${items.length} ${matchClass.replace('_', ' ')} match${items.length === 1 ? '' : 'es'}`);
}

function collectApplyOptions() {
  const replaceExisting = Boolean($('matchReviewReplaceExisting')?.checked);
  const fieldAllow = [...document.querySelectorAll('[data-field-allow]:checked')].map(node => node.dataset.fieldAllow);
  const mediaAllow = [...document.querySelectorAll('[data-media-allow]:checked')].map(node => node.dataset.mediaAllow);
  return {
    replace_existing: replaceExisting,
    field_allow_list: fieldAllow.length ? fieldAllow : null,
    media_allow_list: mediaAllow.length ? mediaAllow : null,
  };
}

async function applyMatchReview() {
  if (!reviewState.previewId) return;
  const options = collectApplyOptions();
  if (options.replace_existing) {
    const ok = await confirmAction({
      title: 'Replace existing metadata',
      target: 'Accepted match decisions',
      consequence: 'Checked fields and media will overwrite existing library values.',
      retained: 'A safety backup is created before replacement begins.',
      recovery: 'Restore from library backups if needed.',
      confirmLabel: 'Replace with backup',
    });
    if (!ok) return;
    if (!options.field_allow_list?.length && !options.media_allow_list?.length) {
      notify('Select at least one field or media type to replace');
      return;
    }
  }
  await api('/api/v2/metadata/matches/apply', {
    method: 'POST',
    body: JSON.stringify({
      preview_id: reviewState.previewId,
      revision: reviewState.revision,
      game_ids: null,
      field_allow_list: options.field_allow_list,
      media_allow_list: options.media_allow_list,
      replace_existing: options.replace_existing,
    }),
  });
  notify('Metadata apply started');
  await refresh();
}

async function openMatchReview({preview_id: previewId = '', import_batch_id: importBatchId = null, game_ids: gameIds = null, class_filter: classFilter = ''} = {}) {
  ensureMatchReviewHosts();
  reviewState.classFilter = classFilter || '';
  if (!$('metadataDialog').open) openDialog($('metadataDialog'));
  setMetadataTab('review');
  renderClassFilters();
  try {
    if (!previewId) {
      const body = {};
      if (importBatchId) body.import_batch_id = importBatchId;
      else if (Array.isArray(gameIds) && gameIds.length) body.game_ids = gameIds;
      else body.game_ids = AppState.games.map(game => game.game_id || String(game.id)).filter(Boolean);
      const queued = await api('/api/v2/metadata/matches/preview', {method: 'POST', body: JSON.stringify(body)});
      previewId = queued.preview_id;
    }
    reviewState.previewId = previewId;
    await waitForPreviewReady(previewId);
    const countsBox = $('matchReviewCounts');
    if (countsBox) countsBox.innerHTML = renderMatchCounts(reviewState.counts);
    await loadMatchItems();
    $('metadataStatus').textContent = 'Review proposed matches before applying. Only exact title+platform matches auto-apply.';
  } catch (error) {
    notify(error.message);
  }
}

async function steamMetadata(id) { try { notify('Downloading Steam metadata and artwork'); await api('/api/metadata/steam',{method:'POST',body:JSON.stringify({id})}); await refresh(); notify('Steam metadata updated'); } catch(error) { notify(error.message); } }
async function openMetadata(game) {
  ensureMatchReviewHosts();
  AppState.metadataGameId = game.id;
  $('metadataQuery').value = game.name;
  $('metadataResults').innerHTML = '';
  if (!$('metadataDialog').open) openDialog($('metadataDialog'));
  setMetadataTab('search');
  try {
    const status = await api('/api/metadata/status');
    renderMetadataStatus(status);
    if (status.ready) searchMetadata();
  } catch(error) { notify(error.message); }
}
function renderMetadataStatus(status = {}) {
  const state = status?.job?.state || '';
  $('metadataStatus').textContent = status.ready ? 'Local database ready.' : state === 'downloading' ? 'Downloading and indexing the official database...' : state === 'error' ? status?.job?.error || 'Error' : 'Download the official metadata database before searching.';
  $('syncMetadata').disabled = state === 'downloading';
  const coverage = status.coverage;
  const coverageBox = $('metadataCoverage');
  const factsBox = $('metadataCoverageFacts');
  if (coverageBox) {
    if (status.ready && coverage && coverage.games) {
      const fields = [
        ['with_cover', 'Games with box front'],
        ['with_background', 'Games with background'],
        ['with_box_back', 'Games with box back'],
        ['with_cart_front', 'Games with cart front'],
        ['with_disc', 'Games with disc'],
        ['with_advertisement', 'Games with ads / flyers'],
        ['with_title_screen', 'Games with title screen'],
        ['with_clear_logo', 'Games with clear logo'],
        ['with_manual', 'Games with manual'],
      ];
      factsBox.innerHTML = `${fact('Games', coverage.games)}${fact('Database matched', coverage.matched_games)}${fact('Match ratio', coverage.matched_ratio == null ? '-' : `${Math.round(coverage.matched_ratio * 100)}%`)}${fields.filter(([key]) => coverage[key] != null).map(([key, label]) => fact(label, coverage[key])).join('')}`;
      coverageBox.style.display = '';
    } else {
      coverageBox.style.display = 'none';
    }
  }
}

async function searchMetadata() {
  try {
    const result = await api(`/api/metadata/search?id=${AppState.metadataGameId}&q=${encodeURIComponent($('metadataQuery').value)}`);
    $('metadataResults').innerHTML = result.results.length ? result.results.map(item => `<div class="metadata-result"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.platform)}${item.release_date ? ` · ${escapeHtml(item.release_date)}` : ''}${item.developer ? ` · ${escapeHtml(item.developer)}` : ''}</small></div><button type="button" class="primary" data-apply-metadata="${Number(item.database_id) || ''}">Use</button></div>`).join('') : '<p class="description">No matching games found.</p>';
    document.querySelectorAll('[data-apply-metadata]').forEach(button => button.onclick = () => applyMetadata(button.dataset.applyMetadata));
  } catch(error) { notify(error.message); }
}
if ($('metadataSearchForm')) $('metadataSearchForm').onsubmit = event => { event.preventDefault(); searchMetadata(); };
$('syncMetadata').onclick = async () => {
  try {
    await api('/api/metadata/sync',{method:'POST',body:'{}'});
    renderMetadataStatus({ready:false,job:{state:'downloading'}});
    watchMetadata();
  } catch(error) { notify(error.message); }
};
$('autoMatchMetadata').onclick = async () => {
  try {
    $('autoMatchMetadata').disabled = true;
    await openMatchReview({game_ids: AppState.games.map(game => game.game_id || String(game.id)).filter(Boolean)});
  } catch(error) { notify(error.message); }
  finally { $('autoMatchMetadata').disabled = false; }
};
$('searchScreenscraper').onclick = async () => {
  const game = AppState.games.find(item => item.id === AppState.metadataGameId);
  try {
    const result = await api(`/api/v2/screenscraper/search?q=${encodeURIComponent($('metadataQuery').value)}&platform=${encodeURIComponent(game?.platform || '')}`);
    $('metadataResults').innerHTML = result.results.length ? result.results.map(item => `<div class="metadata-result"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.system_name || '')}${item.year ? ` · ${escapeHtml(item.year)}` : ''}</small></div><button type="button" class="primary" data-apply-ss="${Number(item.id) || ''}">Use</button></div>`).join('') : '<p class="description">No ScreenScraper matches found.</p>';
    document.querySelectorAll('[data-apply-ss]').forEach(button => button.onclick = async () => {
      try {
        await api('/api/v2/screenscraper/apply',{method:'POST',body:JSON.stringify({id:AppState.games.find(item => item.id === AppState.metadataGameId)?.game_id,scraper_id:Number(button.dataset.applySs),media:['cover','screenshots','fanart','clear_logo']})});
        closeDialog($('metadataDialog'));
        await refresh();
        notify('ScreenScraper metadata applied');
      } catch(error) { notify(error.message); }
    });
  } catch(error) { notify(error.message); }
};
$('hashMatchScreenscraper').onclick = async () => {
  const game = AppState.games.find(item => item.id === AppState.metadataGameId);
  if (!game) return notify('Open a game first, then hash-match it.');
  if (!game.path) return notify('This game has no ROM file to hash.');
  try {
    // The apply route hashes the ROM and matches by hash server-side.
    await api('/api/v2/screenscraper/apply',{method:'POST',body:JSON.stringify({id:game.game_id,rom_path:game.path,fields:['description','year','genre','developer','publisher'],media:['cover','screenshots','clear_logo']})});
    closeDialog($('metadataDialog'));
    notify('ScreenScraper hash-match queued — progress in the Activity Center');
  } catch(error) { notify(error.message); }
};
$('searchIgdb').onclick = async () => {
  const game = AppState.games.find(item => item.id === AppState.metadataGameId);
  try {
    const result = await api(`/api/metadata/igdb/search?q=${encodeURIComponent($('metadataQuery').value)}&platform=${encodeURIComponent(game?.platform || '')}`);
    $('metadataResults').innerHTML = result.results.length ? result.results.map(item => `<div class="metadata-result"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.platforms || '')}${item.year ? ` · ${escapeHtml(item.year)}` : ''}</small><p class="description">${escapeHtml(item.summary || '')}</p></div><button type="button" class="primary" data-apply-igdb="${Number(item.id) || ''}">Use</button></div>`).join('') : '<p class="description">No IGDB matches found.</p>';
    document.querySelectorAll('[data-apply-igdb]').forEach(button => button.onclick = async () => {
      try {
        await api('/api/metadata/igdb/apply',{method:'POST',body:JSON.stringify({id:AppState.metadataGameId,igdb_id:Number(button.dataset.applyIgdb)})});
        closeDialog($('metadataDialog'));
        await refresh();
        notify('IGDB metadata applied');
      } catch(error) { notify(error.message); }
    });
  } catch(error) { notify(error.message); }
};
async function applyMetadata(databaseId) {
  const media = MEDIA_ALLOW_OPTIONS.filter(([,id]) => $(id)?.checked).map(([name]) => name);
  try {
    notify('Downloading selected metadata and media');
    const result = await api('/api/metadata/apply',{method:'POST',body:JSON.stringify({id:AppState.metadataGameId,database_id:databaseId,media,overwrite:$('metadataOverwrite').checked})});
    closeDialog($('metadataDialog'));
    await refresh();
    notify((result.notes || []).length ? result.notes.join(' · ') : 'Metadata applied');
  } catch(error) { notify(error.message); }
}
async function watchMatchMetadata() {
  try {
    if (reviewState.previewId) {
      await refreshPreviewDocument();
      await loadMatchItems();
      $('autoMatchMetadata').disabled = false;
      notify('Match review updated');
      return;
    }
    const status = await api('/api/metadata/status');
    const job = status.job || {};
    if (job.state === 'running') {
      $('metadataStatus').textContent = `Auto-matching: ${job.matched || 0} matched so far.`;
      return setTimeout(watchMatchMetadata, 1200);
    }
    $('autoMatchMetadata').disabled = false;
    await refresh();
    renderMetadataStatus(status);
    notify(`Auto-match finished: ${job.matched || 0} games matched`);
  } catch(error) { notify(error.message); $('autoMatchMetadata').disabled = false; }
}
async function watchMetadata() {
  try {
    const status = await api('/api/metadata/status');
    renderMetadataStatus(status);
    if (status?.job?.state === 'downloading') return setTimeout(watchMetadata, 1500);
    if (status.ready) { notify('Metadata database ready'); searchMetadata(); }
  } catch(error) { notify(error.message); }
}
async function loadAchievements(id) {
  try {
    $('achievementContent').innerHTML = '<p class="description">Matching ROM and loading progress...</p>';
    const result = await api('/api/ra/game',{method:'POST',body:JSON.stringify({id})});
    await refresh();
    if ($('achievementContent')) {
      $('achievementContent').innerHTML = `<p class="description">${result.earned} of ${result.total} earned · ${escapeHtml(result.completion)}${result.earned_hardcore ? ` · ${result.earned_hardcore} hardcore` : ''}${result.beaten ? ` · beaten ${result.beaten}` : ''}${result.mastered ? ` · mastered ${result.mastered}` : ''}${result.motivation ? ` · ${escapeHtml(result.motivation)}` : ''}</p>${(result.achievements || []).map(item => `<div class="achievement"><img src="/api/ra/badge?name=${encodeURIComponent(item.badge)}&locked=${item.earned ? 0 : 1}&token=${encodeURIComponent(token)}" alt="" loading="lazy" decoding="async"><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.description)}</small></div><span>${item.points} pts${item.hardcore ? ' ★' : ''}</span></div>`).join('')}`;
    }
  } catch(error) { notify(error.message); renderDetails(); }
}

ensureMatchReviewHosts();
queueMicrotask(() => {
  if ($('metadataButton')) $('metadataButton').onclick = () => openMatchReview();
});

export { steamMetadata, openMetadata, openMatchReview, renderMetadataStatus, searchMetadata, applyMetadata, watchMatchMetadata, watchMetadata, loadAchievements };
