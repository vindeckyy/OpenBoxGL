import { $, escapeHtml, duration } from './util.js';
import { api, notify, AppState, setButtonBusy, gameForSession, gameIdOf } from './state.js';
import { subscribe, onOpen } from './events.js';
import { refresh, launchExtra } from './library.js';
import { t } from './i18n.js';
import { renderTimelineTab } from './timeline.js';
import { showSessionRecap, showSessionRecapForStopped } from './recap.js';



    function resolveGameId(gameOrId) {
      if (gameOrId && typeof gameOrId === 'object') {
        return gameOrId.game_id || String(gameOrId.id ?? '');
      }
      const game = AppState.games.find(item => item.id === gameOrId || item.game_id === gameOrId);
      return game?.game_id || String(gameOrId ?? '');
    }

    // Resolves true only when a game process was started; every other path
    // (cancelled confirm, blocked preflight, error) resolves false. Callers that
    // close their own surface on launch (the arcade room, S39) depend on this.
    async function launch(gameOrId, trigger = $('playButton')) {
      const game_id = resolveGameId(gameOrId);
      if (!game_id) return false;
      setButtonBusy(trigger, true);
      let launched = false;
      try {
        // launch() accepts either a game object or an id; object callers
        // (palette, bigbox, party, picker) already carry launch_confirm.
        const game = gameOrId && typeof gameOrId === 'object' ? gameOrId
          : AppState.games.find(item => item.id === gameOrId || item.game_id === gameOrId);
        if (game?.launch_confirm) {
          const { confirmAction } = await import('./dialogs.js');
          const ok = await confirmAction({
            title: 'Launch game',
            target: game.name || 'Untitled',
            consequence: 'Launch now?',
            confirmLabel: 'Launch',
          });
          if (!ok) return false;
        }
        const preflight = await api('/api/v2/launch/preflight', {
          method: 'POST',
          body: JSON.stringify({ game_id, candidate: null }),
        });
        if (preflight.status === 'blocked') {
          const messages = (preflight.checks || []).map(check => check.message).filter(Boolean);
          notify(messages.join(' · ') || 'Launch blocked');
          return false;
        }
        if (preflight.status === 'warning') {
          const { confirmAction } = await import('./dialogs.js');
          const warnings = (preflight.checks || []).filter(check => check.severity === 'warning').map(check => check.message).filter(Boolean);
          const ok = await confirmAction({
            title: 'Launch warning',
            message: warnings.join('\n') || 'Some launch checks reported warnings.',
            consequence: 'Continue launching anyway?',
          });
          if (!ok) return false;
        }
        const result = await api('/api/launch', { method: 'POST', body: JSON.stringify({ game_id }) });
        launched = true;
        showLifecycle('Starting', result.game, 'The game process is running', 1800);
        await refresh();
        // F4a: one-time dismissible "mark as Playing?" suggestion. The
        // backend only returns progress_suggest once per game; the dialog
        // never sets progress on its own.
        if (result.progress_suggest) {
          const { confirmAction } = await import('./dialogs.js');
          const marked = await confirmAction({
            title: t('backlog.suggest_title'),
            message: t('backlog.suggest_message', { name: result.game || 'Untitled' }),
            confirmLabel: t('backlog.suggest_confirm'),
          });
          if (marked) {
            await api('/api/v2/library/progress/set', { method: 'POST', body: JSON.stringify({ game_id, progress: 'Playing' }) });
            await refresh();
          }
        }
        return true;
      } catch(error) { notify(error.message); return Boolean(launched); }
      finally { setButtonBusy(trigger, false); }
    }
    function showLifecycle(kind, game, message, milliseconds) {
      $('lifecycleKind').textContent = kind;
      $('lifecycleGame').textContent = game;
      $('lifecycleMessage').textContent = message;
      $('lifecycle').hidden = false;
      clearTimeout(showLifecycle.timer);
      showLifecycle.timer = setTimeout(() => $('lifecycle').hidden = true, milliseconds);
    }
    let sessionPollBusy = false;
    let sessionPollIdle = false;
    // The session stream is subscriptions on the tab's shared event stream
    // (events.js): it owns the EventSource, backoff, close-on-hide and reopen.
    // This module decides what each event means.
    let sessionStreamSubscribed = false;
    let sessionRefreshTimer = null;
    function connectSessionEvents() {
      if (sessionStreamSubscribed) return;
      sessionStreamSubscribed = true;
      subscribe(['session.started', 'session.stopped', 'session.state', 'job.finished'], () => pollSessions());
      subscribe('state.changed', () => {
        if (sessionRefreshTimer) clearTimeout(sessionRefreshTimer);
        sessionRefreshTimer = setTimeout(() => { sessionRefreshTimer = null; refresh().catch(() => {}); }, 500);
      });
      subscribe('session.recap', event => {
        let payload;
        try { payload = JSON.parse(event.data); } catch { return; }
        showSessionRecap(payload).catch(() => {});
      });
      // Anything missed while the stream was closed is caught up with one poll.
      onOpen(() => pollSessions());
    }
    // One pending timer at most. SSE events also call pollSessions() directly;
    // without clearing the pending timer each such call started a second
    // self-rescheduling chain, so the poll rate grew with every session event.
    let sessionPollTimer = 0;
    let sessionPollAgain = false;
    function scheduleSessionPoll(delay) {
      clearTimeout(sessionPollTimer);
      sessionPollTimer = setTimeout(pollSessions, delay);
    }
    async function pollSessions() {
      // S40: the busy branch and the `finally` each scheduled their own
      // successor, so a poll that overlapped the one before it forked the chain
      // and the poll rate doubled every time. One scheduler, called from one
      // place -- `finally` -- is the whole fix; the busy branch only asks the
      // running poll to go again as soon as it finishes.
      if (sessionPollBusy) {
        sessionPollAgain = true;
        return;
      }
      clearTimeout(sessionPollTimer);
      sessionPollTimer = 0;
      sessionPollBusy = true;
      try {
        const result = await api(`/api/running?after=${AppState.lastSessionEvent}`);
        AppState.lastSessionEvent = result.last_event;
        AppState.runningGames = result.running;
        const stopped = result.events.filter(event => event.kind === 'stopped').at(-1);
        if (stopped) {
          // S26: the server now sends a plain int. It used to send the
          // `WaitResult` namedtuple, which json.dumps turns into `[1,false]`;
          // `Number([1,false])` is NaN, so `failed` was always false and every
          // launch that died instantly was reported as "Play time and history
          // were saved". The legacy array is still accepted here because a
          // client can be behind its own server after an upgrade.
          const raw = stopped.exit_code;
          const exitCode = Array.isArray(raw) ? Number(raw[0]) : Number(raw);
          const timedOut = stopped.timed_out === true || (Array.isArray(raw) && raw[1] === true);
          const shortSession = Number(stopped.seconds ?? 0) < 5;
          const failed = Number.isFinite(exitCode) && (exitCode !== 0 || timedOut);
          if (failed && shortSession) {
            showLifecycle('Session failed', stopped.game, `Exited immediately with code ${exitCode}. Check the Launch command and emulator install.`, 5000);
          } else if (failed) {
            showLifecycle('Session ended', stopped.game, `Exited with code ${exitCode}.`, 2500);
          } else {
            showLifecycle('Session ended', stopped.game, 'Play time and history were saved', 1600);
          }
          showSessionRecapForStopped().catch(() => {});
          await refresh();
        }
        $('sessionsButton').textContent = result.running.length ? `${t('nav.running')} (${result.running.length})` : t('nav.running');
        $('sessionsButton').disabled = !result.running.length;
        if (result.running.length) $('status').textContent = `${result.running.length} game${result.running.length === 1 ? '' : 's'} running`;
        if ($('sessionsDialog').open) renderSessions();
      } catch(error) { notify(error.message); }
      finally {
        sessionPollBusy = false;
        // Poll every second while a session is active, every ten when idle.
        sessionPollIdle = !AppState.runningGames.length;
        const again = sessionPollAgain;
        sessionPollAgain = false;
        scheduleSessionPoll(again ? 0 : (sessionPollIdle ? 10000 : 1000));
      }
    }
    async function openHistory() {
      try {
        const result = await api('/api/history');
        $('historyList').innerHTML = result.enabled
          ? (result.history.length ? result.history.map(session => `<div class="history-item"><strong>${escapeHtml(session.game)}</strong><br>${escapeHtml(String(session.started || '').replace('T',' '))} · ${duration(session.seconds)} · exit ${session.exit_code}</div>`).join('') : '<p class="description">No sessions recorded yet.</p>')
          : '<p class="description">Session history tracking is disabled in Settings.</p>';
        // tab init
        $('historyTabList').onclick = () => switchHistoryTab('list');
        $('historyTabTimeline').onclick = () => switchHistoryTab('timeline');
        switchHistoryTab('list');
        if (!$('historyDialog').open) $('historyDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    function switchHistoryTab(tab) {
      const listPane = $('historyListPane');
      const timelinePane = $('historyTimelinePane');
      const listTab = $('historyTabList');
      const timelineTab = $('historyTabTimeline');
      if (!listPane || !timelinePane) return;
      if (tab === 'timeline') {
        listPane.hidden = true;
        timelinePane.hidden = false;
        listTab.classList.remove('active'); listTab.setAttribute('aria-selected', 'false');
        timelineTab.classList.add('active'); timelineTab.setAttribute('aria-selected', 'true');
        renderTimelineTab($('timelineContainer'));
      } else {
        listPane.hidden = false;
        timelinePane.hidden = true;
        listTab.classList.add('active'); listTab.setAttribute('aria-selected', 'true');
        timelineTab.classList.remove('active'); timelineTab.setAttribute('aria-selected', 'false');
      }
    }
    async function openSessions() {
      try {
        const result = await api(`/api/running?after=${AppState.lastSessionEvent}`);
        AppState.runningGames = result.running;
        AppState.lastSessionEvent = result.last_event;
        renderSessions();
        if (!$('sessionsDialog').open) $('sessionsDialog').showModal();
      } catch(error) { notify(error.message); }
    }
    function renderSessions() {
      $('sessionList').innerHTML = AppState.runningGames.length ? AppState.runningGames.map(session => {
        const game = gameForSession(session);
        // S28: the "Read <name>" buttons were numbered with the index *within
        // their own array*, so the click handler could not tell one game's first
        // document from another game's second. Either it acted on the wrong
        // item, or -- because it indexes the live `game` object rather than the
        // snapshot that was rendered -- on an item that no longer exists.
        //
        // The kind is carried in the attribute and the name is in the payload,
        // so the handler never has to re-derive which list an index came from:
        // it looks the button's own index up in the list its own kind names.
        const extras = game ? `${game.documents.map((item,index) => `<button class="icon-button" data-session-extra="${game.id}:documents:${index}">Read ${escapeHtml(item.name)}</button>`).join('')}${game.applications.map((item,index) => `<button class="icon-button" data-session-extra="${game.id}:applications:${index}">${escapeHtml(item.name)}</button>`).join('')}${game.versions.map((item,index) => `<button class="icon-button" data-session-extra="${game.id}:versions:${index}">Version · ${escapeHtml(item.name)}</button>`).join('')}${game.save_paths.length ? `<button class="icon-button" data-session-backup="${game.id}">Back up saves</button>` : ''}` : '';
        return `<div class="detail-card"><h3>${escapeHtml(session.game)}</h3><p class="description">${session.paused ? 'Paused' : 'Running'} · PID ${session.pid} · started ${escapeHtml(String(session.started || '').replace('T',' '))}</p><div class="extras"><button class="primary" data-session-action="${session.launch_id}:${session.paused ? 'resume' : 'pause'}">${session.paused ? 'Resume' : 'Pause'}</button><button class="icon-button" data-session-action="${session.launch_id}:restart">Restart</button><button class="icon-button" data-session-action="${session.launch_id}:stop">Exit</button><button class="icon-button" data-session-action="${session.launch_id}:kill">Force close</button>${extras}</div></div>`;
      }).join('') : '<p class="description">No games are running.</p>';
      document.querySelectorAll('[data-session-action]').forEach(button => button.onclick = async () => {
        const [launch_id,action] = button.dataset.sessionAction.split(':');
        if (action === 'kill') {
          const { confirmAction } = await import('./dialogs.js');
          const ok = await confirmAction({
            title: 'Force close game',
            message: 'Force close this game?',
            consequence: 'Unsaved progress may be lost.',
          });
          if (!ok) return;
        }
        try {
          await api('/api/session/control',{method:'POST',body:JSON.stringify({launch_id,action})});
          notify(action === 'pause' ? 'Game paused' : action === 'resume' ? 'Game resumed' : action === 'restart' ? 'Restarting game' : 'Closing game');
          setTimeout(openSessions, 180);
        } catch(error) { notify(error.message); }
      });
      document.querySelectorAll('[data-session-extra]').forEach(button => button.onclick = () => {
        const [id,kind,index] = button.dataset.sessionExtra.split(':');
        launchExtra(Number(id),kind,Number(index));
      });
      document.querySelectorAll('[data-session-backup]').forEach(button => button.onclick = () => backupSaves(Number(button.dataset.sessionBackup)));
    }
    async function loadBackups(id) {
      try {
        const result = await api(`/api/saves?id=${id}`);
        if (!$('saveBackups')) return;
        $('saveBackups').innerHTML = result.backups.slice(0,8).map(backup => `<button class="icon-button" data-backup="${escapeHtml(backup.name)}">${escapeHtml(backup.label && backup.label !== 'manual' ? `${backup.label} · ` : '')}${escapeHtml(backup.name)}</button>`).join('');
        document.querySelectorAll('[data-backup]').forEach(button => button.onclick = () => restoreSaves(id,button.dataset.backup));
      } catch(error) { notify(error.message); }
    }
    async function backupSaves(id) { try { const result = await api('/api/saves/backup',{method:'POST',body:JSON.stringify({id,game_id:gameIdOf(id)})}); notify(t('notify.sessions.backup_created', {name: result.backup})); loadBackups(id); } catch(error) { notify(error.message); } }
    async function restoreSaves(id,backup) {
      const { confirmAction, openDialog, closeDialog } = await import('./dialogs.js');
      let manifest;
      try { manifest = await api(`/api/saves/manifest?id=${id}&backup=${encodeURIComponent(backup)}`); }
      catch(error) { notify(error.message); return; }
      const dialog = document.createElement('dialog');
      dialog.className = 'dialog';
      dialog.innerHTML = `<div class="dialog-head"><h2>Restore ${escapeHtml(backup)}</h2><button type="button" class="icon-button" data-close>×</button></div><p class="description">${manifest.files.length} file${manifest.files.length === 1 ? '' : 's'} in this backup. Uncheck files you want to keep as-is.</p><div class="extras"><button type="button" class="icon-button" data-all>Select all</button><button type="button" class="icon-button" data-none>Select none</button></div><div id="saveManifestFiles">${manifest.files.map((file,index) => `<label class="check"><input type="checkbox" data-save-file="${index}" checked> ${escapeHtml(file.path)}</label>`).join('')}</div><p class="description" id="saveManifestStatus"></p><div class="description" id="saveRestoreConflicts"></div><div class="dialog-actions"><button type="button" class="icon-button" data-dry>Dry run</button><button type="button" class="primary" data-restore>Restore selected</button></div>`;
      document.body.appendChild(dialog);
      const selected = () => [...dialog.querySelectorAll('[data-save-file]:checked')].map(input => manifest.files[Number(input.dataset.saveFile)].archive);
      const finish = () => { closeDialog(dialog); dialog.remove(); };
      dialog.querySelector('[data-close]').onclick = finish;
      dialog.querySelector('[data-all]').onclick = () => dialog.querySelectorAll('[data-save-file]').forEach(input => { input.checked = true; });
      dialog.querySelector('[data-none]').onclick = () => dialog.querySelectorAll('[data-save-file]').forEach(input => { input.checked = false; });
      const previewRestore = chosen => api('/api/saves/restore/preview', { method: 'POST', body: JSON.stringify({ id, game_id: gameIdOf(id), backup, selected: chosen }) });
      const conflictMarkup = preview => {
        const overwrites = Array.isArray(preview.overwrite) ? preview.overwrite : [];
        const total = Number(preview.counts?.overwrite || overwrites.length || 0);
        if (!total) { dialog.querySelector('#saveRestoreConflicts').innerHTML = ''; return; }
        const paths = overwrites.map(item => item.path || item.archive || '').filter(Boolean);
        const shown = paths.slice(0, 10).map(path => `<li><code>${escapeHtml(path)}</code></li>`).join('');
        dialog.querySelector('#saveRestoreConflicts').innerHTML = `<strong>${total} local file${total === 1 ? '' : 's'} will be overwritten:</strong>${shown ? `<ul>${shown}</ul>` : ''}${total > paths.length ? `<small>${total - paths.length} more not shown.</small>` : ''}`;
      };
      dialog.querySelector('[data-dry]').onclick = async () => {
        const chosen = selected();
        if (!chosen.length) { dialog.querySelector('#saveManifestStatus').textContent = 'Select at least one file.'; return; }
        try {
          const result = await previewRestore(chosen);
          dialog.querySelector('#saveManifestStatus').textContent = `Dry run: ${result.counts.add} new, ${result.counts.overwrite} overwritten, ${result.counts.missing} missing from the archive.`;
          conflictMarkup(result);
        } catch(error) { dialog.querySelector('#saveManifestStatus').textContent = error.message; }
      };
      dialog.querySelector('[data-restore]').onclick = async () => {
        const chosen = selected();
        if (!chosen.length) { dialog.querySelector('#saveManifestStatus').textContent = 'Select at least one file.'; return; }
        let preview;
        try { preview = await previewRestore(chosen); }
        catch(error) { dialog.querySelector('#saveManifestStatus').textContent = error.message; return; }
        conflictMarkup(preview);
        const conflicts = Number(preview.counts?.overwrite || 0);
        const names = (Array.isArray(preview.overwrite) ? preview.overwrite : []).map(item => item.path).filter(Boolean).slice(0, 3);
        const detail = names.length ? ` Differing: ${names.join(', ')}${conflicts > names.length ? `, and ${conflicts - names.length} more` : ''}.` : '';
        const ok = await confirmAction({ title: 'Restore saves', message: `${chosen.length} file(s) will be restored from ${backup}.${conflicts ? ` ${conflicts} local file(s) will be overwritten.` : ''}${detail}`, consequence: 'Current saves will be backed up first.', confirmLabel: conflicts ? 'Restore anyway' : 'Restore' });
        if (!ok) return;
        try { await api('/api/saves/restore',{method:'POST',body:JSON.stringify({id,game_id:gameIdOf(id),backup,selected:chosen,confirm_conflict:Boolean(conflicts)})}); notify(t('notify.sessions.save_restored')); finish(); loadBackups(id); } catch(error) { notify(error.message); }
      };
      openDialog(dialog);
    }
    async function openSaveHistory(id) {
      let backups;
      try { backups = (await api(`/api/saves?id=${encodeURIComponent(id)}`)).backups || []; }
      catch(error) { notify(error.message); return; }
      const { openDialog, closeDialog } = await import('./dialogs.js');
      const dialog = document.createElement('dialog');
      dialog.className = 'dialog';
      dialog.innerHTML = `<div class="dialog-head"><h2>Save version history</h2><button type="button" class="icon-button" data-close>×</button></div><p class="description">${backups.length} backup${backups.length === 1 ? '' : 's'} for this game, newest first.</p><div class="history-list" id="saveVersionList">${backups.length ? backups.map(backup => `<button type="button" class="icon-button" data-version="${escapeHtml(backup.name)}"><strong>${escapeHtml(backup.label || backup.name)}</strong> · ${escapeHtml(new Date(backup.mtime * 1000).toLocaleString())} · ${Math.max(1, Math.round(Number(backup.size || 0) / 1024))} KB</button>`).join('') : '<p class="description">No backups yet.</p>'}</div><div id="saveVersionPreview" class="description">Select a backup to preview its files.</div><div class="dialog-actions"><button type="button" class="icon-button" data-close>Close</button></div>`;
      document.body.appendChild(dialog);
      const finish = () => { closeDialog(dialog); dialog.remove(); };
      dialog.querySelectorAll('[data-close]').forEach(button => button.onclick = finish);
      dialog.querySelectorAll('[data-version]').forEach(button => button.onclick = async () => {
        const backup = button.dataset.version;
        dialog.querySelectorAll('[data-version]').forEach(item => item.classList.toggle('active', item === button));
        const preview = dialog.querySelector('#saveVersionPreview');
        try {
          const result = await api('/api/saves/restore/preview', { method: 'POST', body: JSON.stringify({ id, game_id: gameIdOf(id), backup }) });
          const counts = result.counts || {};
          const overwrites = Array.isArray(result.overwrite) ? result.overwrite : [];
          const conflictPaths = overwrites.map(item => item.path || item.archive || '').filter(Boolean);
          const conflictList = conflictPaths.length ? `<ul>${conflictPaths.slice(0, 10).map(path => `<li><code>${escapeHtml(path)}</code></li>`).join('')}</ul>${Number(counts.overwrite || 0) > conflictPaths.length ? `<small>${Number(counts.overwrite) - conflictPaths.length} more not shown.</small>` : ''}` : '';
          preview.innerHTML = `<p><strong>${escapeHtml(backup)}</strong></p><p>${Number(counts.add || 0)} new · ${Number(counts.overwrite || 0)} overwritten · ${Number(counts.unchanged || 0)} unchanged${counts.missing ? ` · ${Number(counts.missing)} missing from archive` : ''}</p>${conflictList}<button type="button" class="icon-button" id="restoreVersion">${Number(counts.overwrite || 0) ? 'Restore anyway' : 'Restore this version'}</button>`;
          preview.querySelector('#restoreVersion').onclick = () => { finish(); restoreSaves(id, backup); };
        } catch(error) { preview.textContent = error.message; }
      });
      openDialog(dialog);
    }

    async function discoverSaves(id) {
      try {
        const result = await api(`/api/saves/discover?id=${id}`);
        $('saveDiscovery').innerHTML = result.candidates.length ? result.candidates.map(item => `<button class="icon-button" data-save-path="${escapeHtml(item.path)}">${item.shared ? 'Add shared location' : 'Add'} · ${escapeHtml(item.label)}<br><small>${escapeHtml(item.path)}</small></button>`).join('') : 'No new save locations were detected.';
        document.querySelectorAll('[data-save-path]').forEach(button => button.onclick = async () => {
          try { await api('/api/saves/add',{method:'POST',body:JSON.stringify({id,game_id:gameIdOf(id),path:button.dataset.savePath})}); await refresh(); notify(t('notify.sessions.save_location_added')); } catch(error) { notify(error.message); }
        });
      } catch(error) { notify(error.message); }
    }

export { launch, showLifecycle, connectSessionEvents, pollSessions, openHistory, openSessions, renderSessions, loadBackups, backupSaves, restoreSaves, openSaveHistory, discoverSaves };
