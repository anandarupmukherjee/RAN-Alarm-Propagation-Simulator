/* ═══════════════════════════════════════════════════════════════════
   app.js — RAN Network Simulator Frontend
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

// ─── Constants ────────────────────────────────────────────────────────────────
const API   = '/api';
const MAX_CARDS = 150;

const SEV_COLOR = {
  Critical: '#ff3b30', Major: '#ff9500', Minor: '#ffd60a', Warning: '#4d9fff'
};
const SEV_CLASS = {
  Critical: 'h-critical', Major: 'h-major', Minor: 'h-minor', Warning: 'h-warning'
};
// ─── State ────────────────────────────────────────────────────────────────────
let cy;               // Cytoscape instance
let sessionId = null;
let eventSource = null;
let alarmTypes = [];  // from /api/alarm-types
let topology = { nodes: [], edges: [] };

let counters = { total: 0, Critical: 0, Major: 0, Minor: 0, Warning: 0 };
let nodeAlarmCounts = {};    // nodeId → count
let nodeMaxSeverity = {};    // nodeId → severity string
let nodeLastAlarm   = {};    // nodeId → alarm name
let nodeSinr        = {};    // nodeId → dBm
let alarmTypeCounts = {};    // alarmName → count

let sevFilter   = 'all';
let isPaused    = false;
let editMode    = 'select';
let edgeSource  = null;       // node being connected in addEdge mode
let selectedElement = null;
let simSpeed    = 60;

let alarmChart = null;        // Chart.js instance
let statsInterval = null;
let simTimeHours = 0.0;

// ─── Alarm export log (RAN_data schema) ────────────────────────────────────────
const MAX_EXPORT_ROWS = 250000;
let alarmLog = [];            // accumulated alarm records for CSV export
let exportTruncated = false;
let simStartTime = null;      // wall-clock anchor for synthetic "Occurred On"
let lastSimBySource = {};     // source → last sim_time (for Hours_since_prior)
let lastSimBySourceName = {}; // "source||name" → last sim_time (Hours_since_samealarm)

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initCytoscape();
  loadAlarmTypes();
  checkNs3Status();
  document.addEventListener('keydown', onKeyDown);
});

// ─── Cytoscape setup ──────────────────────────────────────────────────────────
function initCytoscape() {
  cy = cytoscape({
    container: document.getElementById('cy'),
    elements:  [],
    style: [
      {
        selector: 'node',
        style: {
          'background-color':    '#0b1829',
          'border-color':        '#1d3461',
          'border-width':        2,
          'width':               52, 'height': 52,
          'label':               'data(label)',
          'color':               '#8b9dc3',
          'font-size':           '11px',
          'text-valign':         'bottom',
          'text-margin-y':       6,
          'font-family':         'JetBrains Mono, monospace',
          'text-outline-width':  2,
          'text-outline-color':  '#030712',
          'shape':               'ellipse',
          'transition-property': 'border-color, background-color, border-width',
          'transition-duration': '0.3s',
        }
      },
      {
        selector: 'node.healthy',
        style: { 'border-color': '#00a152', 'border-width': 2 }
      },
      {
        selector: 'node.h-warning',
        style: { 'border-color': '#4d9fff', 'border-width': 2, 'background-color': '#0a1a2d' }
      },
      {
        selector: 'node.h-minor',
        style: { 'border-color': '#ffd60a', 'border-width': 3, 'background-color': '#2a2200' }
      },
      {
        selector: 'node.h-major',
        style: { 'border-color': '#ff9500', 'border-width': 3, 'background-color': '#2d1a00' }
      },
      {
        selector: 'node.h-critical',
        style: { 'border-color': '#ff3b30', 'border-width': 4, 'background-color': '#2d0a09',
                 'color': '#ff8580' }
      },
      {
        selector: 'node:selected',
        style: { 'border-color': '#00d4ff', 'border-width': 3 }
      },
      {
        selector: 'node.adding-edge',
        style: { 'border-color': '#b388ff', 'border-width': 3 }
      },
      {
        selector: 'edge',
        style: {
          'width':              2,
          'line-color':         '#1d3461',
          'curve-style':        'bezier',
          'target-arrow-shape':'none',
          'transition-property':'line-color, width',
          'transition-duration':'0.3s',
        }
      },
      {
        selector: 'edge.link-failed',
        style: { 'line-color': '#ff3b30', 'width': 2, 'line-style': 'dashed' }
      },
      {
        selector: 'edge.link-active',
        style: { 'line-color': '#00e676', 'width': 2 }
      },
      {
        selector: 'edge:selected',
        style: { 'line-color': '#00d4ff', 'width': 3 }
      },
    ],
    layout:   { name: 'preset' },
    userZoomingEnabled: true,
    userPanningEnabled: true,
    boxSelectionEnabled: false,
    autounselectify: false,
  });

  // ── Cytoscape events ───────────────────────────────────────────────────────
  cy.on('tap', 'node', e => {
    if (editMode === 'addEdge') {
      handleEdgeCreation(e.target);
    } else {
      selectNode(e.target);
    }
  });

  cy.on('tap', 'edge', e => {
    selectedElement = e.target;
    showEdgeInfo(e.target);
    hideNodeInfo();
  });

  cy.on('tap', e => {
    if (e.target === cy) {
      if (editMode === 'addNode') {
        addNodeAt(e.position);
      } else {
        deselectAll();
      }
      if (editMode === 'addEdge' && edgeSource) {
        clearEdgeSource();
      }
    }
  });

  cy.on('cxttap', 'node', e => {
    selectNode(e.target);
    openInjectModal();
  });

  cy.on('position', 'node', () => rebuildTopology());
}

// ─── Topology Editor ──────────────────────────────────────────────────────────
function setMode(mode) {
  editMode   = mode;
  edgeSource = null;
  clearEdgeSource();

  document.querySelectorAll('.tool-btn[id^=mode-]').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('mode-' + mode.toLowerCase());
  if (btn) btn.classList.add('active');

  const hint = document.getElementById('mode-hint');
  const hintMap = {
    select:  'Click to select. Drag to move. Right-click node to inject.',
    addNode: 'Click empty space to add a new eNodeB.',
    addEdge: 'Click source node, then target node to draw a backhaul link.',
  };
  hint.textContent = hintMap[mode] || '';
  hint.classList.toggle('active', mode !== 'select');
  cy.autoungrabify(mode === 'addEdge');
}

let nodeCounter = 1;
function addNodeAt(position) {
  const id   = `NODE-${nodeCounter++}`;
  const node = cy.add({
    group: 'nodes',
    data:  { id, label: id, site_id: id, ne_type: 'BTS3900 LTE' },
    position,
  });
  node.addClass('healthy');
  rebuildTopology();
  selectNode(node);
  populateNodeSelects();
}

function handleEdgeCreation(target) {
  if (!edgeSource) {
    edgeSource = target;
    target.addClass('adding-edge');
    document.getElementById('mode-hint').textContent =
      `Source: ${target.id()} — now click the target node`;
  } else {
    if (edgeSource.id() === target.id()) { clearEdgeSource(); return; }
    const exists = cy.edges(`[source = "${edgeSource.id()}"][target = "${target.id()}"]`).length > 0
                || cy.edges(`[source = "${target.id()}"][target = "${edgeSource.id()}"]`).length > 0;
    if (!exists) {
      cy.add({ group: 'edges', data: { source: edgeSource.id(), target: target.id() } });
      rebuildTopology();
    }
    clearEdgeSource();
    setMode('addEdge'); // stay in addEdge mode
  }
}

function clearEdgeSource() {
  if (edgeSource) { edgeSource.removeClass('adding-edge'); edgeSource = null; }
}

function deleteSelected() {
  cy.$(':selected').remove();
  rebuildTopology();
  hideNodeInfo(); hideEdgeInfo();
  populateNodeSelects();
}

function clearTopology() {
  if (!confirm('Clear all nodes and links?')) return;
  cy.elements().remove();
  topology = { nodes: [], edges: [] };
  nodeAlarmCounts = {}; nodeMaxSeverity = {}; nodeLastAlarm = {}; nodeSinr = {};
  renderHealthGrid();
  renderSinrGrid();
  populateNodeSelects();
}

// Render a {nodes, edges} topology object onto the canvas (shared by all loaders)
function applyTopology(data, message) {
  cy.elements().remove();
  data.nodes.forEach(n => {
    cy.add({ group: 'nodes', data: { id: n.id, label: n.label || n.id, site_id: n.site_id || n.id, ne_type: n.ne_type }, position: { x: n.x, y: n.y } });
  });
  data.edges.forEach(e => {
    cy.add({ group: 'edges', data: { source: e.source, target: e.target } });
  });
  cy.nodes().addClass('healthy');
  topology = data;
  cy.fit(cy.elements(), 30);
  populateNodeSelects();
  renderHealthGrid();
  renderSinrGrid();
  if (message) showToast(message, 'success');
}

// Dispatcher for the topology dropdown
async function loadPresetTopology(name) {
  if (!name) return;
  try {
    if (name === 'simple') {
      await loadDemoTopology();
    } else if (name === 'medium') {
      applyTopology(buildMediumTopology(), 'Medium topology loaded — 50 nodes, 5 aggregation rings');
    } else if (name === 'national') {
      const t = buildNationalTopology();
      applyTopology(t, `National RAN topology loaded — ${t.nodes.length} nodes, ${t.edges.length} links`);
    }
  } catch (e) {
    showToast('Could not load topology: ' + e.message, 'error');
  }
  // reset dropdown back to placeholder so the same option can be re-picked
  const sel = document.getElementById('topo-select');
  if (sel) sel.value = '';
}

async function loadDemoTopology() {
  const res  = await fetch(`${API}/demo-topology`);
  const data = await res.json();
  applyTopology(data, 'Demo topology loaded — 10 real BT sites');
}

// ─── Preset topology generators ────────────────────────────────────────────────
// Medium: 5 aggregation hubs in a backbone ring, each with a star of access eNodeBs.
function buildMediumTopology() {
  const nodes = [], edges = [];
  const HUBS = 5, SPOKES = 9;          // 5 hubs + 45 access = 50 nodes
  const cx = 1000, cy0 = 1000, hubR = 620, spokeR = 300;
  for (let h = 0; h < HUBS; h++) {
    const ha = (2 * Math.PI * h) / HUBS - Math.PI / 2;
    const hx = cx + hubR * Math.cos(ha);
    const hy = cy0 + hubR * Math.sin(ha);
    const hubId = `AGG-${h + 1}`;
    nodes.push({ id: hubId, label: hubId, ne_type: 'ATN 910 (Agg)', x: hx, y: hy });
    // backbone ring between hubs
    edges.push({ source: hubId, target: `AGG-${((h + 1) % HUBS) + 1}` });
    for (let s = 0; s < SPOKES; s++) {
      const sa = (2 * Math.PI * s) / SPOKES;
      const enbId = `ENB-${h + 1}${String(s + 1).padStart(2, '0')}`;
      nodes.push({
        id: enbId, label: enbId,
        ne_type: s % 3 === 0 ? 'BTS3900 LTE' : (s % 3 === 1 ? 'BTS3900 GSM' : 'RRU3953'),
        x: hx + spokeR * Math.cos(sa),
        y: hy + spokeR * Math.sin(sa),
      });
      edges.push({ source: hubId, target: enbId });
    }
  }
  return { nodes, edges };
}

// National RAN: 2 national cores → 6 regional cores (resilience ring) →
// 3 metro aggregation hubs per region → 6 access eNodeBs per metro hub.
function buildNationalTopology() {
  const nodes = [], edges = [];
  const cx = 1400, cy0 = 1400;
  const REGIONS = 6, METROS = 3, ACCESS = 6;
  const REGION_NAMES = ['London', 'South West', 'Midlands', 'North West', 'North East', 'Scotland'];

  // National core (two geo-redundant routers, linked)
  nodes.push({ id: 'NCORE-1', label: 'National Core 1', ne_type: 'NE40E (Core)', x: cx - 220, y: cy0 });
  nodes.push({ id: 'NCORE-2', label: 'National Core 2', ne_type: 'NE40E (Core)', x: cx + 220, y: cy0 });
  edges.push({ source: 'NCORE-1', target: 'NCORE-2' });

  const regionR = 1050, metroR = 360, accessR = 150;
  for (let r = 0; r < REGIONS; r++) {
    const ra = (2 * Math.PI * r) / REGIONS - Math.PI / 2;
    const rx = cx + regionR * Math.cos(ra);
    const ry = cy0 + regionR * Math.sin(ra);
    const rcId = `RCORE-${r + 1}`;
    nodes.push({ id: rcId, label: `${REGION_NAMES[r]} RCore`, ne_type: 'NE40E (Core)', x: rx, y: ry });
    // dual-home each regional core to both national cores
    edges.push({ source: rcId, target: 'NCORE-1' });
    edges.push({ source: rcId, target: 'NCORE-2' });
    // resilience ring between adjacent regional cores
    edges.push({ source: rcId, target: `RCORE-${((r + 1) % REGIONS) + 1}` });

    for (let m = 0; m < METROS; m++) {
      const ma = ra + (m - (METROS - 1) / 2) * 0.42;
      const mx = rx + metroR * Math.cos(ma);
      const my = ry + metroR * Math.sin(ma);
      const metroId = `METRO-${r + 1}-${m + 1}`;
      nodes.push({ id: metroId, label: metroId, ne_type: 'ATN 910 (Agg)', x: mx, y: my });
      edges.push({ source: rcId, target: metroId });

      for (let a = 0; a < ACCESS; a++) {
        const aa = (2 * Math.PI * a) / ACCESS;
        const enbId = `ENB-${r + 1}${m + 1}${String(a + 1).padStart(2, '0')}`;
        nodes.push({
          id: enbId, label: enbId,
          ne_type: a % 3 === 0 ? 'BTS3900 LTE' : (a % 3 === 1 ? 'BTS3900 GSM' : 'RTN 950 (MW)'),
          x: mx + accessR * Math.cos(aa),
          y: my + accessR * Math.sin(aa),
        });
        edges.push({ source: metroId, target: enbId });
      }
    }
  }
  return { nodes, edges };
}

function rebuildTopology() {
  topology = {
    nodes: cy.nodes().map(n => ({
      id:      n.id(),
      site_id: n.data('site_id') || n.id(),
      label:   n.data('label') || n.id(),
      ne_type: n.data('ne_type') || 'BTS3900 LTE',
      x:       n.position('x'),
      y:       n.position('y'),
    })),
    edges: cy.edges().map(e => ({
      source: e.data('source'),
      target: e.data('target'),
    })),
  };
}

function selectNode(node) {
  cy.$(':selected').unselect();
  node.select();
  selectedElement = node;
  showNodeInfo(node);
  hideEdgeInfo();
}

function deselectAll() {
  cy.$(':selected').unselect();
  selectedElement = null;
  hideNodeInfo(); hideEdgeInfo();
}

// ─── Node / Edge info panels ─────────────────────────────────────────────────
function showNodeInfo(node) {
  const id = node.id();
  document.getElementById('node-info').classList.remove('hidden');
  document.getElementById('ni-id').textContent = id;
  document.getElementById('ni-type').textContent = node.data('ne_type') || 'BTS3900 LTE';
  document.getElementById('ni-alarms').textContent = nodeAlarmCounts[id] || 0;
  document.getElementById('ni-sinr').textContent =
    nodeSinr[id] !== undefined ? `${nodeSinr[id].toFixed(1)} dBm` : '—';
  document.getElementById('ni-last').textContent = nodeLastAlarm[id] || '—';
  const neighbours = cy.edges(`[source="${id}"], [target="${id}"]`).map(e =>
    e.data('source') === id ? e.data('target') : e.data('source')
  );
  document.getElementById('ni-neighbours').textContent = neighbours.join(', ') || '—';
}

function hideNodeInfo() { document.getElementById('node-info').classList.add('hidden'); }

function showEdgeInfo(edge) {
  const src = edge.data('source'), tgt = edge.data('target');
  document.getElementById('edge-info').classList.remove('hidden');
  document.getElementById('ei-label').textContent = `${src} ↔ ${tgt}`;
  const failed = edge.hasClass('link-failed');
  const statusEl = document.getElementById('ei-status');
  statusEl.textContent = failed ? 'FAILED' : 'OK';
  statusEl.className = failed ? 'badge-err' : 'badge-ok';
}

function hideEdgeInfo() { document.getElementById('edge-info').classList.add('hidden'); }

function failSelectedEdge() {
  if (!selectedElement || !selectedElement.isEdge()) return;
  const src = selectedElement.data('source'), tgt = selectedElement.data('target');
  injectLinkFailure(src, tgt);
}

// ─── Simulation control ───────────────────────────────────────────────────────
async function startSimulation() {
  rebuildTopology();
  if (topology.nodes.length === 0) {
    showToast('Load or build a topology first', 'error'); return;
  }

  document.getElementById('btn-start').disabled = true;
  document.getElementById('btn-start').textContent = '⏳ Starting…';

  try {
    const res = await fetch(`${API}/simulate/start`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ topology, speed: simSpeed, propagation_prob: 0.35 }),
    });
    const data = await res.json();
    sessionId = data.session_id;

    // Reset the export log for this fresh session
    alarmLog = [];
    exportTruncated = false;
    lastSimBySource = {};
    lastSimBySourceName = {};
    simStartTime = new Date();

    document.getElementById('btn-stop').disabled   = false;
    document.getElementById('btn-inject').disabled = false;
    document.getElementById('btn-random').disabled = false;
    document.getElementById('btn-start').textContent = '▶ Running';
    document.getElementById('empty-state').classList.add('hidden');

    renderHealthGrid();
    renderSinrGrid();
    startSSE();
    startStatsPolling();
    showToast('Simulation started', 'success');
  } catch (e) {
    document.getElementById('btn-start').disabled = false;
    document.getElementById('btn-start').innerHTML = '<span>▶</span> Start Simulation';
    showToast('Failed to start: ' + e.message, 'error');
  }
}

async function stopSimulation() {
  if (!sessionId) return;
  stopSSE();
  stopStatsPolling();
  await fetch(`${API}/simulate/${sessionId}`, { method: 'DELETE' });
  sessionId = null;

  document.getElementById('btn-start').disabled   = false;
  document.getElementById('btn-start').innerHTML  = '<span>▶</span> Start Simulation';
  document.getElementById('btn-stop').disabled    = true;
  document.getElementById('btn-inject').disabled  = true;
  document.getElementById('btn-random').disabled  = true;
  showToast('Simulation stopped', 'info');
}

function updateSpeed(val) {
  simSpeed = parseInt(val);
  document.getElementById('speed-value').textContent = simSpeed + '×';
  document.getElementById('eng-speed').textContent = simSpeed + '× (1h/' + (1/simSpeed*3600).toFixed(1) + 's)';
  if (sessionId) {
    fetch(`${API}/simulate/${sessionId}/speed`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ speed: simSpeed }),
    });
  }
}

// ─── Server-Sent Events ───────────────────────────────────────────────────────
function startSSE() {
  stopSSE();
  eventSource = new EventSource(`${API}/stream/${sessionId}`);
  eventSource.onmessage = e => {
    try { handleEvent(JSON.parse(e.data)); } catch (_) {}
  };
  eventSource.onerror = () => {
    // Reconnect automatically after 2s
    setTimeout(() => { if (sessionId) startSSE(); }, 2000);
  };
}

function stopSSE() {
  if (eventSource) { eventSource.close(); eventSource = null; }
}

function handleEvent(event) {
  if (event.type === 'heartbeat') return;
  if (event.type === 'status') {
    console.log('[SSE status]', event.status);
    return;
  }
  if (event.type !== 'alarm') return;
  if (isPaused) return;

  // Update sim time
  if (event.sim_time) simTimeHours = event.sim_time;

  // Update counters
  counters.total++;
  const sev = event.severity || 'Major';
  if (counters[sev] !== undefined) counters[sev]++;

  updateStatBadges();

  // Update per-node state
  const nid = event.alarm_source;
  nodeAlarmCounts[nid] = (nodeAlarmCounts[nid] || 0) + 1;
  nodeLastAlarm[nid]   = event.alarm_name;
  if (event.sinr_dbm !== undefined) nodeSinr[nid] = event.sinr_dbm;

  // Track worst severity per node
  const sevOrder = { Warning: 1, Minor: 2, Major: 3, Critical: 4 };
  const prev = sevOrder[nodeMaxSeverity[nid]] || 0;
  if ((sevOrder[sev] || 0) > prev) nodeMaxSeverity[nid] = sev;

  // Update Cytoscape node style
  updateNodeStyle(nid, sev);

  // Alarm type chart
  alarmTypeCounts[event.alarm_name] = (alarmTypeCounts[event.alarm_name] || 0) + 1;

  // Record for CSV export (RAN_data schema)
  recordAlarmForExport(event, nid, sev);

  // Always render the card; hide it if it doesn't match the active filter
  renderAlarmCard(event);

  // Update dashboard
  updateDashboard(nid, event);
}

// ─── Alarm Card ───────────────────────────────────────────────────────────────
function renderAlarmCard(event) {
  const feed = document.getElementById('alarm-feed');
  const sev  = event.severity || 'Major';

  const card = document.createElement('div');
  card.className = `alarm-card ${sev === 'Critical' ? 'pulse-anim' : ''}`;
  card.dataset.sev = sev;
  if (sevFilter !== 'all' && sevFilter !== sev) card.style.display = 'none';
  card.dataset.nodeId = event.alarm_source;
  card.onclick = () => highlightNode(event.alarm_source);

  const ts = new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const nextCls = event.next_alarm === 'Noalarm' ? 'noalarm' : '';
  const cascadeBadge = event.cascade_depth > 0
    ? `<span class="ac-cascade">↗ cascade-${event.cascade_depth}</span>` : '';
  const ns3Badge = event.ns3_backed
    ? `<span class="ac-ns3">ns-3</span>` : '';

  card.innerHTML = `
    <span class="ac-sev" data-sev="${sev}"></span>
    <div class="ac-body">
      <div class="ac-name">${escHtml(event.alarm_name)}</div>
      <div class="ac-meta">
        <span class="ac-site">${escHtml(event.alarm_source)}</span>
        <span class="ac-ne">${escHtml(event.ne_type || '')}</span>
        ${ns3Badge}${cascadeBadge}
      </div>
      <div class="ac-next">→ <span class="${nextCls}">${escHtml(event.next_alarm || 'Noalarm')}</span></div>
    </div>
    <span class="ac-time">${ts}</span>
  `;

  feed.prepend(card);

  // Trim old cards
  while (feed.children.length > MAX_CARDS) {
    feed.removeChild(feed.lastChild);
  }
}

// ─── Node styling ─────────────────────────────────────────────────────────────
function updateNodeStyle(nodeId, severity) {
  const node = cy.getElementById(nodeId);
  if (!node.length) return;
  node.removeClass('healthy h-warning h-minor h-major h-critical');
  node.addClass(SEV_CLASS[severity] || 'h-major');

  // Flash animation
  node.style('border-width', '5');
  setTimeout(() => node.style('border-width', ''), 400);
}

function highlightNode(nodeId) {
  const node = cy.getElementById(nodeId);
  if (!node.length) return;
  cy.animate({ zoom: 1.5, center: { eles: node } }, { duration: 300 });
  selectNode(node);
}

// ─── Dashboard updates ────────────────────────────────────────────────────────
function updateStatBadges() {
  document.getElementById('stat-total').textContent    = counters.total;
  document.getElementById('stat-critical').textContent = counters.Critical;
  document.getElementById('stat-major').textContent    = counters.Major;
  document.getElementById('mc-critical').textContent   = counters.Critical;
  document.getElementById('mc-major').textContent      = counters.Major;
  document.getElementById('mc-minor').textContent      = counters.Minor;
  document.getElementById('mc-warning').textContent    = counters.Warning;

  const h = Math.floor(simTimeHours);
  const m = Math.floor((simTimeHours - h) * 60);
  const simTxt = `${h}h ${m}m`;
  document.getElementById('sim-time').textContent   = simTxt;
  document.getElementById('eng-simtime').textContent = simTxt;
}

function updateDashboard(nodeId, event) {
  // Health grid card
  const card = document.getElementById('hc-' + nodeId);
  if (card) {
    card.className = `health-card ${SEV_CLASS[event.severity] || 'h-major'}`;
    card.querySelector('.hc-count').textContent = nodeAlarmCounts[nodeId] || 0;
    card.querySelector('.hc-last').textContent  = event.alarm_name;
    card.querySelector('.hc-last').title        = event.alarm_name;
  }

  // SINR bar
  if (event.sinr_dbm !== undefined) {
    const sinrRow = document.getElementById('sinr-' + nodeId);
    if (sinrRow) {
      const pct = Math.max(0, Math.min(100, ((event.sinr_dbm + 120) / 80) * 100));
      const fill = sinrRow.querySelector('.sinr-fill');
      const val  = sinrRow.querySelector('.sinr-val');
      fill.style.width = pct + '%';
      fill.style.background = event.sinr_dbm < -90
        ? 'var(--sev-c)' : event.sinr_dbm < -80
        ? 'var(--sev-M)' : 'var(--green)';
      val.textContent = event.sinr_dbm.toFixed(1) + ' dB';
    }
  }

  // Update alarm type chart periodically
  if (counters.total % 5 === 0) updateAlarmChart();
}

// ─── Health grid render ───────────────────────────────────────────────────────
function renderHealthGrid() {
  const grid = document.getElementById('node-health-grid');
  grid.innerHTML = '';
  if (topology.nodes.length === 0) {
    grid.innerHTML = '<div class="health-empty">No nodes — load topology first</div>';
    return;
  }
  topology.nodes.forEach(n => {
    const sev  = nodeMaxSeverity[n.id] || '';
    const div  = document.createElement('div');
    div.className = `health-card ${sev ? SEV_CLASS[sev] : 'h-ok'}`;
    div.id = `hc-${n.id}`;
    div.onclick = () => highlightNode(n.id);
    div.innerHTML = `
      <div class="hc-id">${escHtml(n.label || n.id)}</div>
      <div class="hc-count">${nodeAlarmCounts[n.id] || 0}</div>
      <div class="hc-last" title="">${escHtml(nodeLastAlarm[n.id] || 'OK')}</div>
    `;
    grid.appendChild(div);
  });
}

// ─── SINR grid render ─────────────────────────────────────────────────────────
function renderSinrGrid() {
  const grid = document.getElementById('sinr-grid');
  grid.innerHTML = '';
  topology.nodes.forEach(n => {
    const div = document.createElement('div');
    div.className = 'sinr-row'; div.id = `sinr-${n.id}`;
    const sinr = nodeSinr[n.id] !== undefined ? nodeSinr[n.id] : -75;
    const pct  = Math.max(0, Math.min(100, ((sinr + 120) / 80) * 100));
    div.innerHTML = `
      <span class="sinr-id">${escHtml(n.id)}</span>
      <div class="sinr-bar"><div class="sinr-fill" style="width:${pct}%;background:var(--green)"></div></div>
      <span class="sinr-val">${sinr.toFixed(1)} dB</span>
    `;
    grid.appendChild(div);
  });
}

// ─── Alarm type chart ─────────────────────────────────────────────────────────
function initAlarmChart() {
  const ctx = document.getElementById('alarm-type-chart').getContext('2d');
  alarmChart = new Chart(ctx, {
    type: 'bar',
    data: { labels: [], datasets: [{ data: [], backgroundColor: '#0d4d9e', borderColor: '#1a6fd8', borderWidth: 1 }] },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: {
        label: ctx => ` ${ctx.raw} alarms`
      }}},
      scales: {
        x: { ticks: { color: '#4a5b82', font: { size: 9 } }, grid: { color: '#1d3461' } },
        y: { ticks: { color: '#8b9dc3', font: { size: 9, family: 'JetBrains Mono' } }, grid: { display: false } },
      }
    }
  });
}

function updateAlarmChart() {
  if (!alarmChart) initAlarmChart();
  const sorted = Object.entries(alarmTypeCounts).sort((a,b) => b[1]-a[1]).slice(0, 8);
  alarmChart.data.labels   = sorted.map(([k]) => k.length > 28 ? k.slice(0,25)+'…' : k);
  alarmChart.data.datasets[0].data = sorted.map(([,v]) => v);
  alarmChart.data.datasets[0].backgroundColor = sorted.map(([k]) => {
    if (k.includes('S1') || k.includes('Ethernet')) return '#7a1f1f';
    if (k.includes('Cell Blocked') || k.includes('Unavailable')) return '#7a4a00';
    if (k.includes('RF') || k.includes('ALD'))  return '#4a5b00';
    if (k.includes('GSM'))                        return '#1a3a7a';
    return '#0d3050';
  });
  alarmChart.update('none');
}

// ─── Stats polling ────────────────────────────────────────────────────────────
function startStatsPolling() {
  stopStatsPolling();
  statsInterval = setInterval(pollStats, 5000);
}
function stopStatsPolling() {
  if (statsInterval) { clearInterval(statsInterval); statsInterval = null; }
}

async function pollStats() {
  if (!sessionId) return;
  try {
    const [statsRes, ns3Res] = await Promise.all([
      fetch(`${API}/simulate/${sessionId}/stats`),
      fetch(`${API}/ns3/status`),
    ]);
    const stats = await statsRes.json();
    const ns3   = await ns3Res.json();

    document.getElementById('eng-nodes').textContent  = stats.node_counts ? Object.keys(stats.node_counts).length : '—';
    document.getElementById('eng-type').textContent   = ns3.ns3_available ? 'ns-3 LTE ✓' : 'ns-3 offline';
    document.getElementById('eng-speed').textContent  = `${simSpeed}×`;

    // Update ns-3 badge (ns-3 LTE is the mandatory simulation core)
    const badge = document.getElementById('ns3-badge');
    const label = badge.querySelector('.badge-label');
    if (ns3.ns3_available) {
      badge.className = 'ns3-badge active';
      label.textContent = 'ns-3 LTE';
    } else {
      badge.className = 'ns3-badge fallback';
      label.textContent = 'ns-3 offline';
    }
  } catch (_) {}
}

async function checkNs3Status() {
  try {
    const res  = await fetch(`${API}/ns3/status`);
    const data = await res.json();
    const badge = document.getElementById('ns3-badge');
    if (data.ns3_available) {
      badge.className = 'ns3-badge active';
      badge.querySelector('.badge-label').textContent = 'ns-3 LTE';
    } else if (!data.error) {
      badge.className = 'ns3-badge fallback';
      badge.querySelector('.badge-label').textContent = 'ns-3 offline';
    }
  } catch(_) {}
}

// ─── Alarm types loader ───────────────────────────────────────────────────────
async function loadAlarmTypes() {
  try {
    const res = await fetch(`${API}/alarm-types`);
    const d   = await res.json();
    alarmTypes = d.alarm_types || [];
  } catch (_) {}
}

// ─── Inject Fault Modal (categorised) ─────────────────────────────────────────
let alarmCatalog = null;   // { categories:[{name,count,alarms:[{name,severity,count}]}], ... }

async function loadAlarmCatalog() {
  if (alarmCatalog) return alarmCatalog;
  const res = await fetch(`${API}/alarm-catalog`);
  alarmCatalog = await res.json();
  // Populate the category dropdown once
  const sel = document.getElementById('inj-category');
  const total = alarmCatalog.categories.reduce((n, c) => n + c.count, 0);
  sel.innerHTML = `<option value="">All categories (${total})</option>` +
    alarmCatalog.categories.map(c =>
      `<option value="${escHtml(c.name)}">${escHtml(c.name)} (${c.count})</option>`).join('');
  return alarmCatalog;
}

// Flatten alarms matching the current category + severity filters
function injectFilteredAlarms() {
  if (!alarmCatalog) return [];
  const cat = document.getElementById('inj-category').value;
  const sev = document.getElementById('inj-sevfilter').value;
  let alarms = [];
  alarmCatalog.categories.forEach(c => {
    if (!cat || c.name === cat) alarms = alarms.concat(c.alarms);
  });
  if (sev) alarms = alarms.filter(a => a.severity === sev);
  return alarms.sort((a, b) => b.count - a.count);
}

function onInjectCategoryChange() {
  const alarms = injectFilteredAlarms();
  const sel = document.getElementById('inj-alarm');
  sel.innerHTML = alarms.length
    ? alarms.map(a => `<option value="${escHtml(a.name)}" data-sev="${a.severity}">${escHtml(a.name)} — ${a.severity}</option>`).join('')
    : '<option value="">— no alarms match —</option>';
  document.getElementById('inj-alarm-count').textContent =
    alarms.length ? `(${alarms.length})` : '';
  updateAlarmPreview();
}

async function openInjectModal() {
  if (!sessionId) { showToast('Start simulation first', 'error'); return; }
  populateNodeSelects();
  if (selectedElement && selectedElement.isNode()) {
    document.getElementById('inj-node').value = selectedElement.id();
  }
  document.getElementById('inject-overlay').classList.remove('hidden');
  document.getElementById('inject-modal').classList.remove('hidden');
  try {
    await loadAlarmCatalog();
    onInjectCategoryChange();
  } catch (e) {
    showToast('Could not load alarm catalog: ' + e.message, 'error');
  }
}

function closeInjectModal() {
  document.getElementById('inject-overlay').classList.add('hidden');
  document.getElementById('inject-modal').classList.add('hidden');
}

function updateAlarmPreview() {
  const sel  = document.getElementById('inj-alarm');
  const opt  = sel.options[sel.selectedIndex];
  const name = opt ? opt.value : '';
  const sev  = opt ? (opt.dataset.sev || '—') : '—';
  document.getElementById('preview-name').textContent = name || '—';
  const sevBadge = document.getElementById('preview-sev');
  sevBadge.textContent = sev;
  sevBadge.dataset.sev = sev;
  sevBadge.className   = 'severity-badge';
}

async function confirmInject() {
  const nodeId    = document.getElementById('inj-node').value;
  const alarmName = document.getElementById('inj-alarm').value;
  if (!nodeId)    { showToast('Select a node', 'error'); return; }
  if (!alarmName) { showToast('Select an alarm type', 'error'); return; }
  closeInjectModal();
  await injectAlarm(nodeId, alarmName);
}

// Inject a specific real alarm by name (manual_injection path)
async function injectAlarm(nodeId, alarmName) {
  if (!sessionId) return;
  try {
    await fetch(`${API}/simulate/${sessionId}/inject`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ node_id: nodeId, event_type: 'manual_injection', alarm_name: alarmName }),
    });
    showToast(`Injected "${alarmName}" on ${nodeId}`, 'success');
  } catch (e) {
    showToast('Inject failed: ' + e.message, 'error');
  }
}

// Inject a coarse ns-3 event type (used by cascade/link helpers)
async function injectFault(nodeId, eventType) {
  if (!sessionId) return;
  try {
    await fetch(`${API}/simulate/${sessionId}/inject`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ node_id: nodeId, event_type: eventType }),
    });
    showToast(`Injected ${eventType} on ${nodeId}`, 'success');
  } catch (e) {
    showToast('Inject failed: ' + e.message, 'error');
  }
}

function injectOnSelected() {
  if (!selectedElement || !selectedElement.isNode()) return;
  const nodeId = selectedElement.id();
  document.getElementById('inj-node').value = nodeId;
  openInjectModal();
}

async function injectRandomFault() {
  if (!sessionId || topology.nodes.length === 0) return;
  const node = topology.nodes[Math.floor(Math.random() * topology.nodes.length)];
  try {
    await loadAlarmCatalog();
    const all = injectFilteredAlarmsAll();
    const a   = all[Math.floor(Math.random() * all.length)];
    await injectAlarm(node.id, a.name);
  } catch (e) {
    showToast('Random inject failed: ' + e.message, 'error');
  }
}

// All catalog alarms regardless of UI filters (for the Random button)
function injectFilteredAlarmsAll() {
  return alarmCatalog.categories.flatMap(c => c.alarms);
}

// ─── Link Failure Modal ───────────────────────────────────────────────────────
function injectLinkFailure(src, tgt) {
  if (!sessionId) { showToast('Start simulation first', 'error'); return; }
  if (src && tgt) {
    // Called directly (from edge context menu)
    confirmLinkFailureFor(src, tgt);
    return;
  }
  populateNodeSelects();
  document.getElementById('link-overlay').classList.remove('hidden');
  document.getElementById('link-modal').classList.remove('hidden');
}

function closeLinkModal() {
  document.getElementById('link-overlay').classList.add('hidden');
  document.getElementById('link-modal').classList.add('hidden');
}

async function confirmLinkFailure() {
  const src = document.getElementById('lf-source').value;
  const tgt = document.getElementById('lf-target').value;
  if (!src || !tgt) { showToast('Select source and target', 'error'); return; }
  closeLinkModal();
  await confirmLinkFailureFor(src, tgt);
}

async function confirmLinkFailureFor(src, tgt) {
  try {
    await fetch(`${API}/simulate/${sessionId}/link-failure`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ source: src, target: tgt }),
    });
    // Mark edge as failed on canvas
    const edge = cy.edges(`[source="${src}"][target="${tgt}"], [source="${tgt}"][target="${src}"]`);
    edge.addClass('link-failed');
    showToast(`Link failure injected: ${src} ↔ ${tgt}`, 'success');
    // Auto-recover edge colour after 15s
    setTimeout(() => edge.removeClass('link-failed').addClass('link-active'), 15000);
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

// ─── Console controls ─────────────────────────────────────────────────────────
function setSevFilter(sev) {
  sevFilter = sev;
  document.querySelectorAll('.filter-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.sev === sev);
  });
  // Re-apply the filter to cards already in the feed
  document.querySelectorAll('#alarm-feed .alarm-card').forEach(card => {
    const show = sev === 'all' || card.dataset.sev === sev;
    card.style.display = show ? '' : 'none';
  });
}

function togglePause() {
  isPaused = !isPaused;
  const btn = document.getElementById('btn-pause-console');
  btn.textContent = isPaused ? '▶ Resume' : '⏸';
  showToast(isPaused ? 'Alarm feed paused' : 'Alarm feed resumed', 'info');
}

function clearConsole() {
  document.getElementById('alarm-feed').innerHTML = '';
  counters = { total: 0, Critical: 0, Major: 0, Minor: 0, Warning: 0 };
  alarmTypeCounts = {};
  updateStatBadges();
  if (alarmChart) { alarmChart.data.labels=[]; alarmChart.data.datasets[0].data=[]; alarmChart.update(); }
}

// ─── CSV export (RAN_data schema) ──────────────────────────────────────────────
// Accumulate each alarm with the same fields/semantics as RAN_data.csv.
function recordAlarmForExport(event, nid, sev) {
  if (alarmLog.length >= MAX_EXPORT_ROWS) { exportTruncated = true; return; }
  const st = (typeof event.sim_time === 'number') ? event.sim_time : simTimeHours;
  const name = event.alarm_name;
  const sameKey = nid + '||' + name;

  // Inter-arrival times in sim-hours (blank for the first sighting)
  const hsp = (nid in lastSimBySource)       ? +(st - lastSimBySource[nid]).toFixed(2)       : '';
  const hss = (sameKey in lastSimBySourceName) ? +(st - lastSimBySourceName[sameKey]).toFixed(2) : '';
  lastSimBySource[nid] = st;
  lastSimBySourceName[sameKey] = st;

  // Synthetic occurrence time = session start + sim_time hours
  const occ = new Date((simStartTime ? simStartTime.getTime() : Date.now()) + st * 3600 * 1000);
  const p2 = n => String(n).padStart(2, '0');
  const occurred = `${occ.getFullYear()}/${p2(occ.getMonth() + 1)}/${p2(occ.getDate())} ${occ.getHours()}:${p2(occ.getMinutes())}`;

  alarmLog.push([
    name, nid, occurred, sev, event.location || '', event.ne_type || '',
    hsp, hss, (occ.getDay() + 6) % 7 /* Mon=0 */, occ.getHours(),
    event.next_alarm || 'Noalarm',
  ]);
}

function csvCell(v) {
  const s = String(v ?? '');
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function downloadAlarmCsv() {
  if (alarmLog.length === 0) {
    showToast('No alarms to export yet — start the simulation first', 'error');
    return;
  }
  const header = ['Unnamed: 0', 'Alarm Source', 'Name', 'Occurred On (NT)', 'Severity',
    'Location Information', 'NE Type', 'Hours_since_prior', 'Hours_since_samealarm',
    'Alarm_dow', 'Alarm_hour', 'Next_Alarm'];
  const lines = [header.join(',')];
  alarmLog.forEach((r, i) => {
    // r = [name, src, occurred, sev, loc, ne, hsp, hss, dow, hour, next]
    const row = [i, r[1], r[0], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10]];
    lines.push(row.map(csvCell).join(','));
  });
  const csv = '﻿' + lines.join('\n');   // BOM → utf-8-sig, matches RAN_data
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const a = document.createElement('a');
  a.href = url;
  a.download = `ran_simulation_${ts}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  showToast(`Exported ${alarmLog.length.toLocaleString()} alarms` +
            (exportTruncated ? ` (capped at ${MAX_EXPORT_ROWS.toLocaleString()})` : ''), 'success');
}

// ─── Node select population ───────────────────────────────────────────────────
function populateNodeSelects() {
  const nodes = topology.nodes;
  ['inj-node', 'lf-source', 'lf-target'].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const current = sel.value;
    sel.innerHTML = '<option value="">— select node —</option>' +
      nodes.map(n => `<option value="${escHtml(n.id)}">${escHtml(n.label || n.id)}</option>`).join('');
    if (current) sel.value = current;
  });
}

// ─── Keyboard shortcuts ───────────────────────────────────────────────────────
function onKeyDown(e) {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.key === 'Escape')      { setMode('select'); deselectAll(); }
  if (e.key === 'n' || e.key === 'N') setMode('addNode');
  if (e.key === 'l' || e.key === 'L') setMode('addEdge');
  if (e.key === 'Delete' || e.key === 'Backspace') deleteSelected();
}

// ─── Toast ────────────────────────────────────────────────────────────────────
let toastTimer;
function showToast(msg, type = 'info') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast ${type}`;
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 3500);
}

// ─── Network Analytics ──────────────────────────────────────────────────────
let analyticsData = null;

async function openAnalytics() {
  document.getElementById('analytics-overlay').classList.remove('hidden');
  document.getElementById('analytics-modal').classList.remove('hidden');
  showAnalyticsTab('pathways');
  if (analyticsData) { renderAnalytics(analyticsData); return; }
  document.getElementById('analytics-loading').classList.remove('hidden');
  try {
    const res = await fetch(`${API}/analytics`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    analyticsData = await res.json();
    document.getElementById('analytics-loading').classList.add('hidden');
    renderAnalytics(analyticsData);
  } catch (e) {
    document.getElementById('analytics-loading').textContent =
      'Could not load analytics: ' + e.message;
  }
}

function closeAnalytics() {
  document.getElementById('analytics-overlay').classList.add('hidden');
  document.getElementById('analytics-modal').classList.add('hidden');
}

function showAnalyticsTab(tab) {
  document.querySelectorAll('.atab').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === tab));
  ['pathways', 'resilience', 'intra', 'inter'].forEach(t =>
    document.getElementById(`apane-${t}`).classList.toggle('hidden', t !== tab));
}

function bar(pct, cls) {
  return `<span class="abar"><span class="abar-fill ${cls || ''}" style="width:${Math.max(2, pct)}%"></span></span>`;
}

function renderAnalytics(d) {
  const m = d.meta || {};
  document.getElementById('analytics-meta').textContent =
    `${(m.rows_analysed || 0).toLocaleString()} alarms · ${m.alarm_types} types · ` +
    `${(m.sources || 0).toLocaleString()} sites · holdout excluded`;

  // 1 ── Propagation pathways ────────────────────────────────────────────────
  const maxC = Math.max(...d.pathways.map(p => p.count), 1);
  document.getElementById('apane-pathways').innerHTML = `
    <p class="apane-intro">Most frequent alarm-to-alarm transitions (what an alarm
    tends to lead to next on the same element). Probability = share of that source
    alarm's follow-ons.</p>
    <table class="atable">
      <thead><tr><th>From</th><th>→ Leads to</th><th class="num">Count</th><th class="num">P(next)</th><th class="barcol">Frequency</th></tr></thead>
      <tbody>${d.pathways.map(p => `
        <tr><td>${escHtml(p.from)}</td><td class="lead">${escHtml(p.to)}</td>
        <td class="num">${p.count.toLocaleString()}</td>
        <td class="num">${(p.prob * 100).toFixed(1)}%</td>
        <td>${bar(100 * p.count / maxC, 'b-cyan')}</td></tr>`).join('')}
      </tbody></table>`;

  // 2 ── Node resilience ───────────────────────────────────────────────────────
  const r = d.resilience;
  const resRow = x => `<tr><td>${escHtml(x.site || x.ne_type)}</td>
    <td class="num">${x.alarms.toLocaleString()}</td>
    <td class="num">${x.recovery_pct}%</td>
    <td class="num">${(x.median_gap_h ?? x.mean_gap_h)}h</td>
    <td class="num">${x.critical_pct}%</td>
    <td class="num"><strong>${x.resilience}</strong></td>
    <td>${bar(x.resilience, x.resilience >= 45 ? 'b-green' : x.resilience >= 30 ? 'b-amber' : 'b-red')}</td></tr>`;
  document.getElementById('apane-resilience').innerHTML = `
    <p class="apane-intro">${escHtml(r.method)}</p>
    <div class="asubtitle">By equipment type (NE Type)</div>
    <table class="atable"><thead><tr><th>NE Type</th><th class="num">Alarms</th><th class="num">Self-clear</th><th class="num">Med. gap</th><th class="num">Critical</th><th class="num">Index</th><th class="barcol"></th></tr></thead>
      <tbody>${r.by_ne_type.map(resRow).join('')}</tbody></table>
    <div class="acols">
      <div><div class="asubtitle b-green-t">Most resilient sites</div>
        <table class="atable"><thead><tr><th>Site</th><th class="num">Alarms</th><th class="num">Clear</th><th class="num">Gap</th><th class="num">Crit</th><th class="num">Idx</th><th class="barcol"></th></tr></thead>
        <tbody>${r.most_resilient.map(resRow).join('')}</tbody></table></div>
      <div><div class="asubtitle b-red-t">Least resilient sites</div>
        <table class="atable"><thead><tr><th>Site</th><th class="num">Alarms</th><th class="num">Clear</th><th class="num">Gap</th><th class="num">Crit</th><th class="num">Idx</th><th class="barcol"></th></tr></thead>
        <tbody>${r.least_resilient.map(resRow).join('')}</tbody></table></div>
    </div>`;

  // 3 ── Intra-node inter-alarm time ───────────────────────────────────────────
  const it = d.intra_node_timing, s = it.stats;
  const maxH = Math.max(...it.histogram.map(h => h.pct), 1);
  document.getElementById('apane-intra').innerHTML = `
    <p class="apane-intro">${escHtml(it.note)}</p>
    <div class="astats">
      <div class="astat"><span>${s.median_h}h</span><label>Median</label></div>
      <div class="astat"><span>${s.mean_h}h</span><label>Mean</label></div>
      <div class="astat"><span>${s.p25_h}h</span><label>P25</label></div>
      <div class="astat"><span>${s.p75_h}h</span><label>P75</label></div>
      <div class="astat"><span>${s.p90_h}h</span><label>P90</label></div>
    </div>
    <div class="asubtitle">Distribution of gap between consecutive alarms on a node</div>
    ${it.histogram.map(h => `<div class="ahrow"><span class="ahlabel">${escHtml(h.bucket)}</span>
      ${bar(100 * h.pct / maxH, 'b-cyan')}<span class="ahval">${h.pct}%</span></div>`).join('')}
    <div class="asubtitle">Median gap by equipment type</div>
    ${it.by_ne_type.map(x => `<div class="ahrow"><span class="ahlabel">${escHtml(x.ne_type)}</span>
      ${bar(100 * x.median_h / Math.max(...it.by_ne_type.map(y=>y.median_h),1), 'b-amber')}
      <span class="ahval">${x.median_h}h</span></div>`).join('')}`;

  // 4 ── Cross-node propagation time ───────────────────────────────────────────
  const nt = d.inter_node_timing, ns = nt.stats;
  const maxN = Math.max(...nt.histogram.map(h => h.pct), 1);
  document.getElementById('apane-inter').innerHTML = `
    <p class="apane-intro">${escHtml(nt.method)}</p>
    <div class="astats">
      <div class="astat"><span>${ns.median_min}m</span><label>Median</label></div>
      <div class="astat"><span>${ns.mean_min}m</span><label>Mean</label></div>
      <div class="astat"><span>${ns.p90_min}m</span><label>P90</label></div>
      <div class="astat"><span>${ns.pct_inter_node_within_60min}%</span><label>Within 60m</label></div>
      <div class="astat"><span>${(ns.pairs_within_60min||0).toLocaleString()}</span><label>Pairs</label></div>
    </div>
    <div class="asubtitle">Time gap between alarms propagating across different nodes</div>
    ${nt.histogram.map(h => `<div class="ahrow"><span class="ahlabel">${escHtml(h.bucket)}</span>
      ${bar(100 * h.pct / maxN, 'b-red')}<span class="ahval">${h.pct}%</span></div>`).join('')}`;
}

// ─── Utils ────────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
