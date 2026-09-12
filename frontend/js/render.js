/**
 * Pure functions that turn API responses into what goes on screen.
 *
 * Separated from the DOM so they can be tested without a browser, and because
 * the decisions they make are decisions rather than markup. Which tiles are
 * shown, what "stale" means, how a mismatch is worded — all of that is
 * behaviour worth asserting.
 */

/**
 * The headline verdict.
 *
 * `all_clear` comes from the server, which combines the board's counts with
 * every zone being swept. This never computes it from counts alone: a screen
 * that decides for itself that six of six accounted means all clear would
 * bypass the outage and sweep checks entirely.
 */
export function verdict(board, t) {
  if (!board) {
    return { clear: false, headline: t('board.not_all_clear'),
             reasons: ['no data has been received yet'] };
  }
  return {
    clear: Boolean(board.all_clear),
    headline: board.all_clear ? t('board.all_clear') : t('board.not_all_clear'),
    reasons: board.blocking_all_clear || [],
  };
}

/** The count tiles, in the order an operator reads them. */
export function tiles(board, t) {
  if (!board) return [];
  return [
    { key: 'expected', tone: '', value: board.expected, label: t('board.expected') },
    { key: 'accounted', tone: 'green', value: board.accounted, label: t('board.accounted') },
    { key: 'evacuating', tone: '', value: board.still_evacuating, label: t('board.evacuating') },
    { key: 'unobserved', tone: 'orange', value: board.currently_unobserved, label: t('board.unobserved') },
    { key: 'uncertain', tone: 'yellow', value: board.uncertain, label: t('board.uncertain') },
    { key: 'unaccounted', tone: 'red', value: board.unaccounted, label: t('board.unaccounted') },
    { key: 'unknown', tone: 'yellow', value: board.unknown_people, label: t('board.unknown') },
  ];
}

/**
 * How the health strip reads.
 *
 * Three states rather than two. "Degraded" and "cannot see" call for different
 * responses: the first means something is wrong, the second means nothing on
 * this screen can be trusted over a warden's own eyes.
 */
export function healthLine(health, t) {
  if (!health) return { tone: 'ok', text: t('health.ok'), caveat: null };
  if (health.blind) {
    return { tone: 'blind', text: t('health.blind'), caveat: health.caveat };
  }
  if (health.degraded) {
    return {
      tone: 'degraded',
      text: `${t('health.degraded')} — ${health.open_outages} ${t('health.outages')}`,
      caveat: health.caveat,
    };
  }
  return { tone: 'ok', text: t('health.ok'), caveat: health.caveat };
}

/**
 * The staleness banner, or null when the picture is current.
 *
 * A live board silently showing a two-minute-old picture is lying by omission.
 */
export function staleness(freshness, t, now = Date.now()) {
  if (freshness.isEmpty) {
    return { level: 'empty', text: 'No data has been received from the server yet.' };
  }
  if (!freshness.isStale(now)) return null;
  const seconds = Math.round((freshness.ageMs(now) || 0) / 1000);
  const because = freshness.lastError ? ` (${freshness.lastError.message})` : '';
  return {
    level: 'stale',
    text: `Showing data from ${seconds}s ago — the server is not responding${because}.`,
  };
}

/**
 * Percentiles, with everything that qualifies them.
 *
 * Plural, because a small sample and a low coverage are different problems. One
 * says the percentile is weak; the other says it describes almost nobody and
 * may have improved by losing the slow people, which is the way this number is
 * most easily gamed. Showing one meant a drill where 92 of 100 people produced
 * no timing said only "too few measurements" and never mentioned the 92.
 *
 * Worded here rather than passed through from the server, because the server's
 * caveats are English prose and this screen is read in Arabic too.
 */
export function timingLine(timing, t) {
  if (!timing || !timing.building) {
    return { text: t('timing.none'), caveats: [], settled: null };
  }
  const b = timing.building;
  const caveats = [];
  if (b.reliable === false) caveats.push(t('timing.unreliable'));
  if (typeof b.coverage === 'number' && b.coverage < 0.9) {
    const missing = Math.round((1 - b.coverage) * 100);
    caveats.push(`${missing}% ${t('timing.coverage_low')}`);
  }

  // Separate from the evacuation percentiles and usually much longer: the
  // building empties in two minutes, and establishing that nobody is left
  // takes as long as the last uncertain person takes to resolve. It is the
  // number that decides when a commander can stand down, and no screen showed
  // it.
  const settled = typeof timing.accountability_completion_s === 'number'
    ? `${t('timing.completion')} ${timing.accountability_completion_s.toFixed(1)}s`
    : null;

  if (b.p95 === null || b.p95 === undefined) {
    return { text: t('timing.none'), caveats, settled };
  }
  const p50 = b.p50 === null ? '—' : `${b.p50.toFixed(1)}s`;
  const p95 = `${b.p95.toFixed(1)}s`;
  return {
    text: `${t('timing.p50')} ${p50} · ${t('timing.p95')} ${p95} · ` +
          `${t('timing.target')} ${timing.target_p95_s}s`,
    meetsTarget: timing.meets_target,
    caveats,
    settled,
  };
}

/**
 * How a headcount result reads on the warden's screen.
 *
 * The two directions get different words because they mean different things,
 * and the dangerous one says plainly what not to do next.
 */
export function headcountVerdict(result, t) {
  if (!result) return null;
  const wording = {
    MATCH: t('warden.match'),
    SYSTEM_OVERCOUNTED: t('warden.mismatch_over'),
    SYSTEM_UNDERCOUNTED: t('warden.mismatch_under'),
  }[result.kind] || result.kind;
  return {
    severity: result.severity,
    headline: wording,
    detail: result.summary,
    advice: result.recommended_action,
    missing: result.missing_from_the_muster_point,
  };
}

/** Filter a zone roster by a search box and a status tab. */
export function filterRoster(rows, { query = '', status = 'ALL' } = {}) {
  const needle = query.trim().toLowerCase();
  return (rows || []).filter((row) => {
    if (status !== 'ALL' && row.state !== status) return false;
    if (!needle) return true;
    return (
      (row.display_name || '').toLowerCase().includes(needle) ||
      (row.person_ref || '').toLowerCase().includes(needle) ||
      (row.department || '').toLowerCase().includes(needle)
    );
  });
}

/**
 * Order a warden's list so the people who need checking come first.
 *
 * Confirmed people sink. That is not cosmetic: a warden working down a list
 * under pressure should not have to scroll past forty ticks to find the two
 * names nobody has laid eyes on.
 */
export function orderForWarden(rows) {
  const band = { RED: 0, YELLOW: 1, ORANGE: 2, GREEN: 3 };
  return [...(rows || [])].sort((a, b) => {
    const byBand = (band[a.colour] ?? 9) - (band[b.colour] ?? 9);
    if (byBand !== 0) return byBand;
    return (a.display_name || '').localeCompare(b.display_name || '');
  });
}

/**
 * HTML-escape a value for interpolation into markup.
 *
 * Named `escapeHtml` and not `escape` because `escape` is a deprecated global
 * that percent-encodes. `warden.js` called it 26 times without defining or
 * importing one, so every name on a warden's tablet was rendered through it:
 * "Ali Khan" became "Ali%20Khan", and an Arabic name became
 * "%u0639%u0644%u064A". It stopped injection and destroyed legibility, on the
 * one screen whose job is letting a warden read names and find people.
 *
 * A distinct name means a bare `escape(` is now always the wrong one, which
 * `tests/structure.test.js` can check.
 */
export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));
}

/**
 * The inline composer a warden types an escalation or a note into.
 *
 * It replaces a `prompt()`, which is the wrong thing twice over on a tablet.
 * It blocks the page, including the timers that drain the offline queue. And a
 * standalone PWA on iOS may refuse to show one at all, in which case it returns
 * null and the old code read that as "cancelled" -- so a warden pressing
 * escalate at an assembly point would have seen nothing happen and believed
 * they had escalated.
 *
 * An escalation with no words is a red flag nobody can act on, so it cannot be
 * sent empty. `sweep.escalate` would have recorded "no reason recorded", and
 * the drill report promises escalations in the warden's own words.
 */
export function composer(kind, text, t) {
  if (!kind) return { open: false, canSend: false };
  const words = (text || '').trim();
  return {
    open: true,
    kind,
    title: kind === 'NOTE' ? t('warden.note') : t('warden.escalate'),
    canSend: words.length > 0,
    hint: words.length > 0 ? null : t('warden.words_needed'),
    text: words,
  };
}

/**
 * What the operator can do to the drill right now, and what to call it.
 *
 * `api.js` has had `startDrill` and `completeDrill` since Phase 4 and nothing
 * called them, so a commander could watch a drill and not start one. The
 * strings were written in both languages and left unused.
 *
 * Ending a drill stops accountability, so it takes two presses: the first arms
 * it and says what is still outstanding, the second does it. Starting does not,
 * because a drill that starts a second too early costs nothing and hesitating
 * at an alarm costs the thing this product is for.
 */
export function drillControl(drill, t, { armed = false } = {}) {
  if (!drill) {
    return { status: t('drill.none'), action: null, blocking: [] };
  }
  const status = t(`drill.status.${drill.status}`, drill.status);

  if (drill.status === 'DRAFT') {
    return { status, action: { kind: 'start', label: t('drill.start') },
             blocking: [] };
  }
  if (drill.status !== 'RUNNING') {
    return { status, action: null, blocking: [] };
  }

  const blocking = drill.all_clear ? [] : (drill.blocking_all_clear || []);
  return {
    status,
    action: {
      kind: 'complete',
      label: armed ? t('drill.complete_confirm') : t('drill.complete'),
      armed,
    },
    // Shown only once the operator has reached for the button, because it is
    // the moment the question "is everybody out" is actually being answered.
    blocking: armed ? blocking : [],
  };
}

/**
 * Which exit is holding the evacuation up.
 *
 * The backend has measured this since Phase 3 and no screen showed it, so the
 * answer to "where is the queue" existed only in an HTTP response nobody
 * fetched. Section 7 of the validation document leans on it.
 *
 * A field that was not measured stays null all the way to the screen. Density
 * in particular: it needs a capacity most sites never record, and a crowding
 * figure against an invented denominator is worse than none.
 */
export function exitPressure(bottlenecks, t) {
  const exits = bottlenecks && Array.isArray(bottlenecks.exits)
    ? bottlenecks.exits : [];
  if (!exits.length) {
    return { headline: t('bottleneck.none'), rows: [], caveats: [] };
  }

  const limiting = bottlenecks.limiting_zone_id || null;
  const rows = exits
    .map((exit) => ({
      zoneId: exit.zone_id,
      limiting: exit.zone_id === limiting,
      through: exit.completed,
      queue: exit.queue,
      throughput: exit.throughput_per_min ?? null,
      medianDwell: exit.median_dwell_s ?? null,
      density: exit.density ?? null,
    }))
    // Worst first, the same reason a warden's list puts the unchecked on top:
    // an operator reading under pressure should not have to scan for it.
    .sort((a, b) => (Number(b.limiting) - Number(a.limiting))
      || (b.queue - a.queue)
      || a.zoneId.localeCompare(b.zoneId));

  return {
    headline: limiting
      ? `${t('bottleneck.limiting')}: ${limiting}`
      : t('bottleneck.none_limiting'),
    rows,
    caveats: bottlenecks.caveats || [],
  };
}

/** What the sync indicator says. */
export function syncStatus({ online, pending, stalenessMs, fromCache }, t) {
  if (fromCache) {
    // Distinct from being offline: the device may have signal and still be
    // reading a roster the service worker remembered, which is the case a
    // warden is least likely to guess at.
    return { tone: 'offline', text: `${t('warden.remembered')} · ${pending} ${t('warden.pending')}` };
  }
  if (!online) {
    return { tone: 'offline', text: `${t('warden.offline')} · ${pending} ${t('warden.pending')}` };
  }
  if (pending > 0) {
    const seconds = Math.round((stalenessMs || 0) / 1000);
    return { tone: 'offline', text: `${pending} ${t('warden.pending')} (${seconds}s)` };
  }
  return { tone: 'online', text: t('warden.synced') };
}

/**
 * What a zone panel says about its warden's tablet.
 *
 * Three states, and the middle one is why this exists. A zone with a warden
 * still walking it and a zone whose warden has gone out of range look
 * identical on the board -- neither is swept -- and during an evacuation one
 * means wait and the other means send somebody.
 *
 * The threshold is here rather than on the server because how long a tablet
 * may be quiet before somebody walks over to it is an operational decision a
 * site makes, not a number derived from anything. The server reports the
 * duration and keeps the fact; this decides when to say it out loud.
 */
export const WARDEN_SILENCE_MS = 90_000;

export function wardenContact(panel, t, { silenceMs = WARDEN_SILENCE_MS } = {}) {
  const silent = panel ? panel.warden_silent_ms : null;
  if (silent === null || silent === undefined) {
    return { tone: 'unheard', text: t('warden.never_connected') };
  }
  if (silent < silenceMs) return null;
  const seconds = Math.round(silent / 1000);
  return { tone: 'silent', text: `${t('warden.silent_for')} ${seconds}s` };
}
