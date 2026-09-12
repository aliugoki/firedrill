/**
 * The warden PWA.
 *
 * Runs on a tablet at an assembly point, on a device that may lose signal the
 * moment the alarm sounds. Three properties drive everything here.
 *
 * **Every action is durable before the screen acknowledges it.** The tick
 * appears after IndexedDB has the row, not before. A warden who taps confirm
 * and sees it register has evidence that survives the tab being closed.
 *
 * **The system's health is on the warden's screen too.** When the cameras
 * cannot see, the warden needs to know that the list in front of them is a
 * guess, and to rely on their own count instead.
 *
 * **Sync is never silent.** Pending work, its age, and anything the server
 * refused are all visible. A warden must never discover at the end of a drill
 * that half their confirmations never landed.
 */

import { Api, ApiError, Freshness } from './api.js';
import { createTranslator, isRtl } from './i18n.js';
import {
  composer, escapeHtml, filterRoster, headcountVerdict, healthLine,
  needsAHuman, orderForWarden, syncStatus,
} from './render.js';
import { OfflineQueue, reconcileSync } from './queue.js';

const params = new URLSearchParams(location.search);
let lang = params.get('lang') || localStorage.getItem('evac.lang') || 'en';
let t = createTranslator(lang);

const wardenId = params.get('warden') || localStorage.getItem('evac.warden') || 'warden-7';
const zoneId = params.get('zone') || localStorage.getItem('evac.zone') || 'assembly-north';
const drillId = params.get('drill') || localStorage.getItem('evac.drill') || '';
localStorage.setItem('evac.warden', wardenId);
localStorage.setItem('evac.zone', zoneId);

let deviceId = localStorage.getItem('evac.device');
if (!deviceId) {
  deviceId = `device-${crypto.randomUUID().slice(0, 8)}`;
  localStorage.setItem('evac.device', deviceId);
}

const api = new Api().withIdentity({
  userId: wardenId, permissions: ['evac:read', 'evac:warden'], zones: [zoneId],
});
const queue = new OfflineQueue(deviceId, { isOnline: () => navigator.onLine });
const zoneFreshness = new Freshness(15_000);

let screen = 'zone';
let lastHeadcount = null;
//: Which composer is open, if any: 'ESCALATE', 'NOTE', or null.
let composing = null;
let refusals = [];

// --- language ---------------------------------------------------------------

function applyLanguage() {
  t = createTranslator(lang);
  document.documentElement.lang = lang;
  document.documentElement.dir = isRtl(lang) ? 'rtl' : 'ltr';
  document.getElementById('safety').textContent = t('app.safety_notice');
  document.getElementById('zone-title').textContent = `${t('warden.my_zone')} · ${zoneId}`;
  document.getElementById('lang').textContent = lang === 'en' ? 'العربية' : 'English';
  document.getElementById('submit-count').textContent = t('warden.headcount_submit');
  document.getElementById('headcount').placeholder = t('warden.headcount_prompt');
  document.getElementById('sweep').textContent = t('warden.sweep');
  document.getElementById('escalate').textContent = t('warden.escalate');
  document.getElementById('note').textContent = t('warden.note');
  document.getElementById('composer-send').textContent = t('warden.send');
  document.getElementById('composer-cancel').textContent = t('warden.cancel');
  document.getElementById('search').placeholder = t('warden.search');
  // Read by a screen reader, so they are strings like any other. They were
  // hard-coded English in the markup, which is invisible until somebody using
  // assistive technology in Arabic reaches them.
  document.getElementById('headcount').setAttribute(
    'aria-label', t('warden.headcount'));
  document.getElementById('search').setAttribute(
    'aria-label', t('warden.search'));
  paintTabs();
}

document.getElementById('lang').addEventListener('click', () => {
  lang = lang === 'en' ? 'ar' : 'en';
  localStorage.setItem('evac.lang', lang);
  applyLanguage();
  paint();
});

// --- actions ----------------------------------------------------------------

/**
 * Store, then acknowledge, then try to send.
 *
 * The order is the design. Sending first and storing on failure loses the
 * action if the tab dies mid-request, and a lost confirmation is the highest
 * trust evidence in the system disappearing without trace.
 */
async function act(kind, extra = {}) {
  await queue.enqueue({
    kind, warden_id: wardenId, zone_id: zoneId, ts_ms: Date.now(), ...extra,
  });
  await paint();
  sync();
}

async function sync() {
  const pending = await queue.pending();
  if (!pending.length || !navigator.onLine) { await paint(); return; }
  try {
    const response = await api.wardenSync(drillId, pending.map(toWire));
    const outcome = reconcileSync(pending, response);
    await queue.acknowledge(outcome.acknowledged);
    refusals = outcome.refusalMessages;
    await refreshZone();
  } catch (error) {
    // Stays queued. Nothing is dropped and nothing is retried out of order.
    if (!(error instanceof ApiError)) throw error;
  }
  await paint();
}

function toWire(row) {
  return {
    kind: row.kind, warden_id: row.warden_id, device_id: row.device_id,
    zone_id: row.zone_id, ts_ms: row.ts_ms, device_seq: row.device_seq,
    subject: row.subject ?? null, identity: row.identity ?? null,
    note: row.note ?? null, queued_offline: Boolean(row.queued_offline),
  };
}

async function refreshZone() {
  try {
    zoneFreshness.succeed(await api.wardenZone(drillId, zoneId, deviceId));
  } catch (error) {
    zoneFreshness.fail(error);
  }
}

// --- screens ----------------------------------------------------------------

function paintTabs() {
  const tabs = [
    ['zone', t('warden.my_zone')],
    ['roster', t('warden.roster')],
    ['unknown', t('warden.unknown')],
  ];
  const host = document.getElementById('tabs');
  host.innerHTML = tabs.map(([key, label]) =>
    `<button role="tab" data-screen="${escapeHtml(key)}" aria-selected="${escapeHtml(screen === key)}">${
      escapeHtml(label)}</button>`).join('');
  host.querySelectorAll('[data-screen]').forEach((button) => {
    button.addEventListener('click', () => {
      screen = button.dataset.screen;
      paint();
    });
  });
}

async function paint() {
  paintTabs();
  const zone = zoneFreshness.value;

  const status = syncStatus({
    fromCache: zoneFreshness.fromCache,
    online: navigator.onLine,
    pending: await queue.depth(),
    stalenessMs: await queue.stalenessMs(),
  }, t);
  const syncEl = document.getElementById('sync');
  syncEl.className = `warden-status ${status.tone}`;
  syncEl.textContent = status.text +
    (refusals.length ? ` · ${refusals.length} refused` : '');

  const health = healthLine(zone?.system_health, t);
  const healthEl = document.getElementById('system-health');
  healthEl.innerHTML = health.tone === 'ok'
    ? ''
    : `<div class="warden-status ${escapeHtml(health.tone === 'blind' ? 'blind' : 'offline')}">${
        escapeHtml(health.tone === 'blind' ? t('warden.rely_on_count') : health.text)}</div>`;

  document.getElementById('screen-zone').hidden = screen !== 'zone';
  document.getElementById('screen-roster').hidden = screen !== 'roster';
  document.getElementById('screen-unknown').hidden = screen !== 'unknown';

  if (screen === 'zone') paintZone(zone);
  if (screen === 'roster') paintRoster(zone);
  if (screen === 'unknown') paintUnknown(zone);
}

function paintZone(zone) {
  const panel = zone?.panel;
  document.getElementById('zone-tiles').innerHTML = panel ? `
    <div class="tile"><div class="n">${panel.expected}</div>
      <div class="k">${escapeHtml(t('board.expected'))}</div></div>
    <div class="tile green"><div class="n">${panel.confirmed}</div>
      <div class="k">${escapeHtml(t('warden.confirmed'))}</div></div>
    <div class="tile yellow"><div class="n">${panel.outstanding}</div>
      <div class="k">${escapeHtml(t('warden.outstanding'))}</div></div>` : '';

  const verdictHost = document.getElementById('mismatch');
  const result = headcountVerdict(lastHeadcount, t);
  verdictHost.innerHTML = result ? `
    <div class="mismatch ${escapeHtml(result.severity)}">
      <div>${escapeHtml(result.headline)}</div>
      <div class="advice">${escapeHtml(result.detail)}</div>
      <div class="advice">${escapeHtml(result.advice)}</div>
    </div>` : '';

  document.getElementById('sweep').textContent =
    panel?.status === 'COMPLETE' ? t('warden.sweep_done') : t('warden.sweep');
}

function paintRoster(zone) {
  const query = document.getElementById('search').value;
  const rows = orderForWarden(filterRoster(zone?.roster || [], { query }));
  const host = document.getElementById('roster');
  if (!rows.length) { host.innerHTML = `<div class="empty">—</div>`; return; }

  host.innerHTML = rows.map((row) => `
    <div class="row" style="display:block">
      <div style="display:flex;align-items:center;gap:10px">
        <span class="chip ${escapeHtml(row.colour)}">${escapeHtml(t('state.' + row.state, row.state))}</span>
        <div class="who">
          <div class="name">${escapeHtml(row.display_name)}</div>
          <div class="meta">${escapeHtml(row.department || '')}</div>
          <div class="reason">${escapeHtml(row.reason)}</div>
          ${(() => {
            const mark = needsAHuman(row, t);
            return mark ? `<div class="reason ${escapeHtml(mark.tone)}">${
              escapeHtml(mark.text)}</div>` : '';
          })()}
        </div>
      </div>
      <div class="person-actions">
        <button data-act="CONFIRM_PRESENT" data-ref="${escapeHtml(row.person_ref)}">${
          escapeHtml(t('warden.confirm'))}</button>
        <button data-act="NOT_HERE" data-ref="${escapeHtml(row.person_ref)}">${
          escapeHtml(t('warden.not_here'))}</button>
        ${row.person_ref.startsWith('emp:')
          ? `<button data-act="WRONG_PERSON" data-ref="${escapeHtml(row.person_ref)}">${
              escapeHtml(t('warden.wrong_person'))}</button>`
          : ''}
        <button data-act="MARK_ABSENT" data-ref="${escapeHtml(row.person_ref)}">${
          escapeHtml(t('warden.mark_absent'))}</button>
      </div>
    </div>`).join('');

  host.querySelectorAll('[data-act]').forEach((button) => {
    button.addEventListener('click', () => {
      const kind = button.dataset.act;
      const ref = button.dataset.ref;
      const extra = { subject: ref };
      // Rejecting an identity must name the one being rejected, or there is
      // nothing for the system to stop believing -- and it must be the gallery
      // identity the matcher proposed, `EMP-0001`, not the roster reference,
      // `emp:EMP-0001`. Sending the reference was truthy enough to pass
      // validation and then matched no candidate the matcher had ever
      // proposed, so the rejection was recorded and changed nothing. The
      // button is only offered on an employee row, because a visitor has no
      // gallery identity to reject.
      if (kind === 'WRONG_PERSON') extra.identity = ref.replace(/^emp:/, '');
      act(kind, extra);
    });
  });
}

function paintUnknown(zone) {
  const count = zone?.panel?.unknown_tagged ?? 0;
  document.getElementById('unknown-body').innerHTML = `
    <div class="tile yellow"><div class="n">${count}</div>
      <div class="k">${escapeHtml(t('board.unknown'))}</div></div>
    <div class="person-actions">
      <button data-tag="visitor">${escapeHtml(t('warden.tag_visitor'))}</button>
      <button data-tag="contractor">${escapeHtml(t('warden.tag_contractor'))}</button>
    </div>`;
  document.querySelectorAll('[data-tag]').forEach((button) => {
    button.addEventListener('click', () =>
      act('TAG_UNKNOWN', { note: button.dataset.tag }));
  });
}

// --- wiring -----------------------------------------------------------------

document.getElementById('submit-count').addEventListener('click', async () => {
  const input = document.getElementById('headcount');
  const physical = Number(input.value);
  if (!Number.isFinite(physical) || physical < 0) return;
  try {
    lastHeadcount = await api.headcount(drillId, {
      zone_id: zoneId, warden_id: wardenId, device_id: deviceId,
      ts_ms: Date.now(), physical_count: physical,
    });
  } catch (error) {
    lastHeadcount = null;
  }
  await paint();
});

document.getElementById('sweep').addEventListener('click', () =>
  act('SWEEP_COMPLETE'));
function paintComposer() {
  const text = document.getElementById('composer-text');
  const state = composer(composing, text.value, t);

  document.getElementById('composer').hidden = !state.open;
  if (!state.open) return;
  document.getElementById('composer-hint').textContent = state.hint || '';
  document.getElementById('composer-send').disabled = !state.canSend;
}

function openComposer(kind) {
  composing = kind;
  const text = document.getElementById('composer-text');
  text.value = '';
  paintComposer();
  text.focus();
}

document.getElementById('escalate').addEventListener(
  'click', () => openComposer('ESCALATE'));
document.getElementById('note').addEventListener(
  'click', () => openComposer('NOTE'));
document.getElementById('composer-text').addEventListener(
  'input', paintComposer);
document.getElementById('composer-cancel').addEventListener('click', () => {
  composing = null;
  paintComposer();
});
document.getElementById('composer-send').addEventListener('click', () => {
  const text = document.getElementById('composer-text');
  const state = composer(composing, text.value, t);
  if (!state.canSend) return;
  act(state.kind, { note: state.text });
  composing = null;
  text.value = '';
  paintComposer();
});
document.getElementById('search').addEventListener('input', () => paint());

window.addEventListener('online', () => sync());
window.addEventListener('offline', () => paint());

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/static/sw.js', { scope: '/evac/' })
    .catch(() => { /* the app still works; it just will not start offline */ });
}

applyLanguage();
refreshZone().then(paint);
setInterval(() => { refreshZone().then(paint); }, 5_000);
setInterval(sync, 10_000);
