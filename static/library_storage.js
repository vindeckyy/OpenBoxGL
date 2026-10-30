/* Library folder tools in Settings > Library (ADR 0090; plan W1-11 and W3-06).
 *
 * The integrity report is read-only. The move wizard previews a plan, then applies exactly
 * that plan by id: the server recomputes the plan under the store lock and refuses with
 * MOVE_PLAN_STALE when the library changed after the preview. A move always leaves games
 * inside the new folder on portable paths, so the wizard has no path-mode choice.
 */
import { $, escapeHtml } from './util.js';
import { AppState, api, nativePickFolder } from './state.js';
import { t } from './i18n.js';

const CATEGORY_KEYS = {
  game_path_missing: 'library_storage.cat_game_path_missing',
  game_path_unreadable: 'library_storage.cat_game_path_unreadable',
  game_path_outside_root: 'library_storage.cat_path_outside_root',
  path_token_escape: 'library_storage.cat_path_token_escape',
  library_root_missing: 'library_storage.cat_library_root_missing',
  game_duplicate_id: 'library_storage.cat_game_duplicate_id',
  media_missing: 'library_storage.cat_media_missing',
  media_changed: 'library_storage.cat_media_changed',
  media_untracked: 'library_storage.cat_media_untracked',
};
const PLAN_COUNTS = ['tokenized', 'absolute_inside_old', 'missing', 'outside', 'symlink_outside'];

let previewedPlan = null;

function exampleText(examples) {
  const shown = (examples || []).slice(0, 3).map(item => (
    item && typeof item === 'object' ? (item.path || item.game_id || JSON.stringify(item)) : String(item)
  ));
  return shown.join(', ');
}

export function renderIntegrityReport(report) {
  const categories = Object.entries(report?.categories || {});
  if (!categories.length) return `<p>${escapeHtml(t('library_storage.integrity_clean'))}</p>`;
  const rows = categories.map(([name, info]) => {
    const label = CATEGORY_KEYS[name] ? t(CATEGORY_KEYS[name]) : t('library_storage.cat_other', { name });
    const examples = exampleText(info?.examples);
    const detail = examples ? ` <span class="muted">${escapeHtml(t('library_storage.integrity_examples', { examples }))}</span>` : '';
    return `<li><strong>${escapeHtml(label)}</strong>: ${escapeHtml(String(info?.count ?? 0))}${detail}</li>`;
  });
  return `<ul>${rows.join('')}</ul>`;
}

export async function checkLibraryIntegrity() {
  const out = $('libraryIntegrityResult');
  out.hidden = false;
  out.textContent = t('library_storage.integrity_checking');
  try {
    const report = await api('/api/v2/library/integrity');
    out.innerHTML = renderIntegrityReport(report);
  } catch (error) {
    out.textContent = error.message;
  }
}

function showMoveResult(html) {
  const out = $('libraryMoveResult');
  out.hidden = false;
  out.innerHTML = html;
}

function forgetPreview() {
  previewedPlan = null;
  $('libraryMoveApply').disabled = true;
}

export async function previewLibraryMove() {
  const newRoot = $('libraryMoveFolder').value.trim();
  if (!newRoot) {
    forgetPreview();
    showMoveResult(`<p>${escapeHtml(t('library_storage.move_need_folder'))}</p>`);
    return;
  }
  const oldRoot = AppState.appSettings?.library_root || '';
  try {
    const plan = await api('/api/v2/library/move/preview', {
      method: 'POST',
      body: JSON.stringify({ old_root: oldRoot, new_root: newRoot }),
    });
    const counts = plan.counts || {};
    const values = Object.fromEntries(PLAN_COUNTS.map(key => [key, counts[key] ?? 0]));
    previewedPlan = { planId: plan.plan_id, oldRoot, newRoot };
    $('libraryMoveApply').disabled = false;
    showMoveResult(`<p>${escapeHtml(t('library_storage.move_plan', values))}</p>`);
  } catch (error) {
    forgetPreview();
    showMoveResult(`<p>${escapeHtml(t('library_storage.move_failed', { message: error.message }))}</p>`);
  }
}

export async function applyLibraryMove() {
  if (!previewedPlan) return;
  const { planId, oldRoot, newRoot } = previewedPlan;
  try {
    const result = await api('/api/v2/library/move/apply', {
      method: 'POST',
      body: JSON.stringify({ old_root: oldRoot, new_root: newRoot, plan_id: planId }),
    });
    forgetPreview();
    if (AppState.appSettings) {
      AppState.appSettings.library_root = newRoot;
      AppState.appSettings.path_mode = 'portable';
    }
    showMoveResult(`<p>${escapeHtml(t('library_storage.move_done', { moved: result.moved ?? 0 }))}</p>`);
  } catch (error) {
    forgetPreview();
    showMoveResult(`<p>${escapeHtml(t('library_storage.move_failed', { message: error.message }))}</p>`);
  }
}

async function browseForLibraryFolder() {
  const folder = await nativePickFolder(t('library_storage.move_folder')).catch(() => null);
  if (folder) {
    $('libraryMoveFolder').value = folder;
    forgetPreview();
  }
}

export function initLibraryStorage() {
  $('libraryIntegrityCheck').onclick = checkLibraryIntegrity;
  $('libraryMoveBrowse').onclick = browseForLibraryFolder;
  $('libraryMoveFolder').oninput = forgetPreview;
  $('libraryMovePreview').onclick = previewLibraryMove;
  $('libraryMoveApply').onclick = applyLibraryMove;
}
