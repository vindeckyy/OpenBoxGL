/* constellation.js — library relationship graph (force-directed canvas). */
import { $, escapeHtml } from './util.js';
import { t } from './i18n.js';
import { AppState, api, media } from './state.js';

    const KIND_LABELS = {
      series: t('constellation.kind_series'),
      developer: t('constellation.kind_developer'),
      publisher: t('constellation.kind_publisher'),
      genre: t('constellation.kind_genre'),
      platform_family: t('constellation.kind_platform_family'),
      co_played: t('constellation.kind_co_played'),
    };
    const KIND_COLORS = {
      series: 'var(--constellation-edge-series)',
      developer: 'var(--constellation-edge-developer)',
      publisher: 'var(--constellation-edge-publisher)',
      genre: 'var(--constellation-edge-genre)',
      platform_family: 'var(--constellation-edge-platform_family)',
      co_played: 'var(--constellation-edge-co_played)',
    };
    const DEFAULT_KINDS = ['series','developer','publisher','genre','platform_family','co_played'];

    let canvas, ctx, dialog, container;
    let data = { nodes: [], edges: [] };
    let camera = { x: 0, y: 0, zoom: 1 };
    let dragging = null;
    let hovered = null;
    let lastMouse = { x: 0, y: 0 };
    let nodePos = [];
    let sim = null;

    function openConstellation() {
      if (!dialog) initDom();
      dialog.showModal();
      loadAndRender();
    }

    function initDom() {
      dialog = $('constellationDialog');
      canvas = $('constellationCanvas');
      container = $('constellationCanvasWrap');
      ctx = canvas.getContext('2d');

      $('closeConstellation').onclick = () => dialog.close();
      $('constellationRelayout').onclick = () => { startSim(); };
      $('constellationKinds').innerHTML = DEFAULT_KINDS.map(k => `<label class="chip"><input type="checkbox" value="${k}" checked> <span>${KIND_LABELS[k]}</span></label>`).join('');
      $('constellationLimit').onchange = () => loadAndRender();
      $('constellationKinds').onchange = () => loadAndRender();

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

    async function loadAndRender() {
      if (!canvas) return;
      $('constellationLoading').hidden = false;
      $('constellationEmpty').hidden = true;
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
        resizeCanvas();
        startSim();
      } catch(error) { console.error(error); }
    }

    function resizeCanvas() {
      const rect = container.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      canvas.style.width = rect.width + 'px';
      canvas.style.height = rect.height + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function startSim() {
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
            let f = repel / dist2;
            const d = Math.sqrt(dist2);
            dx /= d; dy /= d;
            a.x += dx * f * alpha;
            a.y += dy * f * alpha;
            b.x -= dx * f * alpha;
            b.y -= dy * f * alpha;
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
          a.x -= nx * f * alpha;
          a.y -= ny * f * alpha;
          b.x += nx * f * alpha;
          b.y += ny * f * alpha;
        }
        // Centering
        for (const p of nodePos) {
          p.x -= p.x * 0.01 * alpha;
          p.y -= p.y * 0.01 * alpha;
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
      for (const edge of data.edges) {
        const a = nodePos[edge.s], b = nodePos[edge.t];
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.strokeStyle = KIND_COLORS[edge.kind] || 'var(--text-muted)';
        ctx.globalAlpha = 0.2 + 0.5 * (edge.w || 0.3);
        ctx.lineWidth = 1;
        ctx.stroke();
      }
      ctx.globalAlpha = 1;

      // Nodes
      for (const node of data.nodes) {
        const p = nodePos[node.i];
        ctx.beginPath();
        ctx.arc(p.x, p.y, 16, 0, Math.PI * 2);
        ctx.fillStyle = 'var(--surface-card)';
        ctx.fill();
        ctx.strokeStyle = 'var(--border)';
        ctx.lineWidth = 2;
        ctx.stroke();
        if (node.has_cover) {
          // draw cover as small image; if not loaded, fallback to initial
        }
        ctx.fillStyle = 'var(--text)';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.font = '10px var(--font-body)';
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

    export { openConstellation };
