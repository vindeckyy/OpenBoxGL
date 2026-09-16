/* constellation.js — library relationship graph (force-directed canvas). */
import { $, escapeHtml, safeStorage } from './util.js';
import { t } from './i18n.js';
import { AppState, api, media, notify, notifyError } from './state.js';
import { openDialog } from './dialogs.js';

    // ponytail: labels resolve at render time (not module load) because the
    // locale dictionary is fetched asynchronously after boot; a frozen map
    // would keep raw keys when the dialog opens before the locale arrives.
    function kindLabels() {
      return {
        series: t('constellation.kind_series'),
        developer: t('constellation.kind_developer'),
        publisher: t('constellation.kind_publisher'),
        genre: t('constellation.kind_genre'),
        platform_family: t('constellation.kind_platform_family'),
        co_played: t('constellation.kind_co_played'),
      };
    }
    // ponytail: canvas 2d does not resolve CSS var() — an invalid color
    // assignment is silently ignored (keeps black), which rendered the whole
    // graph invisible on dark themes. Resolve tokens to computed values once
    // per render; keyword fallbacks keep headless/test contexts working.
    function cssToken(name, fallback) {
      try {
        const value = getComputedStyle(canvas || document.documentElement).getPropertyValue(name).trim();
        if (value) return value;
      } catch { /* no computed style available */ }
      return fallback;
    }
    function resolveColors() {
      return {
        series: cssToken('--constellation-edge-series', 'white'),
        developer: cssToken('--constellation-edge-developer', 'white'),
        publisher: cssToken('--constellation-edge-publisher', 'white'),
        genre: cssToken('--constellation-edge-genre', 'white'),
        platform_family: cssToken('--constellation-edge-platform_family', 'white'),
        co_played: cssToken('--constellation-edge-co_played', 'white'),
        nodeFill: cssToken('--surface-card', 'black'),
        nodeStroke: cssToken('--border-card', 'white'),
        nodeText: cssToken('--text', 'white'),
        path: cssToken('--focus', 'yellow'),
      };
    }
    let palette = null;
    const DEFAULT_KINDS = ['series','developer','publisher','genre','platform_family','co_played'];
    const VIEWPOINT_KEY = 'openbox-constellation-viewpoints';
    let viewpoints = [];
    let pathNodes = new Set();
    let pathEdges = new Set();

    let canvas, ctx, dialog, container;
    let cssW = 0, cssH = 0;
    let data = { nodes: [], edges: [] };
    let camera = { x: 0, y: 0, zoom: 1 };
    let dragging = null;
    let hovered = null;
    let lastMouse = { x: 0, y: 0 };
    let nodePos = [];
    let sim = null;

    function openConstellation() {
      if (!dialog) initDom();
      renderKindLabels();
      if (!dialog.open) openDialog(dialog);
      loadAndRender();
    }

    function renderKindLabels() {
      const labels = kindLabels();
      $('constellationKinds').innerHTML = DEFAULT_KINDS.map(k => `<label class="chip"><input type="checkbox" value="${k}" checked> <span>${labels[k]}</span></label>`).join('');
    }

    function initDom() {
      dialog = $('constellationDialog');
      canvas = $('constellationCanvas');
      container = $('constellationCanvasWrap');
      ctx = canvas.getContext('2d');

      $('closeConstellation').onclick = () => dialog.close();
      $('constellationRelayout').onclick = () => { startSim(); };
      renderKindLabels();
      $('constellationLimit').onchange = () => loadAndRender();
      $('constellationKinds').onchange = () => loadAndRender();
      ensureConstellationTools();

      canvas.onmousedown = e => {
        const p = pointOnCanvas(e.clientX, e.clientY);
        const n = nodeAt(p.x, p.y);
        if (n) {
          dragging = n;
        } else {
          dragging = null;
          canvas.style.cursor = 'grabbing';
        }
        lastMouse = { x: e.clientX, y: e.clientY };
      };
      window.addEventListener('mousemove', e => {
        if (e.target !== canvas) return;
        if (dragging && typeof dragging === 'object') {
          const p = pointOnCanvas(e.clientX, e.clientY);
          nodePos[dragging.i].x = p.x;
          nodePos[dragging.i].y = p.y;
          draw();
        } else if (e.buttons & 1) {
          const dx = e.clientX - lastMouse.x;
          const dy = e.clientY - lastMouse.y;
          camera.x += dx;
          camera.y += dy;
          lastMouse = { x: e.clientX, y: e.clientY };
          draw();
        }
        const p = pointOnCanvas(e.clientX, e.clientY);
        const n = nodeAt(p.x, p.y);
        hovered = n;
        canvas.style.cursor = n ? 'pointer' : 'default';
      });
      window.addEventListener('mouseup', () => { dragging = null; canvas.style.cursor = 'default'; });
      canvas.onwheel = e => {
        e.preventDefault();
        const zoomSpeed = 0.001;
        camera.zoom = Math.max(0.25, Math.min(4, camera.zoom - e.deltaY * zoomSpeed));
        draw();
      };
      canvas.onclick = e => {
        const p = pointOnCanvas(e.clientX, e.clientY);
        const n = nodeAt(p.x, p.y);
        if (n && n.game_id) {
          document.dispatchEvent(new CustomEvent('app:show-game', { detail: { gameId: n.game_id } }));
        }
      };
      window.addEventListener('resize', () => { resizeCanvas(); draw(); });
    }

    function pointOnCanvas(clientX, clientY) {
      const rect = canvas.getBoundingClientRect();
      const x = (clientX - rect.left - camera.x - rect.width / 2) / camera.zoom;
      const y = (clientY - rect.top - camera.y - rect.height / 2) / camera.zoom;
      return { x, y };
    }

    function nodeAt(x, y) {
      if (!nodePos.length) return null;
      const r = 18;
      for (let i = data.nodes.length - 1; i >= 0; i--) {
        const p = nodePos[i];
        if ((p.x - x) ** 2 + (p.y - y) ** 2 <= r * r) return data.nodes[i];
      }
      return null;
    }

    function ensureConstellationErrorHost() {
      let host = $('constellationError');
      if (host) return host;
      const wrap = document.querySelector('.constellation-wrap');
      if (!wrap) return null;
      host = document.createElement('p');
      host.id = 'constellationError';
      host.className = 'description';
      host.hidden = true;
      wrap.appendChild(host);
      return host;
    }

    function showConstellationError(error) {
      const host = ensureConstellationErrorHost();
      if (!host) return;
      const message = error?.message || String(error || '');
      host.innerHTML = `<span>${escapeHtml(t('constellation.load_failed'))}${message ? ` ${escapeHtml(message)}` : ''}</span> <button type="button" class="icon-button" id="constellationRetry">${escapeHtml(t('constellation.retry'))}</button>`;
      host.hidden = false;
      const retry = $('constellationRetry');
      if (retry) retry.onclick = () => { host.hidden = true; loadAndRender(); };
    }

    async function loadAndRender() {
      if (!canvas) return;
      $('constellationLoading').hidden = false;
      $('constellationEmpty').hidden = true;
      const errorHost = ensureConstellationErrorHost();
      if (errorHost) errorHost.hidden = true;
      const kinds = [...document.querySelectorAll('#constellationKinds input:checked')].map(i => i.value).join(',');
      const limit = $('constellationLimit').value;
      try {
        data = await api(`/api/v2/library/constellation?kinds=${kinds}&limit=${limit}`);
        if (!data.nodes || !data.nodes.length) {
          $('constellationLoading').hidden = true;
          $('constellationEmpty').hidden = false;
          return;
        }
        nodePos = data.nodes.map((_, i) => {
          const angle = i * 2.4;
          const r = 10 + i * 2;
          return { x: Math.cos(angle) * r, y: Math.sin(angle) * r };
        });
        palette = resolveColors();
        clearPath();
        resizeCanvas();
        refreshConstellationTools();
        $('constellationLoading').hidden = true;
        startSim();
      } catch(error) {
        // A failed fetch used to leave the spinner up forever; show the error
        // and a retry affordance instead of only logging it.
        $('constellationLoading').hidden = true;
        showConstellationError(error);
      }
    }

    function resizeCanvas() {
      const rect = container.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      cssW = rect.width;
      cssH = rect.height;
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      canvas.style.width = rect.width + 'px';
      canvas.style.height = rect.height + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function startSim() {
      palette = resolveColors();
      // ponytail: O(n²) per tick with clamped steps. If libraries grow past
      // the 1000-node cap or layout feels slow, graduate to Barnes-Hut.
      const MAX_STEP = 24;
      function clampStep(fx, fy) {
        const len = Math.hypot(fx, fy);
        if (len > MAX_STEP && len > 0) {
          const scale = MAX_STEP / len;
          return [fx * scale, fy * scale];
        }
        return [fx, fy];
      }
      let alpha = 1.0;
      const k = Math.sqrt((canvas.width * canvas.height) / data.nodes.length) * 0.5;
      const repel = k * k;
      function tick() {
        if (!alpha) return;
        // Repulsion
        for (let i = 0; i < data.nodes.length; i++) {
          for (let j = i + 1; j < data.nodes.length; j++) {
            const a = nodePos[i], b = nodePos[j];
            let dx = a.x - b.x;
            let dy = a.y - b.y;
            let dist2 = dx * dx + dy * dy || 1;
            const d = Math.sqrt(dist2);
            // Fruchterman-Reingold repulsion k²/d (linear decay pairs with
            // the d²/k attraction below so pairs settle near distance k).
            let f = repel / d;
            dx /= d; dy /= d;
            const [sx, sy] = clampStep(dx * f * alpha, dy * f * alpha);
            a.x += sx;
            a.y += sy;
            b.x -= sx;
            b.y -= sy;
          }
        }
        // Attraction along edges
        for (const edge of data.edges) {
          const a = nodePos[edge.s], b = nodePos[edge.t];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const f = (dist * dist) / k * edge.w;
          const nx = dx / dist, ny = dy / dist;
          const [sx, sy] = clampStep(nx * f * alpha, ny * f * alpha);
          a.x -= sx;
          a.y -= sy;
          b.x += sx;
          b.y += sy;
        }
        // Centering
        for (const p of nodePos) {
          p.x -= p.x * 0.01 * alpha;
          p.y -= p.y * 0.01 * alpha;
        }
        // Bound: edgeless graphs have no attraction to balance repulsion,
        // so pairs drift far apart. Clamp into the largest circle that fits
        // inside the viewport (pan/zoom still available for crowded graphs).
        if (cssW > 0 && cssH > 0) {
          const boundR = Math.min(cssW, cssH) / 2 * 0.95;
          for (const p of nodePos) {
            const dist = Math.hypot(p.x, p.y);
            if (dist > boundR && dist > 0) {
              const s = boundR / dist;
              p.x *= s;
              p.y *= s;
            }
          }
        }
        alpha *= 0.985;
        draw();
        if (alpha > 0.02) requestAnimationFrame(tick);
        else { alpha = 0; draw(); }
      }
      tick();
    }

    function draw() {
      const rect = canvas.getBoundingClientRect();
      ctx.clearRect(0, 0, rect.width, rect.height);
      ctx.save();
      ctx.translate(camera.x + rect.width / 2, camera.y + rect.height / 2);
      ctx.scale(camera.zoom, camera.zoom);

      // Edges
      const colors = palette || resolveColors();
      for (const edge of data.edges) {
        const a = nodePos[edge.s], b = nodePos[edge.t];
        const onPath = pathEdges.has(edgeKey(edge.s, edge.t));
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.strokeStyle = onPath ? colors.path : (colors[edge.kind] || colors.genre);
        ctx.globalAlpha = onPath ? 1 : 0.2 + 0.5 * (edge.w || 0.3);
        ctx.lineWidth = onPath ? 3 : 1;
        ctx.stroke();
      }
      ctx.globalAlpha = 1;

      // Nodes
      for (const node of data.nodes) {
        const p = nodePos[node.i];
        ctx.beginPath();
        ctx.arc(p.x, p.y, 16, 0, Math.PI * 2);
        ctx.fillStyle = colors.nodeFill;
        ctx.fill();
        const onPath = pathNodes.has(String(node.game_id || ''));
        ctx.strokeStyle = onPath ? colors.path : colors.nodeStroke;
        ctx.lineWidth = onPath ? 3 : 2;
        ctx.stroke();
        if (node.has_cover) {
          // draw cover as small image; if not loaded, fallback to initial
        }
        ctx.fillStyle = colors.nodeText;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.font = '10px sans-serif';
        ctx.fillText(node.name.slice(0, 1).toUpperCase(), p.x, p.y);
      }

      ctx.restore();

      // Tooltip
      const tip = $('constellationTooltip');
      if (hovered) {
        tip.hidden = false;
        tip.textContent = hovered.name;
        tip.style.left = (lastMouse.x + 12) + 'px';
        tip.style.top = (lastMouse.y + 12) + 'px';
      } else {
        tip.hidden = true;
      }
    }

    // --- F12: viewpoints, path finding, PNG export -------------------------
    function selectedKinds() {
      return [...document.querySelectorAll('#constellationKinds input:checked')].map(input => input.value);
    }

    function loadViewpoints() {
      try {
        const parsed = JSON.parse(safeStorage.get(VIEWPOINT_KEY) || '[]');
        viewpoints = Array.isArray(parsed) ? parsed.filter(item => item && item.name && item.camera) : [];
      } catch {
        viewpoints = [];
      }
      return viewpoints;
    }

    function persistViewpoints() {
      safeStorage.set(VIEWPOINT_KEY, JSON.stringify(viewpoints.slice(0, 24)));
    }

    function currentViewpoint(name) {
      return {
        name: String(name || '').trim().slice(0, 60),
        saved_at: new Date().toISOString(),
        camera: { x: camera.x, y: camera.y, zoom: camera.zoom },
        kinds: selectedKinds(),
        limit: String($('constellationLimit')?.value || '400'),
      };
    }

    function applyViewpoint(viewpoint) {
      if (!viewpoint || !viewpoint.camera) return false;
      camera = {
        x: Number(viewpoint.camera.x) || 0,
        y: Number(viewpoint.camera.y) || 0,
        zoom: Math.max(0.25, Math.min(4, Number(viewpoint.camera.zoom) || 1)),
      };
      const kinds = new Set(Array.isArray(viewpoint.kinds) ? viewpoint.kinds : DEFAULT_KINDS);
      document.querySelectorAll('#constellationKinds input').forEach(input => { input.checked = kinds.has(input.value); });
      if (viewpoint.limit && $('constellationLimit')) $('constellationLimit').value = String(viewpoint.limit);
      draw();
      return true;
    }

    function bfsPath(nodes, edges, fromId, toId) {
      const from = String(fromId || '');
      const to = String(toId || '');
      if (!from || !to) return null;
      if (from === to) return [from];
      const adjacency = new Map();
      for (const edge of edges || []) {
        const a = String(nodes[edge.s]?.game_id || '');
        const b = String(nodes[edge.t]?.game_id || '');
        if (!a || !b) continue;
        if (!adjacency.has(a)) adjacency.set(a, []);
        if (!adjacency.has(b)) adjacency.set(b, []);
        adjacency.get(a).push(b);
        adjacency.get(b).push(a);
      }
      if (!adjacency.has(from) || !adjacency.has(to)) return null;
      const previous = new Map([[from, null]]);
      const queue = [from];
      let head = 0;
      while (head < queue.length) {
        const current = queue[head++];
        if (current === to) break;
        for (const next of adjacency.get(current) || []) {
          if (previous.has(next)) continue;
          previous.set(next, current);
          queue.push(next);
        }
      }
      if (!previous.has(to)) return null;
      const path = [];
      let cursor = to;
      while (cursor !== null) {
        path.push(cursor);
        cursor = previous.get(cursor);
      }
      return path.reverse();
    }

    function edgeKey(a, b) {
      return `${a}-${b}`;
    }

    function clearPath() {
      pathNodes = new Set();
      pathEdges = new Set();
    }

    function highlightPath(path) {
      clearPath();
      if (!path || path.length < 2) return path || null;
      const indexByGame = new Map(data.nodes.map((node, index) => [String(node.game_id || ''), index]));
      pathNodes = new Set(path);
      for (let i = 0; i < path.length - 1; i += 1) {
        const s = indexByGame.get(path[i]);
        const tIndex = indexByGame.get(path[i + 1]);
        if (s === undefined || tIndex === undefined) continue;
        pathEdges.add(edgeKey(s, tIndex));
        pathEdges.add(edgeKey(tIndex, s));
      }
      draw();
      return path;
    }

    function findPathBetweenGames() {
      const from = $('constellationPathFrom')?.value || '';
      const to = $('constellationPathTo')?.value || '';
      const path = bfsPath(data.nodes, data.edges, from, to);
      if (!path) {
        clearPath();
        draw();
        notify('warning', t('constellation.path_none'));
        return null;
      }
      highlightPath(path);
      const names = path.map(gameId => data.nodes.find(node => String(node.game_id || '') === gameId)?.name || gameId);
      notify('success', t('constellation.path_found', { count: path.length - 1, path: names.join(' → ') }));
      return path;
    }

    function refreshConstellationTools() {
      const select = $('constellationViewpoint');
      if (select) {
        loadViewpoints();
        const previous = select.value;
        select.innerHTML = viewpoints.length
          ? viewpoints.map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join('')
          : `<option value="">${escapeHtml(t('constellation.viewpoint_none'))}</option>`;
        if (previous && viewpoints.some(item => item.name === previous)) select.value = previous;
      }
      const options = data.nodes
        .filter(node => node.game_id)
        .map(node => `<option value="${escapeHtml(node.game_id)}">${escapeHtml(node.name)}</option>`)
        .join('');
      for (const id of ['constellationPathFrom', 'constellationPathTo']) {
        const host = $(id);
        if (host) host.innerHTML = options;
      }
    }

    async function saveCurrentViewpoint() {
      const { promptInput } = await import('./dialogs.js');
      const name = await promptInput({ title: t('constellation.viewpoint_save'), label: t('constellation.viewpoint_name'), defaultValue: '' });
      if (name === null || !String(name).trim()) return;
      loadViewpoints();
      const index = viewpoints.findIndex(item => item.name === String(name).trim());
      const viewpoint = currentViewpoint(name);
      if (index >= 0) viewpoints[index] = viewpoint;
      else viewpoints.unshift(viewpoint);
      persistViewpoints();
      refreshConstellationTools();
      notify('success', t('constellation.viewpoint_saved', { name: viewpoint.name }));
    }

    function deleteCurrentViewpoint() {
      const select = $('constellationViewpoint');
      const name = select?.value || '';
      if (!name) return;
      loadViewpoints();
      viewpoints = viewpoints.filter(item => item.name !== name);
      persistViewpoints();
      refreshConstellationTools();
      notify('success', t('constellation.viewpoint_deleted', { name }));
    }

    function exportConstellationPng() {
      if (!canvas) return;
      try {
        const link = document.createElement('a');
        link.download = `openbox-constellation-${Date.now()}.png`;
        link.href = canvas.toDataURL('image/png');
        link.click();
        notify('success', t('constellation.export_done'));
      } catch (error) {
        notifyError(error, t('constellation.export_failed'));
      }
    }

    function ensureConstellationTools() {
      if ($('constellationTools')) return $('constellationTools');
      const wrap = document.querySelector('.constellation-wrap');
      if (!wrap) return null;
      const host = document.createElement('div');
      host.className = 'constellation-tools';
      host.id = 'constellationTools';
      host.innerHTML = `
        <div class="constellation-tool-row">
          <label class="field"><span>${escapeHtml(t('constellation.viewpoint'))}</span><select id="constellationViewpoint"></select></label>
          <button type="button" class="icon-button" id="constellationSaveViewpoint">${escapeHtml(t('constellation.viewpoint_save'))}</button>
          <button type="button" class="icon-button" id="constellationLoadViewpoint">${escapeHtml(t('constellation.viewpoint_load'))}</button>
          <button type="button" class="icon-button" id="constellationDeleteViewpoint">${escapeHtml(t('constellation.viewpoint_delete'))}</button>
          <button type="button" class="icon-button" id="constellationExportPng">${escapeHtml(t('constellation.export_png'))}</button>
        </div>
        <div class="constellation-tool-row">
          <label class="field"><span>${escapeHtml(t('constellation.path_from'))}</span><select id="constellationPathFrom"></select></label>
          <label class="field"><span>${escapeHtml(t('constellation.path_to'))}</span><select id="constellationPathTo"></select></label>
          <button type="button" class="primary" id="constellationFindPath">${escapeHtml(t('constellation.path_find'))}</button>
          <button type="button" class="icon-button" id="constellationClearPath">${escapeHtml(t('constellation.path_clear'))}</button>
        </div>`;
      wrap.appendChild(host);
      $('constellationSaveViewpoint').onclick = () => saveCurrentViewpoint();
      $('constellationLoadViewpoint').onclick = () => {
        loadViewpoints();
        const name = $('constellationViewpoint')?.value || '';
        const viewpoint = viewpoints.find(item => item.name === name);
        if (applyViewpoint(viewpoint)) {
          notify('success', t('constellation.viewpoint_loaded', { name }));
          loadAndRender();
        }
      };
      $('constellationDeleteViewpoint').onclick = () => deleteCurrentViewpoint();
      $('constellationExportPng').onclick = () => exportConstellationPng();
      $('constellationFindPath').onclick = () => findPathBetweenGames();
      $('constellationClearPath').onclick = () => { clearPath(); draw(); };
      return host;
    }

    export { openConstellation, bfsPath };
