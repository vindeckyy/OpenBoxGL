import { $, escapeHtml, duration, formatBytes, formatDate } from './util.js';
import { api, notify, AppState, setButtonBusy, notifyError, token } from './state.js';
import { refresh, launchExtra } from './library.js';
import { renderTimelineTab } from './timeline.js';
import { showSessionRecap, showSessionRecapForStopped } from './recap.js';
import { onServerEvent } from './events.js';
import { t } from './i18n.js';



    function resolveGameId(gameOrId) {
      if (gameOrId && typeof gameOrId === 'object') {
        return gameOrId.game_id || String(gameOrId.id ?? '');
      }
      const game = AppState.games.find(item => item.id === gameOrId || item.game_id === gameOrId);
      return game?.game_id || String(gameOrId ?? '');
    }

    async function launch(gameOrId, trigger = $('playButton')) {
      const game_id = resolveGameId(gameOrId);
      if (!game_id) return;
      setButtonBusy(trigger, true);
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
          if (!ok) return;
        }
        const preflight = await api('/api/v2/launch/preflight', {
          method: 'POST',
          body: JSON.stringify({ game_id, candidate: null }),
        });
        if (preflight.status === 'blocked') {
          const messages = (preflight.checks || []).map(check => check.message).filter(Boolean);
          notify(messages.join(' · ') || 'Launch blocked');
          return;
        }
        if (preflight.status === 'warning') {
          const { confirmAction } = await import('./dialogs.js');
          const warnings = (preflight.checks || []).filter(check => check.severity === 'warning').map(check => check.message).filter(Boolean);
          const ok = await confirmAction({
            title: 'Launch warning',
            message: warnings.join('\n') || 'Some launch checks reported warnings.',
            consequence: 'Continue launching anyway?',
          });
          if (!ok) return;
        }
        const result = await api('/api/launch', { method: 'POST', body: JSON.stringify({ game_id }) });
        showLifecycle('Starting', result.game, 'The game process is running', 1800);
        await refresh();
      } catch(error) { notifyError(error); }
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
    let sessionPollError = false;
    let sessionEventsBound = false;
    function renderSessionsButton() {
      const count = AppState.runningGames.length;
      // Composed from t() rather than stored English, so a localechange pass
      // cannot clobber the count back to the untranslated label.
      $('sessionsButton').textContent = count ? `${t('nav.running')} (${count})` : t('nav.running');
      $('sessionsButton').disabled = !count;
    }
    document.addEventListener('localechange', renderSessionsButton);
    function connectSessionEvents() {
      if (sessionEventsBound) return;
      sessionEventsBound = true;
      // The shared stream (static/events.js) owns reconnects; these
      // subscriptions only map the named frames onto session handlers.
      let _sseRefreshTimer = null;
      const handleSessionEvent = () => pollSessions();
      const handleStateChanged = () => {
        if (_sseRefreshTimer) clearTimeout(_sseRefreshTimer);
        _sseRefreshTimer = setTimeout(() => { _sseRefreshTimer = null; refresh().catch(() => {}); }, 500);
      };
      ['session.started', 'session.stopped', 'session.state', 'job.finished'].forEach(kind => onServerEvent(kind, handleSessionEvent));
      onServerEvent('state.changed', handleStateChanged);
      onServerEvent('session.recap', event => {
        let payload;
        try { payload = JSON.parse(event.data); } catch { return; }
        showSessionRecap(payload).catch(() => {});
      });
      // Unnamed frames still carry {kind|type}; route them to the same handlers.
      onServerEvent('message', event => {
        let data;
        try { data = JSON.parse(event.data); } catch { return; }
        const kind = data?.kind || data?.type;
        if (kind === 'session.started' || kind === 'session.stopped' || kind === 'session.state' || kind === 'job.finished') {
          pollSessions();
        } else if (kind === 'state.changed') {
          handleStateChanged();
        }
      });
    }
    function scheduleSessionPoll(delay) { setTimeout(pollSessions, delay); }
    async function pollSessions() {
      if (sessionPollBusy) {
        setTimeout(pollSessions, 1000);
        return;
      }
      sessionPollBusy = true;
      try {
        const result = await api(`/api/running?after=${AppState.lastSessionEvent}`);
        AppState.lastSessionEvent = result.last_event;
        AppState.runningGames = result.running;
        sessionPollError = false;
        const stopped = result.events.filter(event => event.kind === 'stopped').at(-1);
        if (stopped) {
          const exitCode = Number(stopped.exit_code ?? '');
          const shortSession = Number(stopped.seconds ?? 0) < 5;
          const failed = Number.isFinite(exitCode) && exitCode !== 0;
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
        renderSessionsButton();
        if (result.running.length) $('status').textContent = t('sessions.status_running', {count: result.running.length});
        if ($('sessionsDialog').open) renderSessions();
      } catch(error) {
        // A persistent outage must not toast every 10 seconds; surface the
        // first failure of an outage and stay quiet until polling recovers.
        if (!sessionPollError) {
          sessionPollError = true;
          notifyError(error);
        }
      }
      finally {
        sessionPollBusy = false;
        // Poll every second while a session is active, every ten when idle.
        sessionPollIdle = !AppState.runningGames.length;
        setTimeout(pollSessions, sessionPollIdle ? 10000 : 1000);
      }
    }
    // ── Session/journal export (P5) ─────────────────────────────────────────
    // Bounded server-side, streamed from a temp file; the token query param
    // matches how documents and exports are downloaded elsewhere.
    function renderHistoryExport(history) {
      let host = $('historyExport');
      if (!host) {
        host = document.createElement('div');
        host.id = 'historyExport';
        host.className = 'extras history-export';
        $('historyList')?.after(host);
      }
      if (!host) return;
      const games = new Map();
      (history || []).forEach(session => {
        const id = String(session.game_id || '');
        if (id && !games.has(id)) games.set(id, session.game || id);
      });
      host.innerHTML = `<span class="description">${escapeHtml(t('history.export_title'))}</span>
        <select id="historyExportScope" aria-label="${escapeHtml(t('history.export_scope'))}"><option value="game">${escapeHtml(t('history.export_scope_game'))}</option><option value="member">${escapeHtml(t('history.export_scope_member'))}</option></select>
        <select id="historyExportTarget" aria-label="${escapeHtml(t('history.export_game'))}"><option value="">${escapeHtml(t('history.export_all_games'))}</option>${[...games.entries()].map(([id, name]) => `<option value="${escapeHtml(id)}">${escapeHtml(name)}</option>`).join('')}</select>
        <button type="button" class="icon-button" id="historyExportCsv">${escapeHtml(t('history.export_csv'))}</button>
        <button type="button" class="icon-button" id="historyExportMd">${escapeHtml(t('history.export_md'))}</button>`;
      const scope = $('historyExportScope');
      const target = $('historyExportTarget');
      const syncTarget = () => { target.disabled = scope.value !== 'game'; };
      scope.onchange = syncTarget;
      syncTarget();
      const download = format => {
        const params = new URLSearchParams({scope: scope.value, format});
        if (scope.value === 'game' && target.value) params.set('game_id', target.value);
        const anchor = document.createElement('a');
        anchor.href = `/api/v2/sessions/export?${params.toString()}&token=${encodeURIComponent(token)}`;
        anchor.download = '';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      };
      $('historyExportCsv').onclick = () => download('csv');
      $('historyExportMd').onclick = () => download('md');
    }
    async function openHistory() {
      try {
        const result = await api('/api/history');
        $('historyList').innerHTML = result.enabled
          ? (result.history.length ? result.history.map(session => `<div class="history-item"><strong>${escapeHtml(session.game)}</strong><br>${escapeHtml(formatDate(session.started))} · ${duration(session.seconds)} · exit ${session.exit_code}</div>`).join('') : '<p class="description">No sessions recorded yet.</p>')
          : '<p class="description">Session history tracking is disabled in Settings.</p>';
        renderHistoryExport(result.enabled ? result.history : []);
        // tab init
        $('historyTabList').onclick = () => switchHistoryTab('list');
        $('historyTabTimeline').onclick = () => switchHistoryTab('timeline');
        switchHistoryTab('list');
        if (!$('historyDialog').open) {
          const { openDialog } = await import('./dialogs.js');
          openDialog($('historyDialog'));
        }
      } catch(error) { notifyError(error); }
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
        if (!$('sessionsDialog').open) {
          const { openDialog } = await import('./dialogs.js');
          openDialog($('sessionsDialog'));
        }
      } catch(error) { notifyError(error); }
    }
    function renderSessions() {
      $('sessionList').innerHTML = AppState.runningGames.length ? AppState.runningGames.map(session => {
        const game = AppState.games.find(item => item.id === session.game_id);
        const extras = game ? `${game.documents.map((item,index) => `<button class="icon-button" data-session-extra="${game.id}:documents:${index}">Read ${escapeHtml(item.name)}</button>`).join('')}${game.applications.map((item,index) => `<button class="icon-button" data-session-extra="${game.id}:applications:${index}">${escapeHtml(item.name)}</button>`).join('')}${game.versions.map((item,index) => `<button class="icon-button" data-session-extra="${game.id}:versions:${index}">Version · ${escapeHtml(item.name)}</button>`).join('')}${game.save_paths.length ? `<button class="icon-button" data-session-backup="${game.id}">Back up saves</button>` : ''}` : '';
        return `<div class="detail-card"><h3>${escapeHtml(session.game)}</h3><p class="description">${session.paused ? 'Paused' : 'Running'} · PID ${session.pid} · started ${escapeHtml(formatDate(session.started))}</p><div class="extras"><button class="primary" data-session-action="${session.launch_id}:${session.paused ? 'resume' : 'pause'}">${session.paused ? 'Resume' : 'Pause'}</button><button class="icon-button" data-session-action="${session.launch_id}:restart">Restart</button><button class="icon-button" data-session-action="${session.launch_id}:stop">Exit</button><button class="icon-button" data-session-action="${session.launch_id}:kill">Force close</button>${extras}</div></div>`;
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
        } catch(error) { notifyError(error); }
      });
      document.querySelectorAll('[data-session-extra]').forEach(button => button.onclick = () => {
        const [id,kind,index] = button.dataset.sessionExtra.split(':');
        launchExtra(Number(id),kind,Number(index));
      });
      document.querySelectorAll('[data-session-backup]').forEach(button => button.onclick = () => backupSaves(Number(button.dataset.sessionBackup)));
    }
    function ageText(seconds) {
      const value = Math.max(0, Number(seconds || 0));
      if (value < 3600) return t('history.save_age_minutes', { minutes: String(Math.max(1, Math.round(value / 60))) });
      if (value < 86400) return t('history.save_age_hours', { hours: (value / 3600).toFixed(1) });
      return t('history.save_age_days', { days: (value / 86400).toFixed(1) });
    }
    function sourceLabel(source) {
      const key = `history.save_source_${String(source || 'other').replace(/-/g, '_')}`;
      const translated = t(key);
      return translated === key ? t('history.save_source_other') : translated;
    }
    async function loadBackups(id) {
      try {
        const result = await api(`/api/v2/saves/history?id=${id}`);
        if (!$('saveBackups')) return;
        const versions = (result.versions || []).slice(0, 8);
        $('saveBackups').innerHTML = versions.length
          ? versions.map(version => `<div class="save-version" data-save-version="${escapeHtml(version.name)}"><span class="save-version-source">${escapeHtml(sourceLabel(version.source))}</span><span class="save-version-meta">${escapeHtml(ageText(version.age_seconds))} · ${escapeHtml(formatBytes(version.size))}</span><span class="save-version-actions"><button class="icon-button" data-save-verify="${escapeHtml(version.name)}">${escapeHtml(t('history.verify'))}</button><button class="icon-button" data-save-drill="${escapeHtml(version.name)}">${escapeHtml(t('history.test_restore'))}</button><button class="icon-button" data-save-restore="${escapeHtml(version.name)}">${escapeHtml(t('history.restore'))}</button></span></div>`).join('')
          : `<span class="description">${escapeHtml(t('history.no_versions'))}</span>`;
        $('saveBackups').querySelectorAll('[data-save-verify]').forEach(button => {
          button.onclick = () => verifySaves(id, button.dataset.saveVerify, button);
        });
        $('saveBackups').querySelectorAll('[data-save-drill]').forEach(button => {
          button.onclick = () => testRestoreSaves(id, button.dataset.saveDrill, button);
        });
        $('saveBackups').querySelectorAll('[data-save-restore]').forEach(button => {
          button.onclick = () => restoreSaves(id, button.dataset.saveRestore);
        });
      } catch(error) { notifyError(error); }
    }
    async function backupSaves(id) { try { const result = await api('/api/saves/backup',{method:'POST',body:JSON.stringify({id})}); notify(`Created ${result.backup}`); loadBackups(id); } catch(error) { notifyError(error); } }
    async function verifySaves(id, backup, trigger) {
      setButtonBusy(trigger, true);
      try {
        const result = await api('/api/v2/saves/history/verify',{method:'POST',body:JSON.stringify({id,backup})});
        notify(t('history.verify_ok', { members: result.members || 0, size: formatBytes(result.bytes) }));
      } catch(error) {
        notifyError(error, t('history.verify_failed'));
      } finally {
        setButtonBusy(trigger, false);
      }
    }
    async function testRestoreSaves(id, backup, trigger) {
      // Dry-run restore: the server extracts into a temp dir and verifies file
      // count/checksums; live saves are never touched.
      setButtonBusy(trigger, true);
      try {
        const result = await api('/api/v2/saves/history/test-restore',{method:'POST',body:JSON.stringify({id,backup})});
        if (result.ok) notify('success', t('history.test_restore_ok', { files: result.files || 0, size: formatBytes(result.bytes) }));
        else notify('warning', t('history.test_restore_mismatch', { count: (result.mismatches || []).length }));
      } catch(error) {
        notifyError(error, t('history.test_restore_failed'));
      } finally {
        setButtonBusy(trigger, false);
      }
    }
    async function restoreSaves(id,backup) {
      const { confirmAction } = await import('./dialogs.js');
      const ok = await confirmAction({
        title: t('history.restore'),
        message: t('history.restore_confirm', { backup }),
        consequence: t('history.restore_consequence'),
      });
      if (!ok) return;
      try {
        await api('/api/v2/saves/history/restore',{method:'POST',body:JSON.stringify({id,backup})});
        notify(t('history.restored'));
        loadBackups(id);
      } catch(error) { notifyError(error); }
    }
    async function discoverSaves(id) {
      try {
        const result = await api(`/api/saves/discover?id=${id}`);
        $('saveDiscovery').innerHTML = result.candidates.length ? result.candidates.map(item => `<button class="icon-button" data-save-path="${escapeHtml(item.path)}">${item.shared ? 'Add shared location' : 'Add'} · ${escapeHtml(item.label)}<br><small>${escapeHtml(item.path)}</small></button>`).join('') : 'No new save locations were detected.';
        document.querySelectorAll('[data-save-path]').forEach(button => button.onclick = async () => {
          try { await api('/api/saves/add',{method:'POST',body:JSON.stringify({id,path:button.dataset.savePath})}); await refresh(); notify('Save location added'); } catch(error) { notifyError(error); }
        });
      } catch(error) { notifyError(error); }
    }

export { launch, showLifecycle, connectSessionEvents, pollSessions, openHistory, openSessions, renderSessions, loadBackups, backupSaves, restoreSaves, verifySaves, testRestoreSaves, discoverSaves };
