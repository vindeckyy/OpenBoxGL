import { $, fact, escapeHtml } from './util.js';
import { api, notify, AppState, gameIdOf } from './state.js';
import { refresh } from './library.js';
import { t } from './i18n.js';



    let bulkMediaTimer = 0;
    let bulkMediaWatch = 0;
    const BULK_MEDIA_FIELDS = [['cover','bulkCover'],['background','bulkBackground'],['screenshots','bulkScreenshots'],['poster','bulkPoster'],['square','bulkSquare'],['box_back','bulkBoxBack'],['box_spine','bulkBoxSpine'],['box_3d','bulkBox3d'],['clear_logo','bulkClearLogo'],['fanart','bulkFanart'],['banner','bulkBanner'],['icon','bulkIcon'],['title_screen','bulkTitleScreen'],['cart_front','bulkCartFront'],['cart_back','bulkCartBack'],['disc','bulkDisc'],['advertisement','bulkAdvertisement'],['manual','bulkManual'],['video_snap','bulkVideoSnap'],['video_theme','bulkVideoTheme'],['video_trailer','bulkVideoTrailer'],['music','bulkMusic']];
    function selectedBulkMedia() {
      return BULK_MEDIA_FIELDS.filter(([,id]) => $(id)?.checked).map(([name]) => name);
    }
    function stopBulkMediaWatch() {
      // Bumping the generation invalidates any watcher already inside its
      // status fetch, so a closed dialog can neither reschedule nor reopen.
      bulkMediaWatch += 1;
      if (bulkMediaTimer) { clearTimeout(bulkMediaTimer); bulkMediaTimer = 0; }
    }
    const mediaManagerDialog = $('mediaManagerDialog');
    if (mediaManagerDialog) mediaManagerDialog.addEventListener('close', stopBulkMediaWatch);
    async function openMediaManager() {
      if (!$('mediaManagerDialog').open) $('mediaManagerDialog').showModal();
      try {
        const audit = await api(`/api/media/audit?platform=${encodeURIComponent(AppState.platform)}`);
        $('mediaAudit').innerHTML = `<h3>${AppState.platform === 'all' ? 'Entire library' : escapeHtml(AppState.platform)}</h3><div class="facts">${fact('Games',audit.games)}${fact('Database matched',audit.matched)}${fact('Missing box front',audit.missing_cover)}${fact('Missing background',audit.missing_background)}${fact('Missing screenshots',audit.missing_screenshots)}${fact('Missing box back',audit.missing_box_back)}${fact('Missing box spine',audit.missing_box_spine)}${fact('Missing 3D box',audit.missing_box_3d)}${fact('Missing clear logo',audit.missing_clear_logo)}${fact('Missing fanart',audit.missing_fanart)}${fact('Missing banner',audit.missing_banner)}${fact('Missing icon',audit.missing_icon)}${fact('Missing title screen',audit.missing_title_screen)}${fact('Missing cart front',audit.missing_cart_front)}${fact('Missing cart back',audit.missing_cart_back)}${fact('Missing disc',audit.missing_disc)}${fact('Missing ads / flyers',audit.missing_advertisement)}${fact('Missing manual',audit.missing_manual)}</div>`;
        const status = await api('/api/media/bulk/status');
        renderBulkMediaStatus(status.job);
        await renderMediaQueue();
      } catch(error) { notify(error.message); }
    }
    async function renderMediaQueue() {
      try {
        const queue = await api('/api/media/queue');
        const items = queue.items || [];
        const failed = items.filter(item => item.state === 'failed');
        const rows = items.slice(0, 12).map(item => `<div class="job-row"><strong>${escapeHtml(item.kind || '')}</strong><span class="badge ${item.state === 'failed' ? 'danger' : ''}">${escapeHtml(item.state || 'queued')}</span><small>${escapeHtml(item.source || '')} · ${escapeHtml(item.game_id || '')}${item.attempts ? ` · ${item.attempts} attempts` : ''}${item.last_error ? ` · ${escapeHtml(item.last_error)}` : ''}</small>${item.state === 'failed' ? `<button type="button" class="icon-button" data-retry-media="${escapeHtml(item.id)}">Retry</button>` : ''}</div>`).join('');
        $('mediaQueueStatus').innerHTML = `<h3>Download queue</h3><div class="facts">${fact('Queued', items.filter(item => item.state !== 'failed').length)}${fact('Failed', failed.length)}${fact('Completed this run', queue.completed || 0)}${fact('Paused', queue.paused ? 'yes' : 'no')}</div>${rows || '<p class="description">The media download queue is empty.</p>'}`;
        $('pauseMediaQueue').disabled = !queue.running || queue.paused;
        $('resumeMediaQueue').disabled = !queue.paused;
        $('cancelMediaQueue').disabled = !queue.running;
        document.querySelectorAll('[data-retry-media]').forEach(button => button.onclick = async () => {
          try { await api('/api/media/queue/retry',{method:'POST',body:JSON.stringify({id:button.dataset.retryMedia})}); await renderMediaQueue(); } catch(error) { notify(error.message); }
        });
      } catch(error) { notify(error.message); }
    }
    $('pauseMediaQueue').onclick = async () => { try { await api('/api/media/queue/pause',{method:'POST',body:'{}'}); await renderMediaQueue(); } catch(error) { notify(error.message); } };
    $('resumeMediaQueue').onclick = async () => { try { await api('/api/media/queue/resume',{method:'POST',body:'{}'}); await renderMediaQueue(); } catch(error) { notify(error.message); } };
    $('cancelMediaQueue').onclick = async () => { try { await api('/api/media/queue/cancel',{method:'POST',body:'{}'}); await renderMediaQueue(); } catch(error) { notify(error.message); } };
    $('refreshMediaQueue').onclick = () => renderMediaQueue();
    $('cleanupMediaCache').onclick = async () => {
      try {
        const result = await api('/api/media/cache/cleanup', { method: 'POST', body: JSON.stringify({ days: 30 }) });
        notify(`${result.deleted} cached file(s) removed`);
      } catch (error) { notify(error.message); }
    };
    function renderBulkMediaStatus(job = {}) {
      const manualMissing = job?.manual_missing || 0;
      const state = job?.state || '';
      const completed = job?.completed_game_ids?.length || 0;
      const failed = job?.failed_game_ids?.length || 0;
      const checkpointText = completed || failed ? ` · ${completed} completed · ${failed} failed` : '';
      $('bulkMediaStatus').textContent = state === 'running' ? `${job?.current || 0} of ${job?.total || 0} · ${job?.updated || 0} games updated${checkpointText}` : state === 'done' || state === 'partial' ? `${job?.updated || 0} games updated${checkpointText}${manualMissing ? ` · ${manualMissing} had no manual in their archive` : ''}${job?.errors?.length ? ` · ${job.errors.length} errors` : ''}` : '';
      $('startBulkMedia').disabled = state === 'running';
      if ($('retryBulkMedia')) $('retryBulkMedia').disabled = state === 'running' || !failed;
    }
    $('startBulkMedia').onclick = async () => {
      const media = selectedBulkMedia();
      try {
        await api('/api/media/bulk',{method:'POST',body:JSON.stringify({platform:AppState.platform,media,overwrite:$('bulkOverwrite').checked})});
        watchBulkMedia();
      } catch(error) { notify(error.message); }
    };
    if ($('retryBulkMedia')) $('retryBulkMedia').onclick = async () => {
      const media = selectedBulkMedia();
      try {
        await api('/api/media/bulk',{method:'POST',body:JSON.stringify({platform:AppState.platform,media,overwrite:$('bulkOverwrite').checked,retry_failed:true})});
        watchBulkMedia();
      } catch(error) { notify(error.message); }
    };
    async function watchBulkMedia() {
      const token = ++bulkMediaWatch;
      if (bulkMediaTimer) { clearTimeout(bulkMediaTimer); bulkMediaTimer = 0; }
      try {
        const result = await api('/api/media/bulk/status');
        if (token !== bulkMediaWatch) return; // dialog closed mid-fetch
        renderBulkMediaStatus(result.job);
        if (result?.job?.state === 'running') {
          bulkMediaTimer = setTimeout(watchBulkMedia, 1200);
          return;
        }
        await refresh();
        if (token !== bulkMediaWatch) return;
        await openMediaManager();
        notify(t('notify.media.bulk_finished'));
      } catch(error) { notify(error.message); }
    }
    async function captureScreenshot(id) { try { const result = await api('/api/screenshot',{method:'POST',body:JSON.stringify({id,game_id:gameIdOf(id)})}); await refresh(); notify(t('notify.media.screenshot_saved', {path: result.path})); } catch(error) { notify(error.message); } }
    async function downloadBezel(platform) { if (!platform) return notify(t('notify.media.select_platform_game')); try { const result = await api('/api/bezels/download',{method:'POST',body:JSON.stringify({platform})}); notify(t('notify.media.bezels_downloaded', {path: result.path})); } catch(error) { notify(error.message); } }

export { openMediaManager, renderBulkMediaStatus, watchBulkMedia, captureScreenshot, downloadBezel };
