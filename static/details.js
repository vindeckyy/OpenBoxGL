/* Popped-out game details: URL detection, geometry memory, and opening. */
import { token } from './state.js';

const GEOMETRY_KEY = 'openbox.details.geometry';
const DEFAULT_WIDTH = 900;
const DEFAULT_HEIGHT = 720;
const MARGIN = 40;

function clampGeometry(saved = {}) {
  const availLeft = Number(screen.availLeft) || 0;
  const availTop = Number(screen.availTop) || 0;
  const width = Math.min(screen.availWidth - MARGIN, Math.max(480, Number(saved.width) || DEFAULT_WIDTH));
  const height = Math.min(screen.availHeight - MARGIN, Math.max(360, Number(saved.height) || DEFAULT_HEIGHT));
  const left = Math.min(availLeft + screen.availWidth - width, Math.max(availLeft, Number(saved.left) || 0));
  const top = Math.min(availTop + screen.availHeight - height, Math.max(availTop, Number(saved.top) || 0));
  return { width, height, left, top };
}

function restoreGeometry(detailTarget) {
  if (!detailTarget) return;
  try {
    const saved = JSON.parse(localStorage.getItem(GEOMETRY_KEY) || 'null');
    if (!saved) return;
    const { width, height, left, top } = clampGeometry(saved);
    window.resizeTo(width, height);
    window.moveTo(left, top);
  } catch { /* geometry memory is optional */ }
}

/** Detect a details popup, add its back button, and restore geometry.
 *  Returns the game id from the query string, or null for the main window. */
export function setupDetailsPopup() {
  const detailTarget = new URLSearchParams(location.search).get('details');
  if (!detailTarget) return null;
  document.body.classList.add('details-popup');
  const back = document.createElement('button');
  back.type = 'button';
  back.className = 'icon-button details-popup-back';
  back.textContent = 'Back to library';
  back.onclick = () => {
    if (window.opener && !window.opener.closed) window.opener.focus();
    window.close();
  };
  document.body.appendChild(back);
  restoreGeometry(detailTarget);
  let geometryTimer = 0;
  const saveGeometry = () => {
    clearTimeout(geometryTimer);
    geometryTimer = setTimeout(() => {
      try {
        localStorage.setItem(GEOMETRY_KEY, JSON.stringify({
          width: window.outerWidth,
          height: window.outerHeight,
          left: window.screenX,
          top: window.screenY,
        }));
      } catch { /* storage can be unavailable */ }
    }, 250);
  };
  window.addEventListener('resize', saveGeometry);
  // Window moves do not emit resize. A pointerup after dragging the native
  // title bar and pagehide are the reliable moments to remember geometry.
  window.addEventListener('pointerup', saveGeometry);
  window.addEventListener('pagehide', saveGeometry);
  return detailTarget;
}

/** Open the details popup, centered, reusing the remembered geometry name. */
export function openDetailsPopup(gameId) {
  let geometry = { width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT, left: 0, top: 0 };
  try {
    const saved = JSON.parse(localStorage.getItem(GEOMETRY_KEY) || 'null');
    if (saved) geometry = clampGeometry(saved);
  } catch { /* use defaults */ }
  if (!geometry.left && !geometry.top) {
    geometry.left = Math.max(0, Math.round((screen.availWidth - geometry.width) / 2));
    geometry.top = Math.max(0, Math.round((screen.availHeight - geometry.height) / 2));
  }
  const id = encodeURIComponent(String(gameId));
  return window.open(
    `/?details=${id}&token=${encodeURIComponent(token)}`,
    'openbox-details',
    `popup=yes,width=${geometry.width},height=${geometry.height},left=${geometry.left},top=${geometry.top}`,
  );
}
