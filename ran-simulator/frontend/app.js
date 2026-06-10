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

// ─── Node type catalogue (shape + colour by RAN role) ───────────────────────────
// Each topology node carries an ne_type; we render its category by shape/colour so
// the network role is visible at a glance (severity is shown by the border ring).
const NODE_TYPES = [
  { id: 'NE40E (Core)',          cat: 'Core router',     color: '#7c4dff', shape: 'diamond',         size: 58 },
  { id: 'CX600 (Core)',          cat: 'Core router',     color: '#7c4dff', shape: 'diamond',         size: 58 },
  { id: 'ATN 910 (Agg)',         cat: 'Aggregation',     color: '#1f8fb0', shape: 'hexagon',         size: 50 },
  { id: 'RTN 950 (MW)',          cat: 'Microwave relay', color: '#13a07a', shape: 'rectangle',       size: 42 },
  { id: 'BTS3900 LTE',           cat: 'eNodeB (4G/LTE)', color: '#1a6fd8', shape: 'ellipse',         size: 46 },
  { id: 'BTS5900 5G',            cat: 'gNodeB (5G)',     color: '#c026d3', shape: 'round-triangle',  size: 48 },
  { id: 'BTS3900 GSM',           cat: 'GSM (2G)',        color: '#b08400', shape: 'round-rectangle', size: 44 },
  { id: 'GBTS',                  cat: 'GSM (2G)',        color: '#b08400', shape: 'round-rectangle', size: 44 },
  { id: 'RRU3953',               cat: 'Small cell',      color: '#2f7fc7', shape: 'ellipse',         size: 34 },
  { id: 'Lampsite (Small Cell)', cat: 'Small cell',      color: '#2f7fc7', shape: 'ellipse',         size: 34 },
  { id: '9549',                  cat: 'Controller',      color: '#5b6b8c', shape: 'octagon',         size: 48 },
];
const DEFAULT_TYPE = { id: 'BTS3900 LTE', cat: 'eNodeB (4G/LTE)', color: '#1a6fd8', shape: 'ellipse', size: 46 };

function typeInfo(neType) {
  return NODE_TYPES.find(t => t.id === neType) || { ...DEFAULT_TYPE, id: neType || DEFAULT_TYPE.id };
}
// Build the display data (shape/colour/size) for a node from its ne_type.
function decorateNode(n) {
  const ti = typeInfo(n.ne_type);
  return {
    id:        n.id,
    label:     n.label || n.id,
    site_id:   n.site_id || n.id,
    ne_type:   n.ne_type || DEFAULT_TYPE.id,
    typeColor: ti.color,
    typeShape: ti.shape,
    w:         ti.size,
    h:         ti.size,
  };
}
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
  populateTopologyDropdown();
  renderTypeLegend();
  loadAlarmTypes();
  checkNs3Status();
  document.addEventListener('keydown', onKeyDown);
});

// Build the topology dropdown from the registry
function populateTopologyDropdown() {
  const sel = document.getElementById('topo-select');
  if (!sel) return;
  sel.innerHTML = '<option value="">Load topology…</option>' +
    Object.entries(TOPOLOGIES).map(([k, v]) => `<option value="${k}">${escHtml(v.label)}</option>`).join('');
}

// Render the node-type legend (shape + colour per RAN role)
function renderTypeLegend() {
  const el = document.getElementById('type-legend');
  if (!el) return;
  const seen = new Set();
  const items = [];
  NODE_TYPES.forEach(t => { if (!seen.has(t.cat)) { seen.add(t.cat); items.push(t); } });
  el.innerHTML = items.map(t =>
    `<span class="tl-item"><span class="tl-dot tl-${t.shape}" style="background:${t.color}"></span>${escHtml(t.cat)}</span>`
  ).join('');
}

// ─── Cytoscape setup ──────────────────────────────────────────────────────────
function initCytoscape() {
  cy = cytoscape({
    container: document.getElementById('cy'),
    elements:  [],
    style: [
      {
        selector: 'node',
        style: {
          'background-color':    'data(typeColor)',
          'background-opacity':  0.55,
          'border-color':        '#2a4a7a',
          'border-width':        2,
          'width':               'data(w)', 'height': 'data(h)',
          'label':               'data(label)',
          'color':               '#9fb2d6',
          'font-size':           '10px',
          'text-valign':         'bottom',
          'text-margin-y':       5,
          'font-family':         'JetBrains Mono, monospace',
          'text-outline-width':  2,
          'text-outline-color':  '#030712',
          'shape':               'data(typeShape)',
          'transition-property': 'border-color, border-width',
          'transition-duration': '0.3s',
        }
      },
      // Severity is shown ONLY by the border ring, so node fill keeps showing type.
      {
        selector: 'node.healthy',
        style: { 'border-color': '#1f9e57', 'border-width': 2 }
      },
      {
        selector: 'node.h-warning',
        style: { 'border-color': '#4d9fff', 'border-width': 3 }
      },
      {
        selector: 'node.h-minor',
        style: { 'border-color': '#ffd60a', 'border-width': 3 }
      },
      {
        selector: 'node.h-major',
        style: { 'border-color': '#ff9500', 'border-width': 4 }
      },
      {
        selector: 'node.h-critical',
        style: { 'border-color': '#ff3b30', 'border-width': 5, 'color': '#ff8580' }
      },
      {
        selector: 'node:selected',
        style: { 'border-color': '#00d4ff', 'border-width': 4 }
      },
      {
        selector: 'node.adding-edge',
        style: { 'border-color': '#b388ff', 'border-width': 3 }
      },
      // Watcher highlighting
      {
        selector: 'node.watched',
        style: { 'border-color': '#00e5ff', 'border-width': 6,
                 'background-opacity': 0.85, 'color': '#7af7ff', 'font-size': '13px',
                 'overlay-color': '#00e5ff', 'overlay-opacity': 0.18, 'overlay-padding': 10 }
      },
      {
        selector: 'node.watch-neighbour',
        style: { 'border-color': '#00b8d4', 'border-width': 3, 'background-opacity': 0.75 }
      },
      {
        selector: 'node.dimmed',
        style: { 'opacity': 0.32 }
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
      // Watcher edge highlighting + propagation flash
      {
        selector: 'edge.watch-edge',
        style: { 'line-color': '#00b8d4', 'width': 3 }
      },
      {
        selector: 'edge.cascade-flash',
        style: { 'line-color': '#ff3b30', 'width': 5, 'target-arrow-shape': 'triangle',
                 'target-arrow-color': '#ff3b30', 'mid-target-arrow-color': '#ff3b30' }
      },
      {
        selector: 'edge.dimmed',
        style: { 'opacity': 0.18 }
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
let pendingNodePos = null;     // canvas position awaiting the Add-Node dialog

// Clicking empty canvas in addNode mode opens a dialog to name + type the node.
function addNodeAt(position) {
  pendingNodePos = position;
  openAddNodeModal();
}

function createNode(name, neType, position) {
  const id = name && name.trim() ? name.trim() : `NODE-${nodeCounter++}`;
  if (cy.getElementById(id).length) { showToast(`Node "${id}" already exists`, 'error'); return null; }
  const node = cy.add({
    group: 'nodes',
    data:  decorateNode({ id, label: id, site_id: id, ne_type: neType }),
    position,
  });
  node.addClass('healthy');
  rebuildTopology();
  selectNode(node);
  populateNodeSelects();
  return node;
}

// ─── Add-Node dialog (name + type) ─────────────────────────────────────────────
function ensureTypeSelect(selId, selected) {
  const sel = document.getElementById(selId);
  if (!sel) return;
  // Group options by category
  const byCat = {};
  NODE_TYPES.forEach(t => { (byCat[t.cat] = byCat[t.cat] || []).push(t); });
  sel.innerHTML = Object.entries(byCat).map(([cat, types]) =>
    `<optgroup label="${cat}">` +
    types.map(t => `<option value="${escHtml(t.id)}"${t.id === selected ? ' selected' : ''}>${escHtml(t.id)}</option>`).join('') +
    `</optgroup>`).join('');
}

function openAddNodeModal() {
  ensureTypeSelect('an-type', 'BTS3900 LTE');
  const nameInput = document.getElementById('an-name');
  nameInput.value = `NODE-${nodeCounter}`;
  document.getElementById('addnode-overlay').classList.remove('hidden');
  document.getElementById('addnode-modal').classList.remove('hidden');
  setTimeout(() => { nameInput.focus(); nameInput.select(); }, 30);
}

function closeAddNodeModal() {
  document.getElementById('addnode-overlay').classList.add('hidden');
  document.getElementById('addnode-modal').classList.add('hidden');
  pendingNodePos = null;
}

function confirmAddNode() {
  const name   = document.getElementById('an-name').value;
  const neType = document.getElementById('an-type').value || 'BTS3900 LTE';
  const pos    = pendingNodePos || { x: 400, y: 400 };
  closeAddNodeModal();
  const node = createNode(name, neType, pos);
  if (node) showToast(`Added ${neType} "${node.id()}"`, 'success');
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
  stopWatch();
  cy.elements().remove();
  data.nodes.forEach(n => {
    cy.add({ group: 'nodes', data: decorateNode(n), position: { x: n.x, y: n.y } });
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

// ─── Topology generator helpers ────────────────────────────────────────────────
const T = {
  mk: (nodes, id, label, ne, x, y) => { nodes.push({ id, label, ne_type: ne, x, y }); return id; },
  link: (edges, a, b) => edges.push({ source: a, target: b }),
  ring: (edges, ids) => { for (let i = 0; i < ids.length; i++) edges.push({ source: ids[i], target: ids[(i + 1) % ids.length] }); },
};
const ACCESS_MIX  = ['BTS3900 LTE', 'BTS3900 LTE', 'BTS5900 5G', 'BTS3900 GSM', 'RRU3953'];
const FIVEG_MIX   = ['BTS5900 5G', 'BTS5900 5G', 'BTS3900 LTE', 'RRU3953', 'Lampsite (Small Cell)'];
const RURAL_MIX   = ['GBTS', 'BTS3900 LTE', 'GBTS', 'RTN 950 (MW)'];
const pickMix = (mix, i) => mix[i % mix.length];

// Generic radial hierarchy: cores (ring) → hubs (per core) → access star (per hub).
function buildRadial({ cx = 1400, cy = 1400, cores, coreType, coreLabel, coreR,
                       hubsPerCore, hubType, hubR, accessPerHub, accessR, accessMix,
                       hubResilienceRing = false }) {
  const nodes = [], edges = [];
  const coreIds = [];
  for (let c = 0; c < cores; c++) {
    const a = cores === 1 ? 0 : (2 * Math.PI * c) / cores - Math.PI / 2;
    const x = cores === 1 ? cx : cx + coreR * Math.cos(a);
    const y = cores === 1 ? cy : cy + coreR * Math.sin(a);
    coreIds.push(T.mk(nodes, `CORE-${c + 1}`, `${coreLabel} ${c + 1}`, coreType, x, y));
  }
  if (cores > 1) T.ring(edges, coreIds);            // core resilience ring
  let hubN = 0;
  for (let c = 0; c < cores; c++) {
    const ca = cores === 1 ? 0 : (2 * Math.PI * c) / cores - Math.PI / 2;
    const cxp = cores === 1 ? cx : cx + coreR * Math.cos(ca);
    const cyp = cores === 1 ? cy : cy + coreR * Math.sin(ca);
    const hubIds = [];
    for (let h = 0; h < hubsPerCore; h++) {
      const ha = (2 * Math.PI * h) / hubsPerCore + ca;
      const hx = cxp + hubR * Math.cos(ha);
      const hy = cyp + hubR * Math.sin(ha);
      const hubId = T.mk(nodes, `AGG-${c + 1}-${h + 1}`, `AGG-${c + 1}-${h + 1}`, hubType, hx, hy);
      hubIds.push(hubId);
      T.link(edges, coreIds[c], hubId);
      for (let s = 0; s < accessPerHub; s++) {
        const sa = (2 * Math.PI * s) / accessPerHub;
        const enbId = `ENB-${c + 1}${h + 1}${String(s + 1).padStart(2, '0')}`;
        T.mk(nodes, enbId, enbId, pickMix(accessMix, s), hx + accessR * Math.cos(sa), hy + accessR * Math.sin(sa));
        T.link(edges, hubId, enbId);
      }
      hubN++;
    }
    if (hubResilienceRing && hubIds.length > 2) T.ring(edges, hubIds);
  }
  return { nodes, edges };
}

// 1) Small-cell cluster — dense urban, one hub, macros + many small cells.
function buildSmallCellCluster() {
  const nodes = [], edges = [];
  const cx = 700, cy = 600;
  const hub = T.mk(nodes, 'AGG-1', 'Metro Hub', 'ATN 910 (Agg)', cx, cy);
  for (let i = 0; i < 2; i++) {
    const a = Math.PI * i;
    T.mk(nodes, `MACRO-${i + 1}`, `MACRO-${i + 1}`, 'BTS3900 LTE', cx + 240 * Math.cos(a), cy + 240 * Math.sin(a));
    T.link(edges, hub, `MACRO-${i + 1}`);
  }
  for (let s = 0; s < 12; s++) {
    const a = (2 * Math.PI * s) / 12;
    const r = 150 + (s % 3) * 90;
    const id = `SC-${String(s + 1).padStart(2, '0')}`;
    T.mk(nodes, id, id, s % 4 === 0 ? 'RRU3953' : 'Lampsite (Small Cell)', cx + r * Math.cos(a), cy + r * Math.sin(a));
    // small cells home onto the nearest macro for backhaul
    T.link(edges, s < 6 ? 'MACRO-1' : 'MACRO-2', id);
  }
  return { nodes, edges };
}

// 3) Rural microwave backhaul — chains of microwave relays feeding remote sites.
function buildRuralMicrowave() {
  const nodes = [], edges = [];
  const core = T.mk(nodes, 'CORE-1', 'County Core', 'NE40E (Core)', 250, 700);
  const branches = 3, hops = 4;
  for (let b = 0; b < branches; b++) {
    let prev = core;
    const dirY = 350 + b * 350;
    for (let h = 0; h < hops; h++) {
      const mwId = T.mk(nodes, `MW-${b + 1}-${h + 1}`, `MW-${b + 1}-${h + 1}`, 'RTN 950 (MW)', 520 + h * 320, dirY + (h % 2 ? -80 : 80));
      T.link(edges, prev, mwId);
      // each relay drops one or two rural base stations
      const leafN = (h % 2) + 1;
      for (let l = 0; l < leafN; l++) {
        const leaf = `RBS-${b + 1}-${h + 1}-${l + 1}`;
        T.mk(nodes, leaf, leaf, pickMix(RURAL_MIX, h + l), 520 + h * 320 + 60, dirY + (h % 2 ? -80 : 80) + (l ? 130 : -130));
        T.link(edges, mwId, leaf);
      }
      prev = mwId;
    }
  }
  return { nodes, edges };
}

// 6) 5G dense urban — 5G-heavy access with LTE anchors and small cells.
function build5GDense() {
  return buildRadial({
    cx: 1100, cy: 1000, cores: 1, coreType: 'CX600 (Core)', coreLabel: 'Metro Core', coreR: 0,
    hubsPerCore: 4, hubType: 'ATN 910 (Agg)', hubR: 560, accessPerHub: 14,
    accessR: 270, accessMix: FIVEG_MIX, hubResilienceRing: true,
  });
}

// ─── Topology registry (dropdown order) ─────────────────────────────────────────
const TOPOLOGIES = {
  demo:      { label: 'BT demo — 10 real sites', build: null },   // fetched from backend
  smallcell: { label: 'Small-cell cluster — urban (~15)', build: buildSmallCellCluster },
  town:      { label: 'Town / district (~22)', build: () => buildRadial({
                 cx: 800, cy: 700, cores: 1, coreType: 'NE40E (Core)', coreLabel: 'District Core', coreR: 0,
                 hubsPerCore: 2, hubType: 'ATN 910 (Agg)', hubR: 380, accessPerHub: 9,
                 accessR: 230, accessMix: ACCESS_MIX, hubResilienceRing: true }) },
  rural:     { label: 'Rural microwave backhaul (~24)', build: buildRuralMicrowave },
  city:      { label: 'City metro (~48)', build: () => buildRadial({
                 cx: 1200, cy: 1100, cores: 1, coreType: 'NE40E (Core)', coreLabel: 'City Core', coreR: 0,
                 hubsPerCore: 4, hubType: 'ATN 910 (Agg)', hubR: 620, accessPerHub: 11,
                 accessR: 300, accessMix: ACCESS_MIX, hubResilienceRing: true }) },
  fiveg:     { label: '5G dense urban (~60)', build: build5GDense },
  zone:      { label: 'Zone / county (~80)', build: () => buildRadial({
                 cx: 1400, cy: 1300, cores: 2, coreType: 'NE40E (Core)', coreLabel: 'Zone Core', coreR: 520,
                 hubsPerCore: 4, hubType: 'ATN 910 (Agg)', hubR: 560, accessPerHub: 9,
                 accessR: 260, accessMix: ACCESS_MIX, hubResilienceRing: true }) },
  region:    { label: 'Regional RAN (~130)', build: () => buildRadial({
                 cx: 1700, cy: 1600, cores: 3, coreType: 'NE40E (Core)', coreLabel: 'Regional Core', coreR: 780,
                 hubsPerCore: 6, hubType: 'ATN 910 (Agg)', hubR: 640, accessPerHub: 7,
                 accessR: 250, accessMix: ACCESS_MIX, hubResilienceRing: true }) },
  national:  { label: 'National RAN (~210)', build: buildNationalTopology },
};

// Dispatcher for the topology dropdown
async function loadPresetTopology(name) {
  if (!name) return;
  try {
    if (name === 'demo') {
      await loadDemoTopology();
    } else {
      const spec = TOPOLOGIES[name];
      if (spec && spec.build) {
        const t = spec.build();
        applyTopology(t, `${spec.label} loaded — ${t.nodes.length} nodes, ${t.edges.length} links`);
      }
    }
  } catch (e) {
    showToast('Could not load topology: ' + e.message, 'error');
  }
  const sel = document.getElementById('topo-select');
  if (sel) sel.value = '';
}

async function loadDemoTopology() {
  const res  = await fetch(`${API}/demo-topology`);
  const data = await res.json();
  applyTopology(data, 'BT demo topology loaded — 10 real sites');
}

// National RAN: 2 national cores → 6 regional cores (resilience ring) →
// 3 metro aggregation hubs per region → 6 access eNodeBs per metro hub.
function buildNationalTopology() {
  const nodes = [], edges = [];
  const cx = 1400, cy0 = 1400;
  const REGIONS = 6, METROS = 3, ACCESS = 6;
  const REGION_NAMES = ['London', 'South West', 'Midlands', 'North West', 'North East', 'Scotland'];

  T.mk(nodes, 'NCORE-1', 'National Core 1', 'NE40E (Core)', cx - 220, cy0);
  T.mk(nodes, 'NCORE-2', 'National Core 2', 'NE40E (Core)', cx + 220, cy0);
  edges.push({ source: 'NCORE-1', target: 'NCORE-2' });

  const regionR = 1050, metroR = 360, accessR = 150;
  for (let r = 0; r < REGIONS; r++) {
    const ra = (2 * Math.PI * r) / REGIONS - Math.PI / 2;
    const rx = cx + regionR * Math.cos(ra);
    const ry = cy0 + regionR * Math.sin(ra);
    const rcId = `RCORE-${r + 1}`;
    T.mk(nodes, rcId, `${REGION_NAMES[r]} RCore`, 'NE40E (Core)', rx, ry);
    edges.push({ source: rcId, target: 'NCORE-1' });
    edges.push({ source: rcId, target: 'NCORE-2' });
    edges.push({ source: rcId, target: `RCORE-${((r + 1) % REGIONS) + 1}` });

    for (let m = 0; m < METROS; m++) {
      const ma = ra + (m - (METROS - 1) / 2) * 0.42;
      const mx = rx + metroR * Math.cos(ma);
      const my = ry + metroR * Math.sin(ma);
      const metroId = `METRO-${r + 1}-${m + 1}`;
      T.mk(nodes, metroId, metroId, 'ATN 910 (Agg)', mx, my);
      edges.push({ source: rcId, target: metroId });
      for (let a = 0; a < ACCESS; a++) {
        const aa = (2 * Math.PI * a) / ACCESS;
        const enbId = `ENB-${r + 1}${m + 1}${String(a + 1).padStart(2, '0')}`;
        T.mk(nodes, enbId, enbId, pickMix(ACCESS_MIX, a), mx + accessR * Math.cos(aa), my + accessR * Math.sin(aa));
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
  const wb = document.getElementById('ni-watch-btn');
  if (wb) {
    const watching = watchedNode === id;
    wb.textContent = watching ? '👁 Stop Watching' : '👁 Watch';
    wb.classList.toggle('btn-watching', watching);
  }
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
  // ns-3 LTE is CPU-bound; live simulation is practical to ~30 base stations.
  // Larger topologies still load for design/visualisation/watch, but the live
  // alarm stream is sparse — inject faults to drive propagation analysis.
  if (topology.nodes.length > 35) {
    showToast(`Large topology (${topology.nodes.length} nodes) — ns-3 live alarms will be sparse; inject faults to drive analysis`, 'info');
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

  // Watcher: propagation tracking for the watched node
  trackWatch(event, nid, sev);
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

  document.getElementById('sim-time').textContent   = fmtSimTime(simTimeHours);
  document.getElementById('eng-simtime').textContent = fmtSimTime(simTimeHours);
}

// Sim time is genuine ns-3 simulated network time (seconds-scale), so format
// with seconds/minutes granularity rather than whole hours (which round to 0).
function fmtSimTime(hours) {
  const s = (hours || 0) * 3600;
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  if (s >= 60)   return `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
  return `${s.toFixed(1)}s`;
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

    // Drive sim time from the ns-3 clock heartbeat so it advances even when no
    // alarms are firing (sparse on large/well-covered topologies).
    if (typeof ns3.sim_time_s === 'number') {
      simTimeHours = ns3.sim_time_s / 3600;
      document.getElementById('sim-time').textContent    = fmtSimTime(simTimeHours);
      document.getElementById('eng-simtime').textContent = fmtSimTime(simTimeHours);
    }

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

// ─── Node Watcher (live alarm-propagation analysis) ─────────────────────────
let watchedNode      = null;
let watchNeighbours  = new Set();
let watchData        = null;
let watchLastSelfT   = 0;
let neighbourLastT   = {};       // neighbourId → last alarm wall-time (s)
let watchRenderAt    = 0;
const WATCH_WINDOW   = 6.0;      // s — cascade correlation window

function nowS() { return Date.now() / 1000; }

function neighboursOf(id) {
  return cy.edges(`[source="${id}"], [target="${id}"]`).map(e =>
    e.data('source') === id ? e.data('target') : e.data('source'));
}

function toggleWatchSelected() {
  if (!selectedElement || !selectedElement.isNode()) return;
  const id = selectedElement.id();
  if (watchedNode === id) { stopWatch(); return; }
  watchNode(id);
}

// Top-toolbar entry point: watch the selected node, or guide the user to pick one.
function watchFromToolbar() {
  if (watchedNode) { stopWatch(); return; }
  if (selectedElement && selectedElement.isNode()) { watchNode(selectedElement.id()); return; }
  if (cy.nodes().length === 0) { showToast('Load a topology first', 'error'); return; }
  showToast('Click a node on the map, then press Watch (or use 👁 in its info panel)', 'info');
  setMode('select');
}

function watchNode(id) {
  stopWatch();
  watchedNode = id;
  const nbrs = neighboursOf(id);
  watchNeighbours = new Set(nbrs);
  watchData = { onNode: 0, sev: {}, last: [], out: {}, down: {}, up: {} };
  watchLastSelfT = 0;
  neighbourLastT = {};

  // Canvas highlight: focus the watched node + its direct connections
  cy.batch(() => {
    cy.elements().addClass('dimmed');
    const node = cy.getElementById(id);
    node.removeClass('dimmed').addClass('watched');
    nbrs.forEach(n => cy.getElementById(n).removeClass('dimmed').addClass('watch-neighbour'));
    cy.edges(`[source="${id}"], [target="${id}"]`).removeClass('dimmed').addClass('watch-edge');
  });
  const hood = cy.getElementById(id).closedNeighborhood();
  cy.animate({ fit: { eles: hood, padding: 80 } }, { duration: 350 });

  document.getElementById('watcher-panel').classList.remove('hidden');
  renderWatcher(true);
  syncWatchButtons();
  showToast(`Watching ${id} — ${nbrs.length} connections`, 'info');
}

function syncWatchButtons() {
  const tb = document.getElementById('btn-watch');
  if (tb) {
    tb.textContent = watchedNode ? '👁 Stop Watching' : '👁 Watch Node';
    tb.classList.toggle('btn-active', !!watchedNode);
  }
}

function stopWatch() {
  if (watchedNode) {
    cy.elements().removeClass('watched watch-neighbour watch-edge dimmed cascade-flash');
  }
  watchedNode = null;
  watchNeighbours = new Set();
  watchData = null;
  const p = document.getElementById('watcher-panel');
  if (p) p.classList.add('hidden');
  syncWatchButtons();
  if (selectedElement && selectedElement.isNode && selectedElement.isNode()) showNodeInfo(selectedElement);
}

function flashCascadeEdge(a, b) {
  const edge = cy.edges(`[source="${a}"][target="${b}"], [source="${b}"][target="${a}"]`);
  if (!edge.length) return;
  edge.addClass('cascade-flash');
  setTimeout(() => edge.removeClass('cascade-flash').addClass('watch-edge'), 1200);
}

function trackWatch(event, nid, sev) {
  if (!watchedNode || !watchData) return;
  const t = nowS();
  const name = event.alarm_name;
  const next = event.next_alarm || 'Noalarm';

  if (nid === watchedNode) {
    watchData.onNode++;
    watchData.sev[sev] = (watchData.sev[sev] || 0) + 1;
    watchData.last.unshift({ name, sev, next });
    watchData.last = watchData.last.slice(0, 8);
    const key = name + ' ' + next;
    watchData.out[key] = (watchData.out[key] || 0) + 1;
    watchLastSelfT = t;
    // Upstream: a neighbour that alarmed shortly BEFORE this node (cascade in)
    watchNeighbours.forEach(nb => {
      if (neighbourLastT[nb] && t - neighbourLastT[nb] <= WATCH_WINDOW) {
        watchData.up[nb] = (watchData.up[nb] || 0) + 1;
        flashCascadeEdge(nb, watchedNode);
      }
    });
  } else if (watchNeighbours.has(nid)) {
    neighbourLastT[nid] = t;
    // Downstream: neighbour alarmed shortly AFTER the watched node (cascade out)
    if (watchLastSelfT && t - watchLastSelfT <= WATCH_WINDOW) {
      watchData.down[nid] = (watchData.down[nid] || 0) + 1;
      flashCascadeEdge(watchedNode, nid);
    }
  } else {
    return; // unrelated node — no watcher update
  }

  if (t - watchRenderAt > 0.4) { renderWatcher(); watchRenderAt = t; }
}

function sevDot(sev) {
  const c = SEV_COLOR[sev] || '#4a5b82';
  return `<span class="w-dot" style="background:${c}"></span>`;
}

function renderWatcher(full) {
  if (!watchedNode) return;
  const node = cy.getElementById(watchedNode);
  const neType = node.length ? (node.data('ne_type') || '—') : '—';

  if (full) {
    document.getElementById('w-title').textContent = watchedNode;
    document.getElementById('w-type').textContent = neType;
  }

  // Connections (what it is connected to)
  const conn = [...watchNeighbours].map(nb => {
    const nn = cy.getElementById(nb);
    const t  = nn.length ? typeInfo(nn.data('ne_type')) : DEFAULT_TYPE;
    const cnt = nodeAlarmCounts[nb] || 0;
    const sv  = nodeMaxSeverity[nb];
    return `<div class="w-conn" onclick="highlightNode('${nb}')">
      <span class="w-cdot" style="background:${t.color}"></span>
      <span class="w-cid">${escHtml(nb)}</span>
      <span class="w-ctype">${escHtml(t.cat)}</span>
      <span class="w-ccount">${sv ? sevDot(sv) : ''}${cnt}</span></div>`;
  }).join('') || '<div class="w-empty">No connections</div>';
  document.getElementById('w-connections').innerHTML = conn;

  // Alarms on this node
  const d = watchData;
  const sevTxt = ['Critical', 'Major', 'Minor', 'Warning']
    .filter(s => d.sev[s]).map(s => `${sevDot(s)}${d.sev[s]}`).join(' ') || '—';
  document.getElementById('w-onnode').innerHTML =
    `<div class="w-stat"><span class="w-big">${d.onNode}</span> alarms on node</div>
     <div class="w-sevline">${sevTxt}</div>` +
    (d.last.length ? '<div class="w-lastlist">' + d.last.map(a =>
      `<div class="w-lastrow">${sevDot(a.sev)}<span>${escHtml(a.name)}</span></div>`).join('') + '</div>' : '');

  // Propagation FROM this node (alarm → next_alarm chains)
  const out = Object.entries(d.out).sort((a, b) => b[1] - a[1]).slice(0, 6);
  document.getElementById('w-propagation').innerHTML = out.length ? out.map(([k, c]) => {
    const [a, b] = k.split(' ');
    const cls = b === 'Noalarm' ? 'w-clear' : '';
    return `<div class="w-flow"><span class="w-fa">${escHtml(a)}</span>
      <span class="w-arrow">──▶</span><span class="w-fb ${cls}">${escHtml(b)}</span>
      <span class="w-fcount">×${c}</span></div>`;
  }).join('') : '<div class="w-empty">No alarms yet on this node</div>';

  // Cascades through the topology (neighbour correlations)
  const down = Object.entries(d.down).sort((a, b) => b[1] - a[1]);
  const up   = Object.entries(d.up).sort((a, b) => b[1] - a[1]);
  const cascHtml =
    `<div class="w-casc-h">↘ Propagates to (downstream)</div>` +
    (down.length ? down.map(([n, c]) => `<div class="w-casc" onclick="highlightNode('${n}')">${escHtml(n)} <span class="w-fcount">×${c}</span></div>`).join('') : '<div class="w-empty">none observed</div>') +
    `<div class="w-casc-h">↖ Triggered by (upstream)</div>` +
    (up.length ? up.map(([n, c]) => `<div class="w-casc" onclick="highlightNode('${n}')">${escHtml(n)} <span class="w-fcount">×${c}</span></div>`).join('') : '<div class="w-empty">none observed</div>');
  document.getElementById('w-cascades').innerHTML = cascHtml;
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
