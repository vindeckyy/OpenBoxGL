/* picker.js — "What should I play?" smart picker dialog. */
import { $, escapeHtml } from './util.js';
import { t } from './i18n.js';
import { AppState, api, filteredGames, media, notify } from './state.js';
import { launch } from './sessions.js';
import { render, selectGame } from './library.js';

    const VALID_TIMES = [0, 30, 45, 60, 90, 120];
    const VALID_MOODS = ['any', 'action', 'chill', 'story', 'retro', 'party'];
    const VALID_FAMILIARITIES = ['any', 'new', 'favorite'];

    let lastCriteria = { minutes: 0, mood: 'any', familiarity: 'any', players: 1 };

    function openPicker() {
      if (!AppState.games.length) return notify(t('picker.empty'));
      $('pickerDialog').showModal();
      renderPickerControls();
      $('pickerResult').hidden = true;
      $('pickerResultEmpty').hidden = true;
      $('pickerSpinning').hidden = true;
      $('closePicker').onclick = () => $('pickerDialog').close();
      $('closePicker2').onclick = () => $('pickerDialog').close();
      $('pickerSpin').onclick = () => doPick(false);
      $('pickerRandom').onclick = () => doPick(true);
      // Prevent form submit from closing the dialog.
      $('pickerForm').onsubmit = event => { event.preventDefault(); doPick(false); };
    }

    function renderPickerControls() {
      const time = $('pickerTime');
      if (time) time.value = String(lastCriteria.minutes);
      const mood = $('pickerMood');
      if (mood) mood.value = lastCriteria.mood;
      const familiarity = $('pickerFamiliarity');
      if (familiarity) familiarity.value = lastCriteria.familiarity;
      const players = $('pickerPlayers');
      if (players) players.value = String(lastCriteria.players);
    }

    function collectPickerCriteria() {
      return {
        minutes: Number($('pickerTime')?.value || 0),
        mood: String($('pickerMood')?.value || 'any'),
        familiarity: String($('pickerFamiliarity')?.value || 'any'),
        players: Math.max(1, Math.min(8, Number($('pickerPlayers')?.value || 1))),
        scope: 'all',
        scope_name: '',
      };
    }

    async function doPick(surprise = false) {
      if (surprise) {
        const visible = filteredGames();
        if (!visible.length) {
          $('pickerResult').hidden = true;
          $('pickerResultEmpty').hidden = false;
          return;
        }
        const game = visible[Math.floor(Math.random() * visible.length)];
        showPick({
          id: game.id,
          game_id: game.game_id || String(game.id),
          name: game.name,
          has_cover: game.has_cover,
          cover: game.cover,
          score: 0,
          reason_key: 'picker.reason.random',
          reason_params: { name: game.name },
        });
        return;
      }
      lastCriteria = collectPickerCriteria();
      $('pickerResult').hidden = true;
      $('pickerResultEmpty').hidden = true;
      $('pickerSpinning').hidden = false;
      try {
        const result = await api('/api/v2/library/pick', {
          method: 'POST',
          body: JSON.stringify(lastCriteria),
        });
        $('pickerSpinning').hidden = true;
        if (!result.picks || !result.picks.length) {
          $('pickerResultEmpty').hidden = false;
          return;
        }
        showPick(result.picks[0]);
      } catch(error) {
        $('pickerSpinning').hidden = true;
        notify(error.message);
      }
    }

    function showPick(pick) {
      const game = AppState.games.find(g => String(g.id) === String(pick.id)) || pick;
      const cover = game.has_cover ? `<img src="${escapeHtml(media(game, 'cover'))}" alt="" loading="lazy" decoding="async">` : '';
      const reason = t(pick.reason_key, pick.reason_params || { name: pick.name });
      const placeholder = `<div class="cover-title">${escapeHtml(pick.name)}</div>`;
      $('pickerResultCard').innerHTML = `<div class="cover picker-cover">${cover || placeholder}</div><div class="picker-meta"><h3>${escapeHtml(pick.name)}</h3><p class="description">${escapeHtml(reason)}</p><div class="picker-actions"><button type="button" class="primary" id="pickerLaunch">${t('picker.launch')}</button><button type="button" class="icon-button" id="pickerAnother">${t('picker.again')}</button><button type="button" class="icon-button" id="pickerDetails">${t('picker.details')}</button></div></div>`;
      $('pickerLaunch').onclick = () => { launch(game, $('pickerLaunch')); };
      $('pickerAnother').onclick = () => doPick(false);
      $('pickerDetails').onclick = () => { const found = AppState.games.find(g => String(g.id) === String(pick.id)); if (found) { selectGame(found.id); render(); } $('pickerDialog').close(); };
      $('pickerResult').hidden = false;
      $('pickerResultEmpty').hidden = true;
    }

    export { openPicker };
