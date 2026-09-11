/**
 * The command centre.
 *
 * Polls the board, renders it, and opens the explain drawer on demand. All the
 * decisions live in render.js; this file is the DOM and the polling loop.
 *
 * The one rule it enforces on its own: a failed poll never blanks the screen.
 * It keeps the last good picture and says how old it is, because a board that
 * empties on a network blip looks exactly like a building that has emptied.
 */

import { Api, Freshness } from './api.js';
import { createTranslator, isRtl, formatDuration } from './i18n.js';
import {
  drillControl, escapeHtml, exitPressure, healthLine, orderForWarden,
  staleness, tiles, timingLine, verdict,
} from './render.js';

const params = new URLSearchParams(location.search);
let lang = params.get('lang') || localStorage.getItem('evac.lang') || 'en';
let t = createTranslator(lang);

// Phase 4 reads identity from the page. A gateway sets these headers in a real
// deployment; the seam is left visible rather than hidden behind a fake login.
const api = new Api().withIdentity({
  userId: params.get('user') || 'commander-1',
  permissions: ['evac:read', 'evac:operate'],
});

const boardFreshness = new Freshness(10_000);
const timingFreshness = new Freshness(30_000);
const zoneFreshness = new Freshness(30_000);
const exitFreshness = new Freshness(30_000);

let drillId = params.get('drill') || null;
let drill = null;
//: Ending a drill stops accountability, so the button arms before it acts.
let endArmed = false;

function applyLanguage() {
  t = createTranslator(lang);
  document.documentElement.lang = lang;
  document.documentElement.dir = isRtl(lang) ? 'rtl' : 'ltr';
  document.getElementById('safety').textContent = t('app.safety_notice');
  document.getElementById('title').textContent = t('app.title');
  document.getElementById('priority-title').textContent = t('board.priority');
  document.getElementById('timing-title').textContent = t('board.timing');
  document.getElementById('zones-title').textContent = t('board.zones');
  document.getElementById('exits-title').textContent = t('bottleneck.title');
  document.getElementById('lang').textContent = lang === 'en' ? 'العربية' : 'English';
}

document.getElementById('lang').addEventListener('click', () => {
  lang = lang === 'en' ? 'ar' : 'en';
  localStorage.setItem('evac.lang', lang);
  applyLanguage();
  paint();
});

async function pickDrill() {
  const drills = await api.listDrills();
  const all = Array.isArray(drills) ? drills : [];
  if (drillId) {
    drill = all.find((d) => d.drill_id === drillId) || drill;
    return drillId;
  }
  drill = all.find((d) => d.status === 'RUNNING') || all[0] || null;
  drillId = drill?.drill_id || null;
  return drillId;
}

/** Refresh one panel, recording the outcome against that panel. */
async function refresh(freshness, fetchOne) {
  try {
    freshness.succeed(await fetchOne());
  } catch (error) {
    // Keep the last good picture. Blanking it would look like an empty
    // building rather than a broken connection.
    freshness.fail(error);
  }
}

async function poll() {
  let id;
  try {
    id = await pickDrill();
  } catch (error) {
    boardFreshness.fail(error);
    paint();
    return;
  }
  if (!id) {
    boardFreshness.fail(new Error('no drill'));
    paint();
    return;
  }

  // One try block around all three put every failure on the board's record.
  // A timing request failing marked the board stale when the board had just
  // arrived, and a zone request failing was recorded nowhere at all, so the
  // assembly panel aged silently while the screen looked current.
  await Promise.all([
    refresh(boardFreshness, () => api.board(id)),
    refresh(timingFreshness, () => api.timing(id)),
    refresh(zoneFreshness, () => api.zones(id)),
    refresh(exitFreshness, () => api.bottlenecks(id)),
  ]);
  paint();
}

function paint() {
  const board = boardFreshness.value;
  const banner = staleness(boardFreshness, t);
  const host = document.getElementById('stale-banner');
  host.innerHTML = banner ? `<div class="stale">${escapeHtml(banner.text)}</div>` : '';

  const v = verdict(board, t);
  const section = document.getElementById('verdict');
  section.className = `verdict ${v.clear ? 'clear' : 'not-clear'}`;
  section.querySelector('h2').textContent = v.headline;
  section.querySelector('ul').innerHTML = v.clear
    ? ''
    : v.reasons.map((r) => `<li>${escapeHtml(r)}</li>`).join('');

  document.getElementById('tiles').innerHTML = tiles(board, t)
    .map((tile) => `<div class="tile ${escapeHtml(tile.tone)}">
        <div class="n">${tile.value ?? '—'}</div>
        <div class="k">${escapeHtml(tile.label)}</div>
      </div>`)
    .join('');

  const health = healthLine(board?.health, t);
  const healthEl = document.getElementById('health');
  healthEl.className = `health ${health.tone}`;
  healthEl.textContent = health.text;

  document.getElementById('drill-name').textContent = drill?.name || '';
  paintDrillControl(board);
  document.getElementById('elapsed').textContent = board
    ? `${t('board.elapsed')} ${formatDuration(board.elapsed_ms, lang)}` : '';

  paintPriority(board);
  paintTiming();
  paintZones();
  paintExits();
}

function paintDrillControl(board) {
  // The board's own blocking reasons, not a second opinion computed here: a
  // screen that decides for itself whether a building is clear would bypass
  // the outage and sweep checks the server makes.
  const { status, action, blocking } = drillControl(
    board ? { ...drill, all_clear: board.all_clear,
              blocking_all_clear: board.blocking_all_clear } : drill,
    t, { armed: endArmed });

  document.getElementById('drill-status').textContent = status;

  const button = document.getElementById('drill-action');
  button.hidden = action === null;
  if (action === null) return;
  button.textContent = action.label;
  button.className = action.armed ? 'primary' : '';
  button.dataset.kind = action.kind;

  const host = document.getElementById('stale-banner');
  if (blocking.length) {
    host.innerHTML += `<div class="stale">${escapeHtml(t('drill.still_outstanding'))}: `
      + blocking.map(escape).join('; ') + '</div>';
  }
}

document.getElementById('drill-action').addEventListener('click', async () => {
  const kind = document.getElementById('drill-action').dataset.kind;
  if (kind === 'start') {
    await api.startDrill(drillId);
  } else if (!endArmed) {
    // First press arms it and shows what is still outstanding. Ending a drill
    // stops accountability, and the moment somebody reaches for this button is
    // the moment "is everybody out" is actually being answered.
    endArmed = true;
    paint();
    return;
  } else {
    await api.completeDrill(drillId);
  }
  endArmed = false;
  await poll();
});

function paintPriority(board) {
  const host = document.getElementById('priority');
  const rows = orderForWarden((board?.rows || []).filter(
    (r) => r.state !== 'ACCOUNTED'));
  if (!rows.length) {
    host.innerHTML = `<div class="empty">${escapeHtml(t('board.no_priority'))}</div>`;
    return;
  }
  host.innerHTML = rows.map((row) => `
    <div class="row">
      <span class="chip ${escapeHtml(row.colour)}">${escapeHtml(t('state.' + row.state, row.state))}</span>
      <div class="who">
        <div class="name">${escapeHtml(row.display_name)}</div>
        <div class="meta">${escapeHtml(row.department || '')}${
          row.last_zone_id
            ? ` · ${escapeHtml(t('board.last_seen'))} ${escapeHtml(row.last_zone_id)}${
                row.last_camera_id ? ` (${escapeHtml(row.last_camera_id)})` : ''}`
            : ''}</div>
        <div class="reason">${escapeHtml(row.reason)}</div>
      </div>
      <button data-explain="${escapeHtml(row.person_ref)}">?</button>
    </div>`).join('');

  host.querySelectorAll('[data-explain]').forEach((button) => {
    button.addEventListener('click', () => openDrawer(button.dataset.explain));
  });
}

function paintTiming() {
  const line = timingLine(timingFreshness.value, t);
  const host = document.getElementById('timing');
  host.innerHTML = `<div>${escapeHtml(line.text)}</div>`
    + (line.settled ? `<div>${escapeHtml(line.settled)}</div>` : '')
    + line.caveats.map(
      (note) => `<div class="caveat">${escapeHtml(note)}</div>`).join('');
}

function paintZones() {
  const panels = zoneFreshness.value;
  const host = document.getElementById('zones');
  if (!Array.isArray(panels) || !panels.length) {
    host.innerHTML = `<div class="empty">—</div>`;
    return;
  }
  host.innerHTML = panels.map((panel) => `
    <div class="row">
      <span class="chip ${escapeHtml(panel.is_clean ? 'GREEN' : 'YELLOW')}">${
        panel.is_clean ? '✓' : '…'}</span>
      <div class="who">
        <div class="name">${escapeHtml(panel.zone_id)}</div>
        <div class="meta">${panel.confirmed}/${panel.expected} ${
          escapeHtml(t('warden.confirmed'))} · ${panel.outstanding} ${
          escapeHtml(t('warden.outstanding'))}</div>
        ${(panel.blocking || []).map(
          (reason) => `<div class="reason">${escapeHtml(reason)}</div>`).join('')}
      </div>
    </div>`).join('');
}

function paintExits() {
  const { headline, rows, caveats } = exitPressure(exitFreshness.value, t);
  const host = document.getElementById('exits');

  const measured = (value, suffix) => (value === null || value === undefined
    ? `<span class="caveat">${escapeHtml(t('bottleneck.not_measured'))}</span>`
    : `${value.toFixed(1)}${suffix}`);

  host.innerHTML = `<div>${escapeHtml(headline)}</div>`
    + rows.map((row) => `
      <div class="row">
        <span class="chip ${escapeHtml(row.limiting ? 'ORANGE' : 'GREEN')}">${
          row.queue}</span>
        <div class="who">
          <div class="name">${escapeHtml(row.zoneId)}</div>
          <div class="meta">${row.through} ${escapeHtml(t('bottleneck.through'))}
            · ${row.queue} ${escapeHtml(t('bottleneck.queue'))}</div>
          <div class="reason">${escapeHtml(t('bottleneck.dwell'))} ${
            measured(row.medianDwell, 's')}</div>
        </div>
      </div>`).join('')
    + caveats.map(
      (note) => `<div class="caveat">${escapeHtml(note)}</div>`).join('');
}

async function openDrawer(personRef) {
  const host = document.getElementById('drawer-host');
  host.innerHTML = `<div class="scrim"></div><aside class="drawer">
    <h3>${escapeHtml(personRef)}</h3><pre>loading…</pre></aside>`;
  host.querySelector('.scrim').addEventListener('click', () => { host.innerHTML = ''; });

  // The element is captured before the request, not looked up after it. Asking
  // the host for its `pre` on the way back found whichever drawer was open by
  // then: open one person's explanation, click another before it lands, and the
  // first narrative arrives under the second name. An explanation filed against
  // the wrong person is worse than no explanation, because it reads as an
  // answer.
  const body = host.querySelector('pre');
  try {
    const explanation = await api.explain(drillId, personRef);
    if (body.isConnected) body.textContent = explanation.narrative.join('\n');
  } catch (error) {
    if (body.isConnected) body.textContent = `could not load: ${error.message}`;
  }
}

applyLanguage();
poll();
setInterval(poll, 3_000);
