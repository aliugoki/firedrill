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
import { Settings } from './queue.js';
import { createTranslator, isRtl, formatDuration } from './i18n.js';
import {
  blockingReasons, drillControl, escapeHtml, explainSummary, exitPressure,
  healthLine, describeReason, orderForWarden, outstanding, rowNotes, staleness,
  tiles, timingLine, trend, verdict, whereToLook,
  wardenContact,
} from './render.js';

// Guarded. A browser with site data blocked throws on the `localStorage`
// getter itself, and this line ran at module scope -- so the command centre
// rendered nothing at all, which during an evacuation looks exactly like a
// building that has emptied. See `Settings` in `queue.js`.
const settings = new Settings();

const params = new URLSearchParams(location.search);
let lang = params.get('lang') || settings.get('evac.lang') || 'en';
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
//: The last few minutes of "still to account for", so the screen can say
//: whether the number is falling. Bounded: this runs for the length of a drill
//: on a machine nobody restarts, and an unbounded array in a page that polls
//: every three seconds is a leak with a stopwatch on it.
const outstandingHistory = [];
const HISTORY_LIMIT = 200;

function applyLanguage() {
  t = createTranslator(lang);
  document.documentElement.lang = lang;
  document.documentElement.dir = isRtl(lang) ? 'rtl' : 'ltr';
  document.getElementById('safety').textContent = t('app.safety_notice');
  document.getElementById('title').textContent = t('app.title');
  document.getElementById('priority-title').textContent = t('board.priority');
  document.getElementById('search-title').textContent = t('board.search');
  document.getElementById('timing-title').textContent = t('board.timing');
  document.getElementById('zones-title').textContent = t('board.zones');
  document.getElementById('exits-title').textContent = t('bottleneck.title');
  document.getElementById('lang').textContent = lang === 'en' ? 'العربية' : 'English';
}

document.getElementById('lang').addEventListener('click', () => {
  lang = lang === 'en' ? 'ar' : 'en';
  settings.set('evac.lang', lang);
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

  paintHeadline(board);

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
  paintSearch(board);
  paintTiming();
  paintZones();
  paintExits();
}

function paintHeadline(board) {
  const host = document.getElementById('headline');
  const state = outstanding(board);
  if (!state) { host.innerHTML = ''; return; }

  if (board?.now_ms) {
    outstandingHistory.push({ ts: board.now_ms, remaining: state.remaining });
    if (outstandingHistory.length > HISTORY_LIMIT) outstandingHistory.shift();
  }
  const movement = trend(outstandingHistory, board?.now_ms ?? Date.now());

  const tone = state.remaining === 0 ? 'green' : 'red';
  const percent = Math.round(state.fraction * 100);
  const movementLine = movement
    ? `<div class="headline-trend ${escapeHtml(movement.direction)}">${
        escapeHtml(t(`board.trend_${movement.direction}`))} ${
        movement.direction === 'flat' ? '' : Math.abs(movement.delta)} ${
        escapeHtml(t('board.trend_over'))} ${movement.seconds}s</div>`
    : '';

  host.innerHTML = `
    <div class="headline-figure ${escapeHtml(tone)}">
      <div class="headline-n">${state.remaining}</div>
      <div class="headline-k">${escapeHtml(t('board.still_to_account'))}</div>
      ${movementLine}
    </div>
    <div class="headline-progress">
      <div class="headline-bar"><span></span></div>
      <div class="headline-meta">${state.accounted} / ${state.expected} ${
        escapeHtml(t('board.accounted'))}</div>
    </div>`;

  // Set after rendering rather than as an attribute hole. The structure test
  // refuses an unescaped `${}` inside a quoted attribute and does not make an
  // exception for a number somebody computed, which is the point of it: an
  // exception is a judgement call, and the next one will be made in a hurry.
  host.querySelector('.headline-bar span').style.width = `${percent}%`;
}

function paintSearch(board) {
  const host = document.getElementById('search');
  // The assembly zones as the server lists them, so a person standing in the
  // car park is not put on the same list as one last seen on floor 3. Falls
  // back to the roster's own assignments inside `whereToLook` when the zone
  // request has not landed or has failed.
  const panels = zoneFreshness.value;
  const groups = whereToLook(board, t, Array.isArray(panels)
    ? panels.map((panel) => panel.zone_id) : null);
  if (!groups.length) {
    host.innerHTML = `<div class="empty">${escapeHtml(t('board.nowhere_to_look'))}</div>`;
    return;
  }
  host.innerHTML = groups.map((group) => `
    <div class="row ${escapeHtml(group.atAssembly ? 'at-assembly' : '')}">
      <span class="chip ${escapeHtml(
        group.atAssembly ? 'YELLOW' : (group.unaccounted ? 'RED' : 'YELLOW'))}">${
        group.count}</span>
      <div class="who">
        <div class="name">${escapeHtml(group.label)}</div>
        <div class="advice">${escapeHtml(group.advice)}</div>
        <div class="meta">${escapeHtml(group.names.join(', '))}${
          group.count > group.names.length
            ? ` +${group.count - group.names.length}` : ''}</div>
        ${group.unaccounted && !group.atAssembly
          ? `<div class="reason red">${group.unaccounted} ${
              escapeHtml(t('board.unaccounted'))}</div>`
          : ''}
      </div>
    </div>`).join('');
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
      + blocking.map(escapeHtml).join('; ') + '</div>';
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
        <div class="reason">${escapeHtml(describeReason(row, t))}</div>
        ${rowNotes(row, t).map((note) => `<div class="reason ${
          escapeHtml(note.tone)}">${escapeHtml(note.text)}</div>`).join('')}
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
        ${blockingReasons(panel, t).map(
          (reason) => `<div class="reason">${escapeHtml(reason)}</div>`).join('')}
        ${(() => {
          const contact = wardenContact(panel, t);
          return contact
            ? `<div class="reason ${escapeHtml(contact.tone)}">${
                escapeHtml(contact.text)}</div>`
            : '';
        })()}
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
    if (!body.isConnected) return;
    // Above the narrative, not inside it. A reader scanning thirty lines of
    // observations should not have to find the disagreement themselves.
    const { disputes, blindness } = explainSummary(explanation, t);
    const banners = [...disputes, ...blindness].map(
      (item) => `<div class="reason ${escapeHtml(item.tone)}">${
        escapeHtml(item.text)}</div>`).join('');
    if (banners) body.insertAdjacentHTML('beforebegin', banners);
    body.textContent = explanation.narrative.join('\n');
  } catch (error) {
    if (body.isConnected) body.textContent = `could not load: ${error.message}`;
  }
}

applyLanguage();
poll();
setInterval(poll, 3_000);
