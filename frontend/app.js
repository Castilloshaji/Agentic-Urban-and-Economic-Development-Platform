const API = location.origin + '/api';
let MAP, LAYER, GEO, UNITS = [], SCENARIOS = [], CHOSEN = [], FOCUS = null,
    CURRENT_FIELD = 'flood_hist_50';
// The ideation run the on-screen proposals came from, so an item logged
// from them records where it was proposed.
let LAST_IDEATION_ID = null, LAST_PROPOSALS = null;

const LAYERS = {
  flood_hist_50:   {label: 'Flood-modelled share', fmt: v => (v * 100).toFixed(1) + '%', invert: false},
  nearest_stop_m:  {label: 'Nearest stop', fmt: v => v >= 1000 ? (v / 1000).toFixed(1) + ' km' : Math.round(v) + ' m', invert: false},
  population:      {label: 'Population', fmt: v => Number(v).toLocaleString(), invert: false},
  stop_count:      {label: 'Transit stops', fmt: v => Math.round(v), invert: true},
};
// A neutral ink ramp. Tying the map to the accent made the whole page one
// colour and flattened the hierarchy: everything shouted, so nothing did. The
// data surface is now tonal, which leaves the accent free to mean "the system
// is asserting this".
const RAMP = ['#eceae5', '#d2cfc8', '#aeada8', '#86878a', '#565b63', '#2b3139'];

const DOMAIN_ORDER = ['economic', 'infrastructure', 'transportation', 'environment',
                      'healthcare', 'education', 'budget'];

const $ = id => document.getElementById(id);
// Read the live token so the map follows the ink/cream switch with the page.
const accent = () => getComputedStyle(document.body).getPropertyValue('--accent').trim() || '#d3341c';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const nameOf = id => (UNITS.find(u => u.admin_id === id) || {}).name || id;

async function get(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error((await r.text()).slice(0, 200));
  return r.json();
}
async function post(path, body) {
  const r = await fetch(API + path, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const payload = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(payload.detail || JSON.stringify(payload).slice(0, 200));
  return payload;
}

async function patch(path, body) {
  const r = await fetch(API + path, {
    method: 'PATCH', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const payload = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(payload.detail || JSON.stringify(payload).slice(0, 200));
  return payload;
}

/* ---------- map ---------- */
function breaks(values) {
  const v = values.filter(x => x !== null && x !== undefined && !Number.isNaN(x)).sort((a, b) => a - b);
  if (!v.length) return [0, 1, 2, 3, 4, 5];
  return [0, .2, .4, .6, .8, 1].map(q => v[Math.min(v.length - 1, Math.floor(q * (v.length - 1)))]);
}
function colour(value, bk, invert) {
  if (value === null || value === undefined || Number.isNaN(value)) return '#e9ecef';
  let i = bk.findIndex((b, k) => k > 0 && value <= b);
  if (i < 0) i = RAMP.length - 1;
  return RAMP[invert ? RAMP.length - 1 - i : i];
}
function paint(field) {
  CURRENT_FIELD = field;
  if (!GEO || !LAYER) return;
  const spec = LAYERS[field];
  const bk = breaks(GEO.features.map(f => f.properties[field]));
  LAYER.setStyle(f => {
    const picked = CHOSEN.includes(f.properties.admin_id);
    return {
      fillColor: colour(f.properties[field], bk, spec.invert),
      // Selection has to survive the choropleth, so it is carried by the
      // outline rather than the fill — the fill still has to mean the data.
      // Selection is a hard pencil outline, not a colour change. The fill still
      // has to mean the data, and the accent hue is already spent on the ramp.
      // Selection is the one thing on the map allowed to use the accent.
      weight: picked ? 2.2 : .55,
      color: picked ? accent() : 'rgba(110,112,116,.45)',
      fillOpacity: picked ? .92 : .8,
    };
  });
  $('legend-scale').innerHTML = RAMP.map(c => `<span style="background:${c}"></span>`).join('');
}
async function initMap() {
  MAP = L.map('map', {scrollWheelZoom: true}).setView([10.0, 76.45], 9);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    {attribution: '&copy; OpenStreetMap', maxZoom: 17}).addTo(MAP);
  GEO = await get('/map/boundaries');
  LAYER = L.geoJSON(GEO, {
    onEachFeature: (f, lyr) => {
      const p = f.properties;
      lyr.bindTooltip(
        `<b>${esc(p.name)}</b><br>${esc(p.local_auth || '')}<br>` +
        `flood ${p.flood_hist_50 == null ? 'n/a' : (p.flood_hist_50 * 100).toFixed(1) + '%'} · ` +
        `stops ${p.stop_count ?? 'n/a'}<br><i>click to add / remove</i>`, {sticky: true});
      lyr.on('click', () => toggle(p.admin_id));
    },
  }).addTo(MAP);
  MAP.fitBounds(LAYER.getBounds(), {padding: [12, 12]});
  paint(CURRENT_FIELD);
}

/* ---------- choosing the affected areas ---------- */
function toggle(id) {
  const at = CHOSEN.indexOf(id);
  if (at >= 0) CHOSEN.splice(at, 1); else CHOSEN.push(id);
  FOCUS = at >= 0 ? null : id;
  renderAdminList($('search').value);
  renderChosen();
  paint(CURRENT_FIELD);
  if (FOCUS) showContext(FOCUS); else $('context-card').hidden = true;
  loadLedger();
}
function renderAdminList(filter = '') {
  const f = filter.toLowerCase();
  const rows = UNITS.filter(u => !f || (u.name || '').toLowerCase().includes(f));
  $('admin-list').innerHTML = rows.map(u => `
    <label class="pick${CHOSEN.includes(u.admin_id) ? ' on' : ''}">
      <input type="checkbox" value="${u.admin_id}"${CHOSEN.includes(u.admin_id) ? ' checked' : ''}>
      <span>${esc(u.name)}</span>
    </label>`).join('') || '<p class="muted">No match.</p>';
}
// Beyond this many, naming every chip is noise rather than information.
const CHIP_LIMIT = 12;

function renderChosen() {
  if (!CHOSEN.length) {
    $('chosen').innerHTML =
      `<p class="muted">Nothing selected. Evaluating now would cover the whole
       district. <button class="tiny" id="select-all">Select all ${UNITS.length}</button></p>`;
    return;
  }
  const all = CHOSEN.length === UNITS.length && UNITS.length > 0;
  const head = all
    ? `<p class="muted">All ${UNITS.length} local bodies
       <button class="tiny" id="clear-all">Clear</button></p>`
    : `<p class="muted">${CHOSEN.length} selected
       <button class="tiny" id="select-all">Select all ${UNITS.length}</button>
       <button class="tiny" id="clear-all">Clear</button></p>`;
  const chips = CHOSEN.length <= CHIP_LIMIT
    ? CHOSEN.map(id => `<span class="chip" data-id="${id}">${esc(nameOf(id))}<b>×</b></span>`).join('')
    : CHOSEN.slice(0, CHIP_LIMIT).map(id =>
        `<span class="chip" data-id="${id}">${esc(nameOf(id))}<b>×</b></span>`).join('')
      + `<span class="chip more">and ${CHOSEN.length - CHIP_LIMIT} more</span>`;
  $('chosen').innerHTML = head + chips;
}

function selectAll() {
  CHOSEN = UNITS.map(u => u.admin_id);
  renderAdminList($('search').value);
  renderChosen();
  paint(CURRENT_FIELD);
  loadLedger();
}

function clearAll() {
  CHOSEN = [];
  FOCUS = null;
  $('context-card').hidden = true;
  renderAdminList($('search').value);
  renderChosen();
  paint(CURRENT_FIELD);
  loadLedger();
}
function vintage(src, lvl, year) {
  return `<span class="vintage">${esc(src || 'n/a')}${lvl ? ` · L${lvl}` : ''}${year ? ` · ${year}` : ''}</span>`;
}
async function showContext(id) {
  const c = await get(`/admin/${id}/context`);
  const n = v => v === null || v === undefined ? '<em>not established</em>' : v;
  $('context-card').hidden = false;
  $('admin-context').innerHTML = `<p class="muted">${esc(c.name || nameOf(id))}</p><table>
    <tr><td>Type</td><td>${esc(c.local_body_type || 'n/a')}</td></tr>
    <tr><td>Area</td><td>${c.area_km2 ? c.area_km2.toFixed(1) + ' km²' : 'n/a'}</td></tr>
    <tr><td>Population</td><td>${n(c.population.value && c.population.value.toLocaleString())}<br>${vintage(c.population.source, c.population.source_level, c.population.data_year)}</td></tr>
    <tr><td>Literacy</td><td>${n(c.literacy_rate.value)}${c.literacy_rate.value ? '%' : ''}</td></tr>
    <tr><td>Flood 50yr</td><td>${c.flood.historical_50yr_share == null ? 'n/a' : (c.flood.historical_50yr_share * 100).toFixed(1) + '%'}<br>${vintage(c.flood.source, c.flood.source_level, c.flood.data_year)}</td></tr>
    <tr><td>RCP 8.5</td><td>${c.flood.rcp85_50yr_share == null ? 'n/a' : (c.flood.rcp85_50yr_share * 100).toFixed(1) + '%'}</td></tr>
    <tr><td>Landslide</td><td>${esc(c.landslide.susceptibility || 'none mapped')}<br>${vintage(c.landslide.source, c.landslide.source_level, c.landslide.data_year)}</td></tr>
    <tr><td>Transit stops</td><td>${n(c.transit.stops)}</td></tr>
    <tr><td>Nearest stop</td><td>${c.transit.nearest_stop_m == null ? 'n/a' : Math.round(c.transit.nearest_stop_m) + ' m'}<br>${vintage(c.transit.source, c.transit.source_level, c.transit.data_year)}${c.transit.feed_months_stale ? ` · <b>${c.transit.feed_months_stale} mo stale</b>` : ''}</td></tr>
  </table>`;
}

/* ---------- rendering the evaluation ---------- */
function weightBars(weights) {
  const max = Math.max(...Object.values(weights), 0.0001);
  const lead = Object.keys(weights).sort((a, b) => weights[b] - weights[a])[0];
  return Object.entries(weights).sort((a, b) => b[1] - a[1]).map(([d, w]) => `
    <div class="wbar${d === lead ? ' lead' : ''}">
      <div class="nm">${esc(d)}</div>
      <div class="track"><div class="fill" style="width:${(w / max * 100).toFixed(1)}%"></div></div>
      <div class="val">${(w * 100).toFixed(1)}%</div>
    </div>`).join('');
}
function weightDetail(byArea) {
  return Object.entries(byArea).map(([id, p]) =>
    `<h4 style="margin:14px 0 2px">${esc(nameOf(id))}</h4>` +
    Object.entries(p.detail).map(([d, x]) => `
      <p style="margin:9px 0 3px"><b style="text-transform:capitalize">${esc(d)}</b>:
        relevance ${x.scenario_relevance} (term ${x.relevance_term}) ×
        evidence ${x.evidence_confidence} × modulator ${x.signal_modulator}
        = raw <b>${x.raw_weight}</b> → <b>${(x.final_weight * 100).toFixed(1)}%</b></p>
      <table class="grid"><tr><th>parameter</th><th>norm</th><th>conf</th><th>source</th></tr>
      ${(x.parameters_used || []).map(q => `<tr><td>${esc(q.parameter)}</td><td>${q.normalized}</td>
        <td>${q.confidence}</td>
        <td class="lvl">${esc(q.source || '')}${q.source_level ? ` · L${q.source_level}` : ''}</td></tr>`).join('')}
      ${(x.parameters_skipped || []).map(q => `<tr><td>${esc(q.parameter)}</td><td colspan="3">
        ${esc(q.reason)}</td></tr>`).join('')}
      </table>`).join('')).join('');
}
function renderAreas(areas) {
  $('areas-card').hidden = false;
  $('areas').innerHTML = `<table class="grid">
    <tr><th>local body</th><th>type</th><th>population</th><th>suitability</th>
        <th>stance</th><th>leading domain</th></tr>
    ${areas.map(a => `<tr${a.blocked ? ' class="blocked"' : ''}>
      <td><b>${esc(a.admin_name)}</b></td><td>${esc(a.local_body_type || 'n/a')}</td>
      <td>${a.population === null ? 'n/a' : a.population.toLocaleString()}</td>
      <td>${a.suitability_score.toFixed(2)}</td>
      <td><span class="tag ${a.blocked ? 'unavailable' : 'real'}">${esc(a.stance)}</span></td>
      <td>${esc(a.leading_domain)}</td></tr>`).join('')}</table>`;
}
function renderConstraints(rows) {
  $('constraint-card').hidden = false;
  if (!rows.length) {
    $('constraints').innerHTML = '<p class="muted">No constraints triggered in any selected area.</p>';
    return;
  }
  const rank = {block: 0, condition: 1, advisory: 2};
  $('constraints').innerHTML = rows.slice().sort((a, b) =>
    (rank[a.severity] ?? 3) - (rank[b.severity] ?? 3)).map(k => `
    <div class="con ${k.severity}">
      <div class="code">${esc(k.severity.toUpperCase())} · ${esc(k.code)} · ${esc(k.admin_name)}</div>
      <div>${esc(k.message)}</div>
      <div class="prov">measured ${k.measured_value == null ? 'n/a' : (+k.measured_value).toFixed(4)}
        vs threshold ${k.threshold} · ${esc(k.parameter)} · ${esc(k.source || '')}
        ${k.source_level ? `· L${k.source_level}` : ''} ${k.data_year ? `· ${k.data_year}` : ''}
        · model override: ${k.overridable_by_model ? 'yes' : '<b>not permitted</b>'}</div>
    </div>`).join('');
}
function renderConflicts(list) {
  $('conflict-card').hidden = false;
  $('conflicts').innerHTML = list && list.length
    ? list.map(t => `<div class="cf critical"><div>${esc(t)}</div></div>`).join('')
    : '<p class="muted">No conflicts detected.</p>';
}

function renderAgents(a) {
  if (!a) { $('agent-card').hidden = true; return; }
  $('agent-card').hidden = false;
  if (!a.available) {
    $('agents').innerHTML = `<p class="muted">Agents did not run: ${esc(a.reason || 'no backend')}.
      The measured position, weights and constraints above are unaffected. The
      deterministic path does not depend on them.</p>`;
    return;
  }
  // Every key the four schemas can carry, in reading order. Agents differ in
  // which they fill, so anything absent is simply skipped rather than shown
  // empty — an empty field reads as "assessed and found nothing", which is the
  // opposite of what a missing key means.
  const fields = ['recommendation', 'feasibility_verdict', 'verdict', 'risk_band',
                  'estimated_cost', 'income_employment_impact', 'accessibility_impact',
                  'capacity_assessment', 'environmental_impact', 'mitigation',
                  // healthcare and education
                  'access_assessment', 'service_gap', 'impact_of_proposal',
                  'population_at_risk', 'workforce_implication',
                  // budget allocation
                  'affordability', 'assessment', 'annual_entitlement_share',
                  'funding_route', 'opportunity_cost',
                  'confidence'];
  const lists = ['risks', 'assumptions', 'data_gaps', 'mitigations', 'conditions',
                 'cost_pressures', 'phasing'];
  const order = DOMAIN_ORDER;
  $('agents').innerHTML =
    `<p class="muted">backend ${esc(a.backend.kind)} / ${esc(a.backend.model)} ·
      twin facts anchored on ${esc(nameOf(a.anchor_admin_id))}</p>` +
    order.filter(k => a[k]).map(k => {
      const d = a[k];
      if (d.error) return `<div class="ag"><h4>${esc(k)}</h4>
        <p class="muted">failed: ${esc(d.error)}</p></div>`;
      const an = d.analysis || {};
      const cites = (d.evidence && d.evidence.citations) || [];
      return `<div class="ag"><h4>${esc(k)}</h4><dl>` +
        fields.filter(f => an[f] !== undefined && an[f] !== null && an[f] !== '')
              .map(f => `<dt>${esc(f.replace(/_/g, ' '))}</dt>
                         <dd>${esc(String(an[f]))}</dd>`).join('') +
        lists.filter(f => Array.isArray(an[f]) && an[f].length)
             .map(f => `<dt>${esc(f.replace(/_/g, ' '))}</dt><dd><ul>` +
               an[f].map(x => `<li>${esc(typeof x === 'string' ? x : JSON.stringify(x))}</li>`).join('') +
               '</ul></dd>').join('') +
        `</dl>${cites.length ? `<p class="muted" style="margin-top:6px">cites ${
          cites.map(c => esc(c.source_file)).join(', ')}</p>` : ''}</div>`;
    }).join('');
}

function renderSupervisor(s, d) {
  $('supervisor-card').hidden = false;
  if (!s) {
    $('supervisor').innerHTML = `<p class="headline">${esc(d.headline)}</p>
      <p class="muted">The Supervisor did not run. The agents were not enabled, or
      no backend was reachable. The measured decision above stands on its own.</p>`;
    $('pn-card').hidden = true;
    return;
  }
  if (!s.available) {
    $('supervisor').innerHTML = `<p class="headline">${esc(d.headline)}</p>
      <p class="muted">Supervisor synthesis unavailable: ${esc(s.reason || 'n/a')}.</p>`;
    $('pn-card').hidden = true;
    return;
  }
  const verdictClass = {proceed: 'ok', proceed_with_conditions: 'warn',
                        revise: 'warn', do_not_proceed: 'bad'}[s.verdict] || 'warn';
  const list = (title, items, render) => !Array.isArray(items) || !items.length ? '' :
    `<h4>${title}</h4><ul class="sup">${items.map(render).join('')}</ul>`;
  $('supervisor').innerHTML = `
    ${s.verdict ? `<p><span class="verdict ${verdictClass}">${esc(String(s.verdict).replace(/_/g, ' '))}</span>
      ${s.confidence ? `<span class="muted">confidence ${esc(s.confidence)}</span>` : ''}</p>` : ''}
    <p class="headline">${esc(s.final_recommendation || d.headline)}</p>
    ${s.budget_assessment ? `<p><b>Budget:</b> ${esc(s.budget_assessment)}
      ${d.budget_inr_crore ? `<span class="muted">(₹${d.budget_inr_crore} crore stated)</span>` : ''}</p>` : ''}
    ${list('Conditions to proceed', s.conditions, c =>
      `<li>${esc(typeof c === 'string' ? c : JSON.stringify(c))}</li>`)}
    ${list('Trade-offs', s.trade_offs, t => typeof t === 'string'
      ? `<li>${esc(t)}</li>`
      : `<li><b>${esc((t.between || []).join ? (t.between || []).join(' vs ') : t.between)}</b>:
         ${esc(t.tension || '')} <i>${esc(t.resolution || '')}</i></li>`)}
    ${list('Unresolved domain concerns', s.dissenting_domains, x => `<li>${esc(x)}</li>`)}
    ${list('Could not be assessed from available data', s.data_gaps, x =>
      `<li>${esc(typeof x === 'string' ? x : JSON.stringify(x))}</li>`)}
    ${(s.bound_by && s.bound_by.areas_blocked && s.bound_by.areas_blocked.length)
      ? `<p class="prov">Bound by measured data: blocked in
         ${s.bound_by.areas_blocked.map(esc).join(', ')}. The Supervisor cannot
         override this.</p>` : ''}`;

  const side = (items, kind) => !Array.isArray(items) || !items.length
    ? `<p class="muted">None reported.</p>`
    : items.map(x => {
        if (typeof x === 'string') return `<div class="pn ${kind}"><div>${esc(x)}</div></div>`;
        return `<div class="pn ${kind}">
          <div class="pnhead">${esc(x.domain || '')}${x.severity ? ` · ${esc(x.severity)}` : ''}</div>
          <div>${esc(x.point || '')}</div>
          ${x.evidence ? `<div class="prov">${esc(x.evidence)}</div>` : ''}</div>`;
      }).join('');
  $('pn-card').hidden = false;
  $('pn').innerHTML = `<div class="pncols">
    <div><h4 class="good">Positives</h4>${side(s.positives, 'good')}</div>
    <div><h4 class="bad">Negatives</h4>${side(s.negatives, 'bad')}</div>
  </div>`;
}

async function renderCoverage(d) {
  // Parameters come in the run payload, one set per selected area, so there is
  // no second round trip and the reader can audit every area rather than only
  // the anchor. The anchors table is fetched because it is small and identical
  // for every run.
  const byArea = d.parameters || {};
  const ids = Object.keys(byArea);
  if (!ids.length) { $('coverage-card').hidden = true; return; }
  $('coverage-card').hidden = false;

  const draw = (id) => {
    const params = byArea[id];
    const counts = params.reduce((a, p) => ({...a, [p.status]: (a[p.status] || 0) + 1}), {});
    $('coverage-summary').innerHTML = `
      ${ids.length > 1 ? `<label class="inline">Area
        <select id="coverage-area">${ids.map(x =>
          `<option value="${x}"${x === id ? ' selected' : ''}>${esc(nameOf(x))}</option>`).join('')}
        </select></label>` : ''}
      <p class="muted">${esc(nameOf(id))} · ${params.length} parameters
        ${counts.unavailable ? `· ${counts.unavailable} without a data path` : ''}</p>`;

    const order = {real: 0, proxy: 1, unavailable: 2};
    const num = v => v === null || v === undefined ? '<em>n/a</em>'
      : (+v).toLocaleString(undefined, {maximumFractionDigits: 3});
    $('coverage').innerHTML = `<table class="grid">
      <tr><th>parameter</th><th>domain</th><th>value</th><th>conf</th><th>source</th></tr>
      ${params.slice().sort((a, b) =>
          order[a.status] - order[b.status] || a.domain.localeCompare(b.domain))
        .map(p => `<tr>
          <td>${esc(p.name)}</td><td>${esc(p.domain)}</td><td>${num(p.value)}</td>
          <td>${p.confidence}</td>
          <td class="lvl">${esc(p.source || 'n/a')}${p.source_level ? ` · L${p.source_level}` : ''}${p.data_year ? ` · ${p.data_year}` : ''}</td>
        </tr>`).join('')}
      </table>`;

    const picker = $('coverage-area');
    if (picker) picker.onchange = e => draw(e.target.value);
  };
  draw(d.anchor_admin_id in byArea ? d.anchor_admin_id : ids[0]);

}




/* ---------- zone visibility ---------- */
// A numbered zone heading with nothing under it reads as a broken page, so the
// headings follow their contents. Driven off the cards' own hidden state rather
// than a parallel flag, which would drift the moment a renderer changed.
function syncZones() {
  for (const zone of document.querySelectorAll('.zone')) {
    const cards = [...zone.querySelectorAll('.card')];
    zone.hidden = cards.length > 0 && cards.every(c => c.hidden);
  }
}

/* ---------- explore: instant, measured-data-only views ---------- */
// These three exist because the agents take minutes and the deterministic
// engine takes milliseconds. Showing the arithmetic first makes it obvious
// which half of the system is doing the actual deciding.

async function showNeeds() {
  if (!CHOSEN.length) return alert('Select at least one local body first.');
  const btn = $('needs-run');
  btn.disabled = true;
  try {
    // /api/needs takes one area at a time; merge the selection client-side and
    // keep the worst severity per shortfall.
    const parts = await Promise.all(CHOSEN.map(id => get(`/needs?admin_id=${encodeURIComponent(id)}`)));
    const ranked = parts.flatMap(p => p.ranked || []).sort((a, b) => b.severity - a.severity);
    const pressure = {};
    for (const p of parts) {
      for (const [dom, v] of Object.entries(p.domain_pressure || {})) {
        pressure[dom] = Math.max(pressure[dom] ?? 0, v);
      }
    }
    renderNeeds({
      needs: {ranked},
      budget_split: Object.fromEntries(Object.entries(pressure).map(([d, v]) =>
        [d, {need_pressure: v, share: 0, envelope_inr_crore: null,
             basis: 'measured need pressure (no budget given yet)'}])),
    });
    syncZones();
    $('needs-card').scrollIntoView({behavior: 'smooth', block: 'start'});
    $('explore-note').textContent =
      `${ranked.length} shortfall(s) above the reporting floor across ${CHOSEN.length} area(s).`;
  } catch (error) {
    $('explore-note').innerHTML = `<b style="color:var(--bad)">${esc(error.message)}</b>`;
  } finally {
    btn.disabled = false;
  }
}

async function showRanking() {
  const key = $('rank-scenario').value;
  const btn = $('rank-run');
  btn.disabled = true;
  try {
    const rows = await get(`/scenario/${encodeURIComponent(key)}/ranking?limit=97`);
    $('rank-card').hidden = false;
    const chosen = new Set(CHOSEN);
    $('rank').innerHTML = `<p class="muted">${rows.length} local bodies scored for
      <b>${esc((SCENARIOS.find(x => x.key === key) || {}).label || key)}</b>.
      Your selection is highlighted.</p>
      <table class="grid">
      <tr><th>#</th><th>local body</th><th>type</th><th>score</th><th>stance</th><th>leading domain</th></tr>
      ${rows.map((r, n) => `<tr class="${chosen.has(r.admin_id) ? 'picked' : ''}${
          r.stance === 'not_permitted' ? ' blocked' : ''}">
        <td>${n + 1}</td><td><b>${esc(r.name)}</b></td><td>${esc(r.type || 'n/a')}</td>
        <td>${r.score.toFixed(3)}</td>
        <td><span class="tag ${r.stance === 'not_permitted' ? 'unavailable' : 'real'}">${esc(r.stance)}</span></td>
        <td>${esc(r.leading_domain)}</td></tr>`).join('')}</table>`;
    syncZones();
    $('rank-card').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (error) {
    $('explore-note').innerHTML = `<b style="color:var(--bad)">${esc(error.message)}</b>`;
  } finally {
    btn.disabled = false;
  }
}

async function showCompare() {
  if (!CHOSEN.length) return alert('Select a local body first.');
  const a = $('cmp-a').value, b = $('cmp-b').value;
  if (a === b) return alert('Pick two different objectives.');
  const btn = $('cmp-run');
  btn.disabled = true;
  try {
    const d = await get(`/scenario/compare?admin_id=${encodeURIComponent(CHOSEN[0])}`
      + `&a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    $('cmp-card').hidden = false;
    const label = k => esc((SCENARIOS.find(x => x.key === k) || {}).label || k);
    const domains = Object.keys(d.weight_shift || {});
    const shift = v => `<span class="${v > 0.001 ? 'up' : v < -0.001 ? 'down' : 'flat'}">${
      v > 0 ? '+' : ''}${(v * 100).toFixed(1)}pp</span>`;
    $('cmp').innerHTML = `
      <p class="muted">${esc(d.admin_name)} ·
        ${d.data_unchanged ? '<b>the underlying data is byte-identical in both runs</b>'
                           : 'data differed between runs'}</p>
      <table class="grid">
        <tr><th>domain weight</th><th>${label(d.scenario_a.key)}</th>
            <th>${label(d.scenario_b.key)}</th><th>shift</th></tr>
        ${domains.map(dom => `<tr>
          <td>${esc(dom)}</td>
          <td>${(d.scenario_a.weights[dom] * 100).toFixed(1)}%${
            d.scenario_a.leading === dom ? ' <b>(leads)</b>' : ''}</td>
          <td>${(d.scenario_b.weights[dom] * 100).toFixed(1)}%${
            d.scenario_b.leading === dom ? ' <b>(leads)</b>' : ''}</td>
          <td>${shift(d.weight_shift[dom])}</td></tr>`).join('')}
        <tr><td><b>suitability</b></td><td>${d.scenario_a.score.toFixed(3)}</td>
            <td>${d.scenario_b.score.toFixed(3)}</td><td></td></tr>
        <tr><td><b>stance</b></td><td>${esc(d.scenario_a.stance)}</td>
            <td>${esc(d.scenario_b.stance)}</td><td></td></tr>
      </table>
      <p class="prov">leading domain ${d.leading_domain_changed ? 'changed' : 'unchanged'}
        · decision ${d.decision_changed ? 'changed' : 'unchanged'}
        · no language model participates in either column</p>`;
    syncZones();
    $('cmp-card').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (error) {
    $('explore-note').innerHTML = `<b style="color:var(--bad)">${esc(error.message)}</b>`;
  } finally {
    btn.disabled = false;
  }
}

/* ---------- manual ledger entry ---------- */
async function saveManual() {
  const title = $('m-title').value.trim();
  if (!title) return alert('Give it a title.');
  if (!CHOSEN.length) return alert('Select the areas it applies to.');
  const cost = $('m-cost').value;
  try {
    await post('/implementations', {
      idea: {
        title, domain: $('m-domain').value,
        addresses: $('m-addresses').value || null,
        est_cost_inr_crore: cost ? Number(cost) : null,
      },
      admin_ids: CHOSEN, origin: 'manual',
    });
  } catch (error) {
    $('manual-note').innerHTML = `<b style="color:var(--bad)">${esc(error.message)}</b>`;
    return;
  }
  $('m-title').value = '';
  $('m-cost').value = '';
  $('manual-note').textContent = 'Logged. Future ideation will not re-propose it.';
  await loadLedger();
}

/* ---------- implementation ledger ---------- */
const STATUS_LABEL = {planned: 'planned', in_progress: 'in progress',
                      completed: 'completed', on_hold: 'on hold',
                      cancelled: 'cancelled'};
// Mirrors TRANSITIONS in src/decision/implementations.py. The server is the
// authority and refuses an illegal move with 409; this only avoids offering one.
const NEXT = {planned: ['in_progress', 'on_hold', 'cancelled'],
              in_progress: ['completed', 'on_hold', 'cancelled'],
              on_hold: ['planned', 'in_progress', 'cancelled'],
              completed: [], cancelled: ['planned']};

async function loadLedger() {
  let d;
  try {
    const params = CHOSEN.map(id => `admin_id=${encodeURIComponent(id)}`);
    const filter = ($('ledger-filter') || {}).value;
    if (filter) params.push(`status=${encodeURIComponent(filter)}`);
    d = await get('/implementations' + (params.length ? '?' + params.join('&') : ''));
  } catch (error) {
    $('ledger').innerHTML = `<p class="muted">Could not load the ledger: ${esc(error.message)}</p>`;
    return;
  }
  const rows = d.implementations || [];
  const counts = d.by_status || {};
  $('ledger-summary').innerHTML = `<p class="muted">
    ${rows.length} item(s)${CHOSEN.length ? ' in the selected areas' : ' district-wide'}
    · committed ${crore(d.committed_inr_crore)}
    ${Object.entries(counts).map(([s, v]) =>
      `· <span class="st ${esc(s)}">${esc(STATUS_LABEL[s] || s)} ${v.count}</span>`).join(' ')}</p>`;

  $('ledger').innerHTML = rows.length ? `<table class="grid">
    <tr><th>item</th><th>domain</th><th>addresses</th><th>cost</th><th>status</th><th>move to</th></tr>
    ${rows.map(r => `<tr>
      <td><b>${esc(r.title)}</b>
        ${r.detail ? `<div class="prov">${esc(r.detail)}</div>` : ''}
        ${Array.isArray(r.conditions) && r.conditions.length
          ? `<div class="prov">conditions: ${r.conditions.map(esc).join('; ')}</div>` : ''}
        <div class="prov">${esc((r.admin_ids || []).map(nameOf).join(', '))}
          · logged ${esc(String(r.created_at).slice(0, 10))}
          · origin ${esc(r.origin)}${r.note ? ` · ${esc(r.note)}` : ''}</div></td>
      <td>${esc(r.domain)}</td>
      <td>${r.addresses ? `<code>${esc(r.addresses)}</code>` : 'n/a'}</td>
      <td>${crore(r.est_cost_inr_crore)}</td>
      <td><span class="st ${esc(r.status)}">${esc(STATUS_LABEL[r.status] || r.status)}</span></td>
      <td>${(NEXT[r.status] || []).map(s =>
        `<button class="tiny" data-impl="${r.id}" data-to="${s}">${esc(STATUS_LABEL[s])}</button>`
        ).join(' ') || '<span class="muted">n/a</span>'}
        <div><button class="tiny ghost" data-hist="${r.id}">history</button></div></td>
    </tr>
    <tr class="histrow" id="hist-${r.id}" hidden><td colspan="6"></td></tr>`).join('')}</table>`
    : '<p class="muted">Nothing committed yet. Propose ideas for a budget, then press <b>Implement</b> on the ones you want to take forward.</p>';
}

async function toggleHistory(id) {
  const row = document.getElementById(`hist-${id}`);
  if (!row) return;
  if (!row.hidden) { row.hidden = true; return; }
  row.hidden = false;
  const cell = row.firstElementChild;
  cell.innerHTML = '<span class="muted">loading</span>';
  try {
    const d = await get(`/implementations/${id}`);
    const i = d.implementation;
    cell.innerHTML = `<div class="timeline">
      ${d.events.map(e => `<div class="tl">
        <span class="when">${esc(String(e.created_at).slice(0, 16).replace('T', ' '))}</span>
        <span class="st ${esc(e.to_status)}">${esc(STATUS_LABEL[e.to_status] || e.to_status)}</span>
        ${e.from_status ? `<span class="muted">from ${esc(STATUS_LABEL[e.from_status] || e.from_status)}</span>` : '<span class="muted">created</span>'}
        ${e.note ? `<span>${esc(e.note)}</span>` : ''}
      </div>`).join('')}
      <div class="prov">${i.evidence ? `evidence: ${esc(i.evidence)} · ` : ''}origin
        ${esc(i.origin)}${i.origin_id ? ` (${esc(i.origin_id)})` : ''}
        ${i.completed_at ? ` · completed ${esc(String(i.completed_at).slice(0, 10))}` : ''}
        · next: ${d.allowed_next.length ? d.allowed_next.map(x => esc(STATUS_LABEL[x] || x)).join(', ') : 'terminal'}</div>
    </div>`;
  } catch (error) {
    cell.innerHTML = `<span class="muted">Could not load history: ${esc(error.message)}</span>`;
  }
}

async function moveImplementation(id, to) {
  // The note is the difference between a timeline that records state and one
  // that records why. Optional, because forcing it would just produce "x".
  const note = prompt(`Moving to ${STATUS_LABEL[to] || to}. Note (optional):`, '');
  if (note === null) return;   // cancelled
  try {
    await patch(`/implementations/${id}`, {status: to, note: note.trim() || null});
  } catch (error) {
    alert(`Could not move it: ${error.message}`);
  }
  await loadLedger();
}

async function implementIdea(idea, areaNames) {
  // The ledger is keyed on admin_id; the agents name areas in prose, so map
  // back through UNITS and fall back to the whole selection when a name does
  // not resolve rather than logging the item against nothing.
  const ids = (areaNames || []).map(n => {
    const hit = UNITS.find(u => (u.name || '').toLowerCase() === String(n).toLowerCase())
      || UNITS.find(u => (u.name || '').toLowerCase().includes(String(n).toLowerCase()));
    return hit && hit.admin_id;
  }).filter(Boolean);
  const admin_ids = ids.length ? ids : CHOSEN;
  try {
    await post('/implementations', {
      idea, admin_ids, origin: 'ideation', origin_id: LAST_IDEATION_ID,
    });
  } catch (error) {
    return alert(`Could not log it: ${error.message}`);
  }
  await loadLedger();
  $('ledger-card').scrollIntoView({behavior: 'smooth', block: 'start'});
}

/* ---------- budget-only ideation ---------- */
const crore = v => v === null || v === undefined ? 'n/a'
  : '₹' + Number(v).toLocaleString(undefined, {maximumFractionDigits: 2}) + ' cr';

function renderNeeds(d) {
  $('needs-card').hidden = false;
  const split = d.budget_split || {};
  $('split').innerHTML = `<table class="grid">
    <tr><th>domain</th><th>need pressure</th><th>share</th><th>envelope</th><th>basis</th></tr>
    ${Object.entries(split).sort((a, b) => b[1].share - a[1].share).map(([dom, v]) => `
      <tr><td><b>${esc(dom)}</b></td><td>${v.need_pressure.toFixed(2)}</td>
      <td>${(v.share * 100).toFixed(1)}%</td><td>${crore(v.envelope_inr_crore)}</td>
      <td class="lvl">${esc(v.basis)}</td></tr>`).join('')}</table>`;

  const ranked = (d.needs && d.needs.ranked) || [];
  $('needs').innerHTML = ranked.length ? `<table class="grid">
    <tr><th>severity</th><th>domain</th><th>area</th><th>shortfall</th><th>evidence</th></tr>
    ${ranked.slice(0, 14).map(n => `<tr>
      <td><b>${n.severity.toFixed(2)}</b></td><td>${esc(n.domain)}</td>
      <td>${esc(n.admin_name)}</td>
      <td>${esc(n.reading.split(': ').slice(1).join(': ').replace(/ puts it in the.*$/, ''))}</td>
      <td class="lvl">${esc(n.source || '')}${n.data_year ? ` · ${n.data_year}` : ''}</td>
    </tr>`).join('')}</table>`
    : '<p class="muted">No shortfall above the reporting floor in the selected areas.</p>';
}

function renderHazardBlocks(blocks) {
  if (!blocks || !blocks.length) return '';
  return `<h4>Binding hazard constraints</h4>` + blocks.map(b => `
    <div class="con block"><div class="code">BLOCK · ${esc(b.code)} · ${esc(b.admin_name)}</div>
    <div>${esc(b.message)}</div>
    <div class="prov">applies to ${esc(b.applies_to)} · measured
      ${b.measured_value} vs threshold ${b.threshold} · ${esc(b.source || '')}
      · model override: <b>not permitted</b></div></div>`).join('');
}

function renderProposals(p) {
  if (!p) { $('proposals-card').hidden = true; return; }
  $('proposals-card').hidden = false;
  if (!p.available) {
    $('proposals').innerHTML = `<p class="muted">Agents did not run: ${esc(p.reason || 'no backend')}.
      The needs and the budget split above are unaffected.</p>`;
    return;
  }
  const order = DOMAIN_ORDER;
  $('proposals').innerHTML =
    `<p class="muted">backend ${esc(p.backend.kind)} / ${esc(p.backend.model)}</p>` +
    order.filter(k => p[k]).map(k => {
      const d = p[k];
      if (!d.available) return `<div class="ag"><h4>${esc(k)}</h4>
        <p class="muted">${esc(d.reason || 'no proposal')}</p></div>`;
      const ideas = d.ideas || [];
      const sh = d.envelope_shortfall;
      return `<div class="ag"><h4>${esc(k)}
        <span class="w">${ideas.length} idea(s) · total ${crore(d.total_est_cost_inr_crore)}
        ${d.envelope_share !== undefined ? `· ${(d.envelope_share * 100).toFixed(0)}% of envelope` : ''}
        ${d.fits_envelope === false ? '· <b style="color:var(--bad)">over envelope</b>' : ''}
        ${d.mandate ? `· mandate ${esc(d.mandate)}` : ''}</span></h4>` +
        (sh ? `<div class="prov ${sh.claimed_substantial_use ? 'dupnote' : ''}">
          ${esc(sh.note)}</div>` : '') +
        (ideas.length ? ideas.map((i, n) => `<div class="idea${i.duplicate_of ? ' dup' : ''}">
          <div class="ititle">${esc(i.title || '(untitled)')}
            <span class="feas ${esc(i.feasibility || '')}">${esc(i.feasibility || '')}</span>
            <span class="cost">${crore(i.est_cost_inr_crore)}</span></div>
          ${i.duplicate_of ? `<div class="prov dupnote">Already logged as
            <b>${esc(i.duplicate_of.title)}</b> (${esc(i.duplicate_of.status)}).
            ${esc(i.duplicate_of.reason)}. Flagged automatically.</div>`
            : `<button class="tiny impl" data-domain="${esc(k)}" data-idx="${n}">Implement this</button>`}
          <div>${esc(i.what || '')}</div>
          <div class="prov">addresses <code>${esc(i.addresses || 'n/a')}</code>
            ${Array.isArray(i.areas) && i.areas.length ? ` · ${i.areas.map(esc).join(', ')}` : ''}
            ${i.feasibility_reason ? ` · ${esc(i.feasibility_reason)}` : ''}</div>
          ${i.evidence ? `<div class="prov">evidence: ${esc(i.evidence)}</div>` : ''}
          ${Array.isArray(i.depends_on) && i.depends_on.length
            ? `<div class="prov">depends on: ${i.depends_on.map(esc).join('; ')}</div>` : ''}
        </div>`).join('') : '<p class="muted">No idea proposed.</p>') +
        (Array.isArray(d.could_not_cost) && d.could_not_cost.length
          ? `<p class="muted" style="margin-top:6px">could not cost:
             ${d.could_not_cost.map(x => esc(typeof x === 'string' ? x : JSON.stringify(x))).join('; ')}</p>`
          : '') +
        `</div>`;
    }).join('');
}

function renderPortfolio(d) {
  $('portfolio-card').hidden = false;
  const p = d.portfolio;
  if (!p || !p.available) {
    $('portfolio').innerHTML = `<p class="muted">No portfolio:
      ${esc((p && p.reason) || 'the agents did not run')}. The measured needs and
      the budget split below stand on their own.</p>` + renderHazardBlocks(d.hazard_blocks);
    return;
  }
  const items = p.portfolio || [];
  // Prefer the server's recomputed figures over the model's own arithmetic.
  const bc = p.budget_check || {};
  const spent = bc.selected_cost_inr_crore ?? p.total_cost_inr_crore;
  const headroom = bc.selected_cost_inr_crore !== undefined
    ? d.budget_inr_crore - bc.selected_cost_inr_crore
    : p.budget_headroom_inr_crore;
  const used = bc.utilisation;
  const over = (headroom ?? 0) < 0;
  const list = (title, arr, render) => !Array.isArray(arr) || !arr.length ? '' :
    `<h4>${title}</h4><ul class="sup">${arr.map(render).join('')}</ul>`;
  $('portfolio').innerHTML = `
    <p><span class="verdict ${over ? 'bad' : used !== undefined && used < 0.6 ? 'warn' : 'ok'}">
      ${crore(spent)} of ${crore(d.budget_inr_crore)}${used !== undefined ? ` · ${(used * 100).toFixed(0)}% used` : ''}</span>
      <span class="muted">headroom ${crore(headroom)}${over ? ', over budget' : ''}
      ${p.confidence ? ` · confidence ${esc(p.confidence)}` : ''}</span></p>
    ${used !== undefined && used < 0.6 && !over ? `<div class="shortfall">
      <p><b>${(used * 100).toFixed(0)}% of the budget is allocated.</b>
      The agents proposed ${crore(bc.total_offered_by_agents_inr_crore)} of work
      in total, and the Supervisor selected ${crore(spent)} of that.</p>
      ${bc.unproposed_inr_crore > 0 ? `<p>${crore(bc.unproposed_inr_crore)} has
        no proposed use at all. The agents could not identify work at this scale,
        which is a limit of what they were able to propose rather than a finding
        about the district.</p>` : ''}
      <p class="prov">Figures recomputed from the selected items, not taken from
        the model.</p></div>` : ''}
    ${(bc.unaccounted_proposals || []).length ? `<div class="contradiction">
      <h4>Proposed, then unaccounted for</h4>
      <p class="muted">Offered by an agent and neither selected nor deferred.
        Work does not get to disappear without a reason.</p>
      <ul class="sup">${bc.unaccounted_proposals.map(u => `<li>
        <b>${esc(u.title || '')}</b> (${esc(u.domain || '')})
        ${crore(u.est_cost_inr_crore)}</li>`).join('')}</ul></div>` : ''}
    ${(bc.contradictory_deferrals || []).length ? `<div class="contradiction">
      <h4>Deferrals that do not hold up</h4>
      <p class="muted">These were deferred for budget reasons while budget
        remained. The arithmetic contradicts the stated reason. Shown rather than
        corrected, because quietly rewriting the reasoning would hide it.</p>
      <ul class="sup">${bc.contradictory_deferrals.map(c => `<li>
        <b>${esc(c.title || '')}</b> (${esc(c.domain || '')}):
        "${esc(c.stated_reason || '')}" &mdash; ${esc(c.contradiction)}</li>`).join('')}
      </ul></div>` : ''}
    ${p.rationale ? `<p class="headline">${esc(p.rationale)}</p>` : ''}
    ${items.length ? `<table class="grid">
      <tr><th>#</th><th>intervention</th><th>domain</th><th>areas</th><th>cost</th><th>feasibility</th></tr>
      ${items.map((i, n) => `<tr>
        <td>${n + 1}</td><td><b>${esc(i.title || '')}</b>
          ${i.why_selected ? `<div class="prov">${esc(i.why_selected)}</div>` : ''}
          ${Array.isArray(i.conditions) && i.conditions.length
            ? `<div class="prov">conditions: ${i.conditions.map(esc).join('; ')}</div>` : ''}</td>
        <td>${esc(i.domain || '')}</td>
        <td>${Array.isArray(i.areas) ? i.areas.map(esc).join(', ') : esc(i.areas || '')}</td>
        <td>${crore(i.est_cost_inr_crore)}</td>
        <td><span class="feas ${esc(i.feasibility || '')}">${esc(i.feasibility || '')}</span>
          <div><button class="tiny pimpl" data-idx="${n}">Implement</button></div></td>
      </tr>`).join('')}</table>` : '<p class="muted">Nothing selected.</p>'}
    ${p.biggest_unmet_need ? `<h4>Biggest need this does not address</h4>
      <p>${esc(p.biggest_unmet_need)}</p>` : ''}
    ${list('Sequencing', p.sequencing, x => typeof x === 'string' ? `<li>${esc(x)}</li>`
      : `<li><b>${esc(x.phase || '')}</b>: ${esc(Array.isArray(x.items) ? x.items.join(', ') : (x.items || ''))}
         <i>${esc(x.reason || '')}</i></li>`)}
    ${list('Deferred', p.deferred, x => typeof x === 'string' ? `<li>${esc(x)}</li>`
      : `<li><b>${esc(x.title || '')}</b> (${esc(x.domain || '')}): ${esc(x.why_deferred || '')}</li>`)}
    ${list('Could not be assessed from available data', p.data_gaps, x =>
      `<li>${esc(typeof x === 'string' ? x : JSON.stringify(x))}</li>`)}
    ${renderHazardBlocks(d.hazard_blocks)}
    ${(p.bound_by && p.bound_by.hazard_blocked_areas || []).length
      ? `<p class="prov">Bound by measured data: permanent siting blocked in
         ${p.bound_by.hazard_blocked_areas.map(esc).join(', ')}. The Supervisor
         cannot override this.</p>` : ''}`;
}

async function ideate() {
  if (!CHOSEN.length) return alert('Select at least one local body first.');
  const budget = $('ideate-budget').value;
  if (!budget) return alert('Enter a budget.');
  const btn = $('ideate-run');
  btn.disabled = true;
  btn.textContent = 'Agents proposing, minutes';
  $('ideate-note').textContent =
    'Measuring needs, splitting the budget, then seven agents and the Supervisor '
    + 'on a local model. This takes a few minutes.';
  // Hide the review-flow cards so the two modes are never shown mixed together.
  for (const id of ['supervisor-card', 'pn-card', 'agent-card', 'areas-card',
                    'priority-card', 'constraint-card', 'conflict-card']) {
    $(id).hidden = true;
  }
  try {
    const d = await post('/ideate', {admin_ids: CHOSEN, budget_inr_crore: Number(budget)});
    LAST_IDEATION_ID = d.ideation_id;
    LAST_PROPOSALS = d;
    renderPortfolio(d);
    renderNeeds(d);
    renderProposals(d.proposals);
    // The needs table already carries per-parameter provenance for this flow,
    // so the coverage card would only repeat it.
    $('coverage-card').hidden = true;
    syncZones();
    $('ideate-note').textContent = '';
    $('portfolio-card').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (error) {
    $('ideate-note').innerHTML = `<b style="color:var(--bad)">${esc(error.message)}</b>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Propose work for this budget';
  }
}

function renderResult(d) {
  for (const id of ['portfolio-card', 'needs-card', 'proposals-card']) $(id).hidden = true;
  renderSupervisor(d.supervisor, d);
  renderAgents(d.agents);
  renderAreas(d.areas);
  $('priority-card').hidden = false;
  $('weights').innerHTML = weightBars(d.weights);
  const b = d.weight_basis;
  $('weight-basis').innerHTML = `Weights drawn from the <b>${esc(b.scenario_label)}</b>
    relevance profile (match ${b.match_score}, ${esc(b.method)}), blended across
    ${d.areas.length} area(s) by population. Formula
    <code>${esc(d.determinism.formula_version)}</code>.`;
  $('weight-detail').innerHTML = weightDetail(d.priority_detail);
  renderConstraints(d.constraints);
  renderConflicts(d.conflicts);
  renderCoverage(d);
  syncZones();
  $('supervisor-card').scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function evaluate() {
  const text = $('idea').value.trim();
  if (!text) return alert('Describe your idea first.');
  if (!CHOSEN.length) return alert('Select at least one affected local body.');
  const btn = $('run'), withAgents = $('with-agents').checked;
  btn.disabled = true;
  btn.textContent = withAgents ? 'Agents running, minutes' : 'Evaluating';
  $('run-note').textContent = withAgents
    ? 'Seven domain agents then the Supervisor, on a local model. This takes several minutes.'
    : '';
  try {
    const body = {text, admin_ids: CHOSEN, with_agents: withAgents};
    const budget = $('budget').value;
    if (budget) body.budget_inr_crore = Number(budget);
    renderResult(await post('/proposal', body));
    $('run-note').textContent = '';
  } catch (error) {
    $('run-note').innerHTML = `<b style="color:var(--bad)">${esc(error.message)}</b>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Evaluate this intervention';
  }
}

/* ---------- boot ---------- */
(async () => {
  try {
    const h = await get('/health');
    $('health').textContent = `${h.status} · ${h.units} local bodies`;
    $('health').className = 'pill ok';
  } catch {
    $('health').textContent = 'API unreachable';
    $('health').className = 'pill err';
    return;
  }
  UNITS = await get('/admin?limit=200');
  // Nothing selected means the whole district, not an error. A planner opening
  // the page is asking about Ernakulam, so that is the starting position and
  // narrowing is the deliberate act.
  if (!CHOSEN.length) CHOSEN = UNITS.map(u => u.admin_id);
  SCENARIOS = await get('/scenarios');
  const options = SCENARIOS.map(x =>
    `<option value="${esc(x.key)}">${esc(x.label)}</option>`).join('');
  for (const id of ['rank-scenario', 'cmp-a', 'cmp-b']) $(id).innerHTML = options;
  // Default the comparison to two objectives that genuinely diverge, so the
  // first click shows the point rather than two near-identical columns.
  $('cmp-a').value = (SCENARIOS.find(x => x.key === 'industrial_development')
                      || SCENARIOS[0]).key;
  $('cmp-b').value = (SCENARIOS.find(x => x.key === 'flood_resilient_development')
                      || SCENARIOS[SCENARIOS.length - 1]).key;
  renderAdminList();
  renderChosen();
  // The parameter names come from the engine, not a hard-coded list: a
  // hand-logged item must be able to target the same shortfalls a proposal can,
  // or it will not suppress a re-proposal.
  try {
    const params = await get(`/admin/${UNITS[0].admin_id}/parameters`);
    $('m-addresses').innerHTML = '<option value="">targets which shortfall</option>'
      + params.slice().sort((a, b) => a.domain.localeCompare(b.domain)
          || a.name.localeCompare(b.name))
        .map(x => `<option value="${esc(x.name)}">${esc(x.domain)} · ${esc(x.name)}</option>`).join('');
  } catch { /* the picker is a convenience; the form works without it */ }
  $('search').oninput = e => renderAdminList(e.target.value);
  $('admin-list').onchange = e => {
    if (e.target.type === 'checkbox') toggle(e.target.value);
  };
  $('chosen').onclick = e => {
    if (e.target.id === 'select-all') return selectAll();
    if (e.target.id === 'clear-all') return clearAll();
    const chip = e.target.closest('.chip[data-id]');
    if (chip) toggle(chip.dataset.id);
  };
  $('layer-select').onchange = e => paint(e.target.value);
  $('run').onclick = evaluate;
  $('ideate-run').onclick = ideate;

  // One delegated handler per container: the buttons are re-rendered on every
  // run, so binding them individually would leak listeners.
  $('proposals').onclick = e => {
    const btn = e.target.closest('button.impl');
    if (!btn || !LAST_PROPOSALS) return;
    const idea = ((LAST_PROPOSALS.proposals[btn.dataset.domain] || {}).ideas
                  || [])[Number(btn.dataset.idx)];
    if (idea) implementIdea({...idea, domain: btn.dataset.domain}, idea.areas);
  };
  $('portfolio').onclick = e => {
    const btn = e.target.closest('button.pimpl');
    if (!btn || !LAST_PROPOSALS) return;
    const item = (LAST_PROPOSALS.portfolio.portfolio || [])[Number(btn.dataset.idx)];
    if (item) implementIdea(item, item.areas);
  };
  $('ledger').onclick = e => {
    const move = e.target.closest('button[data-impl]');
    if (move) return moveImplementation(move.dataset.impl, move.dataset.to);
    const hist = e.target.closest('button[data-hist]');
    if (hist) toggleHistory(hist.dataset.hist);
  };
  $('ledger-filter').onchange = loadLedger;
  $('manual-toggle').onclick = () => {
    const form = $('manual-form');
    form.hidden = !form.hidden;
  };
  $('manual-save').onclick = saveManual;
  $('needs-run').onclick = showNeeds;
  $('rank-run').onclick = showRanking;
  $('cmp-run').onclick = showCompare;
  $('idea').addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) evaluate();
  });
  await initMap();
  await loadLedger();
  syncZones();
})();
