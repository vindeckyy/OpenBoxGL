import { $, escapeHtml, duration } from './util.js';
import { api, notify, AppState, token, setButtonBusy } from './state.js';
import { refresh, launchExtra } from './library.js';



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
      } catch(error) { notify(error.message); }
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
    function connectSessionEvents() {
      try {
        const source = new EventSource(`/api/events?token=${encodeURIComponent(token)}`);
        let _sseRefreshTimer = null;
        source.onmessage = event => {
          let data;
          try { data = JSON.parse(event.data); } catch { return; }
          const kind = data?.kind || data?.type;
          if (kind === 'session.started' || kind === 'session.stopped' || kind === 'session.state' || kind === 'job.finished') {
            pollSessions();
          } else if (kind === 'state.changed') {
            if (_sseRefreshTimer) clearTimeout(_sseRefreshTimer);
            _sseRefreshTimer = setTimeout(() => { _sseRefreshTimer = null; refresh().catch(() => {}); }, 500);
          }
        };
        source.onerror = () => { source.close(); /* fall back to polling */ };
      } catch { /* EventSource unsupported; polling stays */ }
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
          await refresh();
        }
        $('sessionsButton').textContent = result.running.length ? `Running (${result.running.length})` : 'Running';
        $('sessionsButton').disabled = !result.running.length;
        if (result.running.length) $('status').textContent = `${result.running.length} game${result.running.length === 1 ? '' : 's'} running`;
        if ($('sessionsDialog').open) renderSessions();
      } catch(error) { notify(error.message); }
      finally {
        sessionPollBusy = false;
        // Poll every second while a session is active, every ten when idle.
        sessionPollIdle = !AppState.runningGames.length;
        setTimeout(pollSessions, sessionPollIdle ? 10000 : 1000);
      }
    }
    async function openHistory() {
      try {
        const result = await api('/api/history');
        $('historyList').innerHTML = result.enabled
          ? (result.history.length ? result.history.map(session => `<div class="history-item"><strong>${escapeHtml(session.game)}</strong><br>${escapeHtml(String(session.started || '').replace('T',' '))} · ${duration(session.seconds)} · exit ${session.exit_code}</div>`).join('') : '<p class="description">No sessions recorded yet.</p>')
          : '<p class="description">Session history tracking is disabled in Settings.</p>';
        if (!$('historyDialog').open) $('historyDialog').showModal();
      } catch(error) { notify(error.message); }
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
        const game = AppState.games.find(item => item.id === session.game_id);
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
        $('saveBackups').innerHTML = result.backups.slice(0,8).map(backup => `<button class="icon-button" data-backup="${escapeHtml(backup.name)}">${escapeHtml(backup.name)}</button>`).join('');
        document.querySelectorAll('[data-backup]').forEach(button => button.onclick = () => restoreSaves(id,button.dataset.backup));
      } catch(error) { notify(error.message); }
    }
    async function backupSaves(id) { try { const result = await api('/api/saves/backup',{method:'POST',body:JSON.stringify({id})}); notify(`Created ${result.backup}`); loadBackups(id); } catch(error) { notify(error.message); } }
    async function restoreSaves(id,backup) {
      const { confirmAction } = await import('./dialogs.js');
      const ok = await confirmAction({
        title: 'Restore saves',
        message: `Restore ${backup}?`,
        consequence: 'Current saves will be backed up first.',
      });
      if (!ok) return;
      try { await api('/api/saves/restore',{method:'POST',body:JSON.stringify({id,backup})}); notify('Save restored'); loadBackups(id); } catch(error) { notify(error.message); }
    }
    async function discoverSaves(id) {
      try {
        const result = await api(`/api/saves/discover?id=${id}`);
        $('saveDiscovery').innerHTML = result.candidates.length ? result.candidates.map(item => `<button class="icon-button" data-save-path="${escapeHtml(item.path)}">${item.shared ? 'Add shared location' : 'Add'} · ${escapeHtml(item.label)}<br><small>${escapeHtml(item.path)}</small></button>`).join('') : 'No new save locations were detected.';
        document.querySelectorAll('[data-save-path]').forEach(button => button.onclick = async () => {
          try { await api('/api/saves/add',{method:'POST',body:JSON.stringify({id,path:button.dataset.savePath})}); await refresh(); notify('Save location added'); } catch(error) { notify(error.message); }
        });
      } catch(error) { notify(error.message); }
    }

export { launch, showLifecycle, connectSessionEvents, pollSessions, openHistory, openSessions, renderSessions, loadBackups, backupSaves, restoreSaves, discoverSaves };
