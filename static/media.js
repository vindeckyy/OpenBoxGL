import { $, fact, escapeHtml } from './util.js';
import { api, notify, AppState } from './state.js';
import { refresh } from './library.js';



    async function openMediaManager() {
      if (!$('mediaManagerDialog').open) $('mediaManagerDialog').showModal();
      try {
        const audit = await api(`/api/media/audit?platform=${encodeURIComponent(AppState.platform)}`);
        $('mediaAudit').innerHTML = `<h3>${AppState.platform === 'all' ? 'Entire library' : escapeHtml(AppState.platform)}</h3><div class="facts">${fact('Games',audit.games)}${fact('Database matched',audit.matched)}${fact('Missing box front',audit.missing_cover)}${fact('Missing background',audit.missing_background)}${fact('Missing screenshots',audit.missing_screenshots)}${fact('Missing box back',audit.missing_box_back)}${fact('Missing box spine',audit.missing_box_spine)}${fact('Missing 3D box',audit.missing_box_3d)}${fact('Missing clear logo',audit.missing_clear_logo)}${fact('Missing fanart',audit.missing_fanart)}${fact('Missing banner',audit.missing_banner)}${fact('Missing icon',audit.missing_icon)}${fact('Missing title screen',audit.missing_title_screen)}${fact('Missing cart front',audit.missing_cart_front)}${fact('Missing cart back',audit.missing_cart_back)}${fact('Missing disc',audit.missing_disc)}${fact('Missing ads / flyers',audit.missing_advertisement)}${fact('Missing manual',audit.missing_manual)}</div>`;
        const status = await api('/api/media/bulk/status');
        renderBulkMediaStatus(status.job);
      } catch(error) { notify(error.message); }
    }
    function renderBulkMediaStatus(job) {
      const manualMissing = job.manual_missing || 0;
      $('bulkMediaStatus').textContent = job.state === 'running' ? `${job.current || 0} of ${job.total || 0} · ${job.updated || 0} games updated` : job.state === 'done' ? `${job.updated || 0} games updated${manualMissing ? ` · ${manualMissing} had no manual in their archive` : ''}${job.errors?.length ? ` · ${job.errors.length} errors` : ''}` : '';
      $('startBulkMedia').disabled = job.state === 'running';
    }
    $('startBulkMedia').onclick = async () => {
      const media = [['cover','bulkCover'],['background','bulkBackground'],['screenshots','bulkScreenshots'],['box_back','bulkBoxBack'],['box_spine','bulkBoxSpine'],['box_3d','bulkBox3d'],['clear_logo','bulkClearLogo'],['fanart','bulkFanart'],['banner','bulkBanner'],['icon','bulkIcon'],['title_screen','bulkTitleScreen'],['cart_front','bulkCartFront'],['cart_back','bulkCartBack'],['disc','bulkDisc'],['advertisement','bulkAdvertisement'],['manual','bulkManual']].filter(([,id]) => $(id).checked).map(([name]) => name);
      try {
        await api('/api/media/bulk',{method:'POST',body:JSON.stringify({platform:AppState.platform,media,overwrite:$('bulkOverwrite').checked})});
        watchBulkMedia();
      } catch(error) { notify(error.message); }
    };
    async function watchBulkMedia() {
      try {
        const result = await api('/api/media/bulk/status');
        renderBulkMediaStatus(result.job);
        if (result.job.state === 'running') return setTimeout(watchBulkMedia, 1200);
        await refresh();
        await openMediaManager();
        notify('Bulk media download finished');
      } catch(error) { notify(error.message); }
    }
    async function captureScreenshot(id) { try { const result = await api('/api/screenshot',{method:'POST',body:JSON.stringify({id})}); await refresh(); notify(`Screenshot saved to ${result.path}`); } catch(error) { notify(error.message); } }
    async function downloadBezel(platform) { if (!platform) return notify('Select a game with a platform first'); try { const result = await api('/api/bezels/download',{method:'POST',body:JSON.stringify({platform})}); notify(`Bezels downloaded to ${result.path}`); } catch(error) { notify(error.message); } }

export { openMediaManager, renderBulkMediaStatus, watchBulkMedia, captureScreenshot, downloadBezel };
