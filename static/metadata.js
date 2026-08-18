import { $, escapeHtml, fact } from './util.js';
import { api, notify, AppState, token } from './state.js';
import { refresh, renderDetails } from './library.js';



    async function steamMetadata(id) { try { notify('Downloading Steam metadata and artwork'); await api('/api/metadata/steam',{method:'POST',body:JSON.stringify({id})}); await refresh(); notify('Steam metadata updated'); } catch(error) { notify(error.message); } }
    async function openMetadata(game) {
      AppState.metadataGameId = game.id;
      $('metadataQuery').value = game.name;
      $('metadataResults').innerHTML = '';
      if (!$('metadataDialog').open) $('metadataDialog').showModal();
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
    $('metadataSearchForm').onsubmit = event => { event.preventDefault(); searchMetadata(); };
    $('syncMetadata').onclick = async () => {
      try {
        await api('/api/metadata/sync',{method:'POST',body:'{}'});
        renderMetadataStatus({ready:false,job:{state:'downloading'}});
        watchMetadata();
      } catch(error) { notify(error.message); }
    };
    $('autoMatchMetadata').onclick = async () => {
      try {
        $('metadataStatus').textContent = 'Auto-matching your library by exact title. This only binds exact matches; ambiguous titles are left for you to confirm.';
        $('autoMatchMetadata').disabled = true;
        await api('/api/metadata/match',{method:'POST',body:JSON.stringify({platform:AppState.platform})});
        watchMatchMetadata();
      } catch(error) { notify(error.message); $('autoMatchMetadata').disabled = false; }
    };
    $('searchIgdb').onclick = async () => {
      const game = AppState.games.find(item => item.id === AppState.metadataGameId);
      try {
        const result = await api(`/api/metadata/igdb/search?q=${encodeURIComponent($('metadataQuery').value)}&platform=${encodeURIComponent(game?.platform || '')}`);
        $('metadataResults').innerHTML = result.results.length ? result.results.map(item => `<div class="metadata-result"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.platforms || '')}${item.year ? ` · ${escapeHtml(item.year)}` : ''}</small><p class="description">${escapeHtml(item.summary || '')}</p></div><button type="button" class="primary" data-apply-igdb="${Number(item.id) || ''}">Use</button></div>`).join('') : '<p class="description">No IGDB matches found.</p>';
        document.querySelectorAll('[data-apply-igdb]').forEach(button => button.onclick = async () => {
          try {
            await api('/api/metadata/igdb/apply',{method:'POST',body:JSON.stringify({id:AppState.metadataGameId,igdb_id:Number(button.dataset.applyIgdb)})});
            $('metadataDialog').close();
            await refresh();
            notify('IGDB metadata applied');
          } catch(error) { notify(error.message); }
        });
      } catch(error) { notify(error.message); }
    };
    async function applyMetadata(databaseId) {
      const media = [['cover','metadataCover'],['background','metadataBackground'],['screenshots','metadataScreenshots'],['box_back','metadataBoxBack'],['box_spine','metadataBoxSpine'],['box_3d','metadataBox3d'],['clear_logo','metadataClearLogo'],['fanart','metadataFanart'],['banner','metadataBanner'],['icon','metadataIcon'],['title_screen','metadataTitleScreen'],['cart_front','metadataCartFront'],['cart_back','metadataCartBack'],['disc','metadataDisc'],['advertisement','metadataAdvertisement'],['manual','metadataManual']].filter(([,id]) => $(id).checked).map(([name]) => name);
      try {
        notify('Downloading selected metadata and media');
        const result = await api('/api/metadata/apply',{method:'POST',body:JSON.stringify({id:AppState.metadataGameId,database_id:databaseId,media,overwrite:$('metadataOverwrite').checked})});
        $('metadataDialog').close();
        await refresh();
        notify((result.notes || []).length ? result.notes.join(' · ') : 'Metadata applied');
      } catch(error) { notify(error.message); }
    }
    async function watchMatchMetadata() {
      try {
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

export { steamMetadata, openMetadata, renderMetadataStatus, searchMetadata, applyMetadata, watchMatchMetadata, watchMetadata, loadAchievements };
