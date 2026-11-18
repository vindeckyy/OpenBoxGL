/* Deal Radar — opt-in Steam price watch & manual entries UI (ADR 0109). */
import { escapeHtml } from './util.js';
import { AppState, api, notify, waitForJob } from './state.js';
import { t } from './i18n.js';
import { openDialog, closeDialog, confirmAction } from './dialogs.js';

function formatMoneyCents(cents, currency) {
  if (typeof cents !== 'number' || Number.isNaN(cents)) return '—';
  const cur = String(currency || '').trim().toUpperCase();
  const amount = (cents / 100).toFixed(2);
  return cur ? `${cur} ${amount}` : amount;
}

function renderSparklineSvg(history) {
  const pts = (Array.isArray(history) ? history : []).filter(p => typeof p?.price_cents === 'number');
  if (pts.length < 2) return '';
  const w = 96;
  const h = 24;
  const min = Math.min(...pts.map(p => p.price_cents));
  const max = Math.max(...pts.map(p => p.price_cents));
  const span = Math.max(1, max - min);
  const coords = pts.map((p, i) => {
    const x = Math.round((i / (pts.length - 1)) * (w - 4)) + 2;
    const y = h - 3 - Math.round(((p.price_cents - min) / span) * (h - 6));
    return `${x},${y}`;
  }).join(' ');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline fill="none" stroke="var(--brand)" stroke-width="2" points="${coords}"/></svg>`;
}

function formatAgeLabel(ageHours) {
  if (typeof ageHours !== 'number') return '';
  if (ageHours < 1) return t('deals.age_fresh');
  if (ageHours < 48) return t('deals.age_hours', { hours: ageHours });
  return t('deals.age_days', { days: Math.floor(ageHours / 24) });
}

async function enableDealsOptIn() {
  AppState.appSettings = await api('/api/settings', {
    method: 'POST',
    body: JSON.stringify({ ...AppState.appSettings, deals_enabled: true }),
  });
}

async function watchSteamAppFromGame(game) {
  if (!game?.steam_app_id) return;
  try {
    if (!AppState.appSettings.deals_enabled) {
      const ok = await confirmAction({
        title: t('deals.optin_title'),
        message: t('deals.optin_body'),
        consequence: t('deals.optin_host_note'),
        confirmLabel: t('deals.optin_confirm'),
      });
      if (!ok) return;
      await enableDealsOptIn();
    }
    await api('/api/v2/deals/watch', {
      method: 'POST',
      body: JSON.stringify({
        appid: String(game.steam_app_id),
        name: game.name || '',
        store: 'steam',
      }),
    });
    notify(t('deals.watched_added', { name: game.name || game.steam_app_id }));
  } catch (error) {
    notify(error.message || String(error));
  }
}

async function openDealsDialog() {
  const dialog = document.createElement('dialog');
  dialog.className = 'whats-new-dialog';
  dialog.innerHTML = `
    <div class="dialog-head">
      <h2>${escapeHtml(t('deals.menu_title'))}</h2>
      <button type="button" class="icon-button" data-close aria-label="${escapeHtml(t('common.close'))}">×</button>
    </div>
    <div id="dealsDialogBody"><p class="description">${escapeHtml(t('common.loading'))}</p></div>
    <div class="dialog-actions"><button type="button" class="primary" data-close>${escapeHtml(t('common.close'))}</button></div>
  `;
  const cleanup = () => { closeDialog(dialog); dialog.remove(); };
  dialog.querySelectorAll('[data-close]').forEach(btn => btn.addEventListener('click', cleanup));
  dialog.addEventListener('cancel', cleanup, { once: true });
  document.body.appendChild(dialog);
  openDialog(dialog);

  const body = dialog.querySelector('#dealsDialogBody');
  const renderDialog = async () => {
    try {
      const data = await api('/api/v2/deals');
      if (data.available === false) {
        body.innerHTML = `
          <div class="detail-card">
            <h3>${escapeHtml(t('deals.optin_title'))}</h3>
            <p class="description">${escapeHtml(t('deals.optin_body'))}</p>
            <p class="description"><strong>${escapeHtml(t('deals.optin_host_note'))}</strong></p>
            <button type="button" class="primary" id="dealsEnableBtn">${escapeHtml(t('deals.optin_confirm'))}</button>
          </div>`;
        body.querySelector('#dealsEnableBtn')?.addEventListener('click', async () => {
          await enableDealsOptIn();
          notify(t('deals.enabled_notice'));
          await renderDialog();
        });
        return;
      }

      const items = Array.isArray(data.watchlist) ? data.watchlist : [];
      const rowsHtml = items.length
        ? items.map(item => {
          const priceStr = formatMoneyCents(item.last_price_cents, item.last_currency);
          const lowestStr = typeof item.lowest_retained_cents === 'number'
            ? t('deals.lowest_retained', { price: formatMoneyCents(item.lowest_retained_cents, item.last_currency) })
            : '';
          const targetStr = typeof item.target_price_cents === 'number'
            ? t('deals.target_label', { price: formatMoneyCents(item.target_price_cents, item.last_currency) })
            : '';
          const ageStr = formatAgeLabel(item.price_age_hours);
          const discountBadge = item.last_discount_pct > 0 ? `<span class="badge">-${Number(item.last_discount_pct)}%</span>` : '';
          const errStr = item.fetch_error ? ` · ${escapeHtml(item.fetch_error)}` : '';
          return `
            <div class="backlog-row" data-deal-row="${escapeHtml(item.id)}">
              <div class="grow">
                <strong>${escapeHtml(item.name)}</strong>
                ${discountBadge}
                <span class="badge">${escapeHtml(item.manual ? t('deals.manual_badge') : 'Steam')}</span>
                <small class="description">${escapeHtml([priceStr, lowestStr, targetStr, ageStr].filter(Boolean).join(' · '))}${errStr}</small>
              </div>
              ${renderSparklineSvg(item.history)}
              <button type="button" class="icon-button" data-deal-log="${escapeHtml(item.id)}">${escapeHtml(t('deals.log_price'))}</button>
              <button type="button" class="icon-button" data-deal-unwatch="${escapeHtml(item.id)}">${escapeHtml(t('deals.unwatch'))}</button>
            </div>`;
        }).join('')
        : `<p class="description">${escapeHtml(t('deals.empty'))}</p>`;

      body.innerHTML = `
        <div class="backlog-controls">
          <button type="button" class="primary" id="dealsRefreshBtn">${escapeHtml(t('deals.refresh_now'))}</button>
          <span class="description">${escapeHtml(t('deals.host_disclosure'))}</span>
        </div>
        <div class="detail-card">
          <form class="extras" id="dealsSearchForm">
            <input class="search grow" id="dealsSearchInput" maxlength="100" placeholder="${escapeHtml(t('deals.search_placeholder'))}">
            <button type="submit" class="icon-button">${escapeHtml(t('deals.search_btn'))}</button>
          </form>
          <div id="dealsSearchResults" class="history-list"></div>
        </div>
        <div class="detail-card">
          <h4>${escapeHtml(t('deals.manual_add_title'))}</h4>
          <form class="extras" id="dealsManualForm">
            <input class="search" name="name" maxlength="200" required placeholder="${escapeHtml(t('deals.manual_name'))}">
            <input type="number" step="0.01" min="0" name="target" placeholder="${escapeHtml(t('deals.target_placeholder'))}">
            <button type="submit" class="icon-button">${escapeHtml(t('deals.add_manual'))}</button>
          </form>
        </div>
        <div class="history-list" id="dealsWatchlistRows">${rowsHtml}</div>
      `;

      body.querySelector('#dealsRefreshBtn')?.addEventListener('click', async ev => {
        const btn = ev.currentTarget;
        if (btn) btn.disabled = true;
        try {
          const queued = await api('/api/v2/deals/refresh', { method: 'POST', body: JSON.stringify({}) });
          notify(t('deals.refresh_started'));
          await waitForJob(queued.job_id);
          notify(t('deals.refresh_done'));
          await renderDialog();
        } catch (error) {
          notify(error.message || String(error));
        } finally {
          if (btn) btn.disabled = false;
        }
      });

      body.querySelector('#dealsSearchForm')?.addEventListener('submit', async ev => {
        ev.preventDefault();
        const q = body.querySelector('#dealsSearchInput')?.value.trim() || '';
        const box = body.querySelector('#dealsSearchResults');
        if (!q || !box) return;
        box.innerHTML = `<p class="description">${escapeHtml(t('common.loading'))}</p>`;
        try {
          const res = await api(`/api/v2/deals/search?q=${encodeURIComponent(q)}`);
          const hits = res.results || [];
          box.innerHTML = hits.length
            ? hits.map(hit => `
                <div class="backlog-row">
                  <div class="grow">
                    <strong>${escapeHtml(hit.name)}</strong>
                    <small class="description">${escapeHtml(formatMoneyCents(hit.final_cents, hit.currency))}</small>
                  </div>
                  <button type="button" class="icon-button" data-watch-appid="${escapeHtml(hit.appid)}" data-watch-name="${escapeHtml(hit.name)}" data-watch-art="${escapeHtml(hit.artwork_url || '')}">${escapeHtml(t('deals.watch_btn'))}</button>
                </div>`).join('')
            : `<p class="description">${escapeHtml(t('deals.search_empty'))}</p>`;
          box.querySelectorAll('[data-watch-appid]').forEach(btn => {
            btn.addEventListener('click', async () => {
              await api('/api/v2/deals/watch', {
                method: 'POST',
                body: JSON.stringify({
                  appid: btn.dataset.watchAppid,
                  name: btn.dataset.watchName,
                  artwork_url: btn.dataset.watchArt,
                  store: 'steam',
                }),
              });
              notify(t('deals.watched_added', { name: btn.dataset.watchName }));
              await renderDialog();
            });
          });
        } catch (error) {
          box.innerHTML = `<p class="description">${escapeHtml(error.message || String(error))}</p>`;
        }
      });

      body.querySelector('#dealsManualForm')?.addEventListener('submit', async ev => {
        ev.preventDefault();
        const form = ev.currentTarget;
        const rawTarget = form.target.value.trim();
        const targetCents = rawTarget === '' ? null : Math.max(0, Math.round(Number(rawTarget) * 100));
        try {
          await api('/api/v2/deals/watch', {
            method: 'POST',
            body: JSON.stringify({
              name: form.name.value.trim(),
              manual: true,
              store: 'manual',
              target_price_cents: targetCents,
            }),
          });
          notify(t('deals.watched_added', { name: form.name.value.trim() }));
          await renderDialog();
        } catch (error) {
          notify(error.message || String(error));
        }
      });

      body.querySelectorAll('[data-deal-log]').forEach(btn => {
        btn.addEventListener('click', async () => {
          const id = btn.dataset.dealLog;
          const entryDialog = document.createElement('dialog');
          entryDialog.innerHTML = `
            <div class="dialog-head">
              <h2>${escapeHtml(t('deals.log_price'))}</h2>
              <button type="button" class="icon-button" data-close>×</button>
            </div>
            <form class="form" id="dealEntryForm">
              <label class="field"><span>${escapeHtml(t('deals.field_price'))}</span><input type="number" step="0.01" min="0" name="price" required></label>
              <label class="field"><span>${escapeHtml(t('deals.field_currency'))}</span><input name="currency" maxlength="3" value="USD" required></label>
              <label class="field"><span>${escapeHtml(t('deals.field_note'))}</span><input name="note" maxlength="280"></label>
              <div class="dialog-actions"><button type="button" class="icon-button" data-close>${escapeHtml(t('common.cancel'))}</button><button type="submit" class="primary">${escapeHtml(t('common.save'))}</button></div>
            </form>`;
          const closeSub = () => { closeDialog(entryDialog); entryDialog.remove(); };
          entryDialog.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', closeSub));
          entryDialog.addEventListener('cancel', closeSub, { once: true });
          entryDialog.querySelector('#dealEntryForm')?.addEventListener('submit', async ev => {
            ev.preventDefault();
            const f = ev.currentTarget;
            await api('/api/v2/deals/entry', {
              method: 'POST',
              body: JSON.stringify({
                id,
                price_cents: Math.round(Number(f.price.value) * 100),
                currency: f.currency.value.trim().toUpperCase(),
                note: f.note.value.trim(),
              }),
            });
            closeSub();
            await renderDialog();
          });
          document.body.appendChild(entryDialog);
          openDialog(entryDialog);
        });
      });

      body.querySelectorAll('[data-deal-unwatch]').forEach(btn => {
        btn.addEventListener('click', async () => {
          await api('/api/v2/deals/unwatch', { method: 'POST', body: JSON.stringify({ id: btn.dataset.dealUnwatch }) });
          notify(t('deals.unwatched'));
          await renderDialog();
        });
      });
    } catch (error) {
      body.innerHTML = `<p class="description">${escapeHtml(error.message || String(error))}</p>`;
    }
  };
  await renderDialog();
}

export { openDealsDialog, watchSteamAppFromGame };
