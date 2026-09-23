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
             reasons: [t('board.no_data_yet')] };
  }
  return {
    clear: Boolean(board.all_clear),
    headline: board.all_clear ? t('board.all_clear') : t('board.not_all_clear'),
    // Worded here from the codes. `blocking_all_clear` is the server's own
    // English and stays as the fallback for an edge node that sends no codes.
    reasons: blockingReasons(board, t),
  };
}

/**
 * Every refusal the board carries, worded, with the server's prose as a
 * fallback per entry rather than for the whole list -- a code added in a
 * version this device has not been updated to must not take the other
 * refusals down with it.
 */
export function blockingReasons(board, t) {
  // Either shape. The board calls its prose `blocking_all_clear` and a zone
  // panel calls its own `blocking`; both carry the same `blockers` codes
  // alongside, and a second function to word the second list is how the two
  // drift apart.
  const sentences = board?.blocking_all_clear || board?.blocking || [];
  const coded = board?.blockers;
  if (!Array.isArray(coded) || !coded.length) return sentences;
  return coded.map((blocker, index) => describeBlocker(
    { ...blocker, text: sentences[index] }, t));
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
  // First, and it displaces the sample-size line rather than joining it. A
  // clock correction sets `reliable: false` too, and left to itself the board
  // read "too few measurements" on a drill with a hundred of them -- which
  // sends a reader to look for more people when the problem is that a duration
  // between two clock readings taken either side of a correction is not a
  // duration.
  const moved = b.clock_corrections || [];
  for (const correction of moved) {
    const seconds = Math.round(Math.abs(correction.delta_ms) / 1000);
    caveats.push(`${t('timing.clock_moved')} (${seconds}s)`);
  }
  if (b.reliable === false && !moved.length) caveats.push(t('timing.unreliable'));
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
    // Within a band, the people no camera will ever settle. They have no
    // gallery entry or they need help moving, so waiting changes nothing for
    // them and the warden is the only way they get accounted for.
    const byHuman = Number(Boolean(b.needs_human_to_account))
      - Number(Boolean(a.needs_human_to_account));
    if (byHuman !== 0) return byHuman;
    return (a.display_name || '').localeCompare(b.display_name || '');
  });
}

/**
 * The mark on a person the cameras cannot settle, or null.
 *
 * `needs_human_to_account` reached the API on every row and no screen read it.
 * `roster.py` says it should be surfaced at drill start rather than discovered
 * at minute four while an operator waits for a match that can never arrive --
 * and the flag travelled the whole way to the browser and stopped.
 *
 * Only while they are unaccounted for. Once a warden has confirmed them the
 * fact has done its work, and a badge on a settled row is clutter on a screen
 * that cannot afford any.
 */
export function needsAHuman(row, t) {
  if (!row || !row.needs_human_to_account) return null;
  if (row.state === 'ACCOUNTED') return null;
  return { tone: 'yellow', text: t('board.needs_human') };
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

  const blocking = drill.all_clear ? [] : blockingReasons(drill, t);
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
    caveats: describeCaveats(bottlenecks, t),
  };
}

/**
 * Exit caveats, worded here rather than passed through as the server's
 * English.
 *
 * A caveat is the line that stops a reader taking a number as more solid than
 * it is, which makes it exactly the line that must not arrive in a language
 * they do not read. Per entry rather than for the whole list: a code added in
 * a version this device has not been updated to must not take the other
 * caveats down with it.
 */
export function describeCaveats(panel, t) {
  const sentences = panel?.caveats || [];
  const coded = panel?.caveat_codes;
  if (!Array.isArray(coded) || !coded.length) return sentences;
  return coded.map((caveat, index) => {
    const key = `caveat.${caveat.code}`;
    const worded = t(key);
    if (worded === key) return sentences[index] ?? key;
    const detail = caveat.detail || {};
    if (caveat.code === 'NO_CAPACITY') {
      return `${worded}: ${(detail.zones || []).join(', ')}`;
    }
    if (caveat.code === 'SHORT_WINDOW') {
      return `${worded} (${detail.seconds ?? 30}s)`;
    }
    return worded;
  });
}

/** What the sync indicator says. */
export function syncStatus({ online, pending, stalenessMs, fromCache,
                             storageFailed = false,
                             settingsFailed = false }, t) {
  if (storageFailed) {
    // First, and in the blind tone, because it outranks everything else this
    // strip can say. A warden whose device cannot write to IndexedDB -- a
    // tablet in private browsing, one with site data blocked, one out of quota
    // -- taps confirm and nothing happens. `queue.js` says of that exact
    // shape: a warden getting no response at all is worse than one told it
    // failed, because the first looks like the app is thinking and they move
    // on. The queue was built to reject rather than hang and the screen
    // dropped the rejection, which produced the silence anyway.
    return { tone: 'blind', text: t('warden.cannot_save') };
  }
  if (settingsFailed) {
    // Ranked under the one above and over everything below it. Confirmations
    // still reach IndexedDB, so this is not "your work is being lost"; what is
    // lost is the device's own identity, and with it the sequence numbers that
    // let the server tell an action that vanished from one that was late. A
    // warden cannot fix that, but the person reading the board can stop
    // trusting this device's gaps, and nothing said it.
    return { tone: 'offline', text: t('warden.cannot_remember') };
  }
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

/**
 * The two things the explain drawer showed a reader only by implication.
 *
 * `is_disputed` was a boolean: the drawer could say the system cannot settle
 * this person's identity without saying between whom, and naming the competing
 * claims is the whole of what invariant 3 asks for. A conflict is reported and
 * never adjudicated, and reporting it means saying what it is.
 *
 * Blindness arrived inside `context`, indistinguishable from an ordinary
 * observation. "The camera covering their floor was down for two minutes" is
 * the answer to the question a warden actually asks about somebody unaccounted
 * for, and it was one line among thirty.
 */
export function explainSummary(explanation, t) {
  if (!explanation) return { disputes: [], blindness: [] };
  const disputes = (explanation.disputes || []).map((dispute) => ({
    tone: 'yellow',
    text: `${t('explain.claimed_as')} ${(dispute.identities || []).join(' / ')}`,
  }));
  const blindness = (explanation.blindness || []).map((item) => ({
    tone: 'orange',
    text: item.summary,
  }));
  return { disputes, blindness };
}

/**
 * Why the system lost sight of somebody, as opposed to the fact that it did.
 *
 * A row that says "not seen for 90 seconds" invites the reading that the
 * person moved. Often the person did not move and the camera stopped working,
 * and those call for opposite responses: one is a search, the other is a
 * caveat on the board. `health.py` names this the question that matters at the
 * assembly point and the answer reached no screen.
 *
 * Not shown for somebody already accounted for. The system lost sight of them
 * and then found them, and the reason no longer changes what anyone does.
 */
export function lostSightBecause(row, t) {
  if (!row || row.state === 'ACCOUNTED') return null;
  const cameras = row.blinded_by || [];
  if (!cameras.length) return null;
  return {
    tone: 'orange',
    text: `${t('board.blinded_by')} ${cameras.join(', ')}`,
  };
}

/**
 * Every note that belongs under a person's row, in the order they matter.
 *
 * One function rather than one per note, because each screen renders them in
 * the same place and a second copy of the loop is where the two screens start
 * to disagree about what a warden sees and what a commander sees.
 */
export function rowNotes(row, t) {
  return [needsAHuman(row, t), lostSightBecause(row, t)].filter(Boolean);
}

/**
 * What a warden needs to read after a sync, rather than a count of it.
 *
 * The sync route says a device "that syncs forty actions and has one refused
 * must be able to tell which, or a warden's screen shows work that never
 * landed". The server said which, `reconcileSync` turned it into sentences,
 * and the screen printed "· 1 refused" and dropped them.
 *
 * The two kinds are not the same and must not read as one number. A refusal is
 * final: that action is not in the system and will not be, and the warden has
 * to do something about it. An unanswered action is still queued and goes
 * again on the next sync, so it is worth showing only because a queue that
 * never empties means something is wrong that nobody else will notice.
 */
export function syncProblems({ refusals = [], unanswered = [] }, t) {
  const lines = refusals.map((text) => ({ tone: 'red', text }));
  if (unanswered.length) {
    lines.push({
      tone: 'yellow',
      text: `${unanswered.length} ${t('warden.no_answer')}`,
    });
  }
  return lines;
}

/**
 * A person's reason, worded in the language being read.
 *
 * The server sends `reason` as English prose and `reason_code` plus the values
 * the wording needs. Prose is right for the post-drill report, which is a
 * document; a screen is not a document. This line is read in Arabic on a
 * tablet at an assembly point and it is what tells a warden what to do about
 * the person in front of them.
 *
 * Falls back to the server's prose when the code is missing or unknown -- an
 * older edge node, or a code added in a version this device has not been
 * updated to. An English sentence under somebody's name beats a blank line.
 */
export function describeReason(row, t) {
  if (!row) return '';
  const code = row.reason_code;
  if (!code) return row.reason || '';

  const detail = row.reason_detail || {};
  const key = code === 'ASSEMBLY_WITH_IDENTITY' && detail.face_visible === false
    ? 'reason.ASSEMBLY_WITH_IDENTITY_STALE'
    : `reason.${code}`;
  const headline = t(key);
  // `t` renders the key itself when a string is missing, which is deliberately
  // ugly in testing and useless on a tablet. The server's own sentence is a
  // better thing to show than `reason.TRACK_LOST`.
  if (headline === key) return row.reason || '';

  const parts = [headline];
  if (detail.warden_id) parts.push(detail.warden_id);
  if (detail.zone_id && code === 'WARDEN_CONFIRMED_AT_ASSEMBLY') {
    parts.push(`· ${detail.zone_id}`);
  }
  if (detail.why) parts.push(`· ${detail.why}`);
  if (typeof detail.seconds === 'number') {
    parts.push(`· ${detail.seconds} ${t('reason.seconds_in')}`);
  }
  if (detail.zone_id && code !== 'WARDEN_CONFIRMED_AT_ASSEMBLY') {
    parts.push(`· ${t('reason.last_seen')} ${detail.zone_id}`);
    if (detail.camera_id) parts.push(`${t('reason.on_camera')} ${detail.camera_id}`);
  }
  if (Array.isArray(detail.identities) && detail.identities.length) {
    parts.push(`· ${detail.identities.join(' / ')}`);
  }
  return parts.join(' ');
}

/**
 * A refusal to show ALL CLEAR, worded in the language being read.
 *
 * The same arrangement as `describeReason` and for the same reason: this list
 * is what a commander reads before deciding whether to keep two hundred people
 * standing outside, and it was English prose on a screen used in Arabic.
 *
 * Zone-scoped refusals lead with the zone, because the command centre shows
 * every zone at once and "the sweep is still in progress" without one is a
 * sentence nobody can act on.
 */
export function describeBlocker(blocker, t) {
  if (!blocker) return '';
  if (typeof blocker === 'string') return blocker;   // an older edge node
  const { code, detail = {} } = blocker;
  const words = t(`blocker.${code}`);
  if (words === `blocker.${code}`) return blocker.text || '';

  const zone = detail.zone_id ? `${detail.zone_id}: ` : '';
  switch (code) {
    case 'PEOPLE_UNACCOUNTED':
      return `${detail.outstanding} ${t('blocker.of')} ${detail.expected} `
        + `${t('blocker.people')} ${words}`;
    case 'OPEN_OUTAGES':
      return `${detail.count} ${words}`;
    case 'UNASSIGNED_PEOPLE':
      return `${detail.count} ${t('blocker.people')} ${words}`;
    case 'ZONE_UNCONFIRMED':
      return `${zone}${detail.count} ${t('blocker.people')} ${words}`;
    case 'SWEEP_ESCALATED':
      return `${zone}${words}: ${detail.reason ?? ''}`;
    case 'HEADCOUNT_MISMATCH':
      // The numbers rather than the server's sentence about them: the sentence
      // is English and the numbers are not.
      return `${zone}${words} (${detail.physical} / ${detail.system})`;
    default:
      return `${zone}${words}`;
  }
}

/**
 * The one number a commander is actually working toward: zero.
 *
 * It was on none of the seven tiles. "Expected 212" and "accounted for 176"
 * were both there and the difference was not, so the number that has to reach
 * zero was something you worked out in your head, twice a minute, under
 * pressure. The verdict said it in prose; prose is not a number you can watch
 * fall.
 */
export function outstanding(board) {
  if (!board) return null;
  const expected = board.expected ?? 0;
  const accounted = board.accounted ?? 0;
  const remaining = Math.max(0, expected - accounted);
  return {
    remaining,
    expected,
    accounted,
    fraction: expected ? accounted / expected : 0,
  };
}

/**
 * Whether it is getting better, from the board's own history.
 *
 * Twenty people outstanding at minute five means two different things: twenty
 * down from sixty, which is an evacuation working, or twenty stuck at twenty
 * for three minutes, which is a search that has not started. The screen showed
 * the same "20" for both.
 *
 * Says nothing until there is enough history to mean anything. A trend drawn
 * from two samples ten seconds apart is noise presented as a direction, and a
 * commander acting on noise is worse off than one acting on nothing.
 */
export const TREND_WINDOW_MS = 60_000;
export const TREND_MINIMUM_MS = 25_000;

export function trend(history, now = Date.now()) {
  const samples = (history || []).filter(
    (s) => s && typeof s.remaining === 'number' && now - s.ts <= TREND_WINDOW_MS);
  if (samples.length < 2) return null;
  const oldest = samples[0];
  const newest = samples[samples.length - 1];
  const span = newest.ts - oldest.ts;
  if (span < TREND_MINIMUM_MS) return null;

  const delta = newest.remaining - oldest.remaining;
  return {
    delta,
    seconds: Math.round(span / 1000),
    // `flat` is a finding, not an absence of one: nobody has been accounted
    // for in a minute, and that is the case worth a commander's attention.
    direction: delta < 0 ? 'falling' : delta > 0 ? 'rising' : 'flat',
  };
}

/**
 * Where to send somebody, rather than a list of who is missing.
 *
 * Thirty-four names each naming a different zone is a log. The same
 * information grouped by where those people were last seen is a dispatch
 * list: "floor-3: four people" is an instruction and "Dania Rauf, track went
 * stale" is not.
 *
 * The split that matters is not the zone, it is what the zone means. Somebody
 * last seen on floor 3 needs a search team. Somebody last seen standing in the
 * car park needs a warden to walk over and tick them off, and sending a search
 * team into the building for them spends the only budget there is -- the first
 * version of this panel listed both under the same heading and sorted the car
 * park above two floors.
 *
 * People nobody has ever seen are grouped under their own heading rather than
 * dropped. They are the ones a search cannot start from, and they are exactly
 * who a warden has to be sent to look for by name. They sit with the search
 * tier, because until something says otherwise they are inside.
 */
export function whereToLook(board, t, assemblyZones = null) {
  const rows = (board?.rows || []).filter((r) => r.state !== 'ACCOUNTED');

  // The zone panels when the caller has them, and the roster's own assignments
  // otherwise. A failed zone request must not silently turn the assembly point
  // back into somewhere to send a search team.
  const assembly = assemblyZones
    ? new Set(assemblyZones)
    : new Set((board?.rows || [])
      .map((r) => r.assigned_assembly_zone).filter(Boolean));

  const byZone = new Map();
  for (const row of rows) {
    const zone = row.last_zone_id || null;
    const key = zone ?? '__unseen__';
    if (!byZone.has(key)) {
      byZone.set(key, { zone, people: [], unseen: zone === null });
    }
    byZone.get(key).people.push(row);
  }

  const worst = (group) => group.people.filter((p) => p.state === 'UNACCOUNTED').length;
  return [...byZone.values()]
    .map((group) => {
      const atAssembly = group.zone !== null && assembly.has(group.zone);
      return {
        zone: group.zone,
        label: group.zone ?? t('board.never_seen'),
        unseen: group.unseen,
        atAssembly,
        advice: t(atAssembly ? 'board.needs_a_tick' : 'board.go_look'),
        count: group.people.length,
        unaccounted: worst(group),
        names: group.people.slice(0, 3).map((p) => p.display_name),
      };
    })
    // Inside the building first, whatever the counts say. Then most
    // unaccounted, then most people: a zone with four people nobody can
    // account for outranks one with nine who are merely unconfirmed.
    .sort((a, b) => (Number(a.atAssembly) - Number(b.atAssembly))
      || (b.unaccounted - a.unaccounted)
      || (b.count - a.count));
}

/**
 * The two themes, and what the button offering the other one says.
 *
 * Opted into rather than detected. There is no signal worth trusting -- the
 * ambient light API is gone from the browsers that matter, the OS colour
 * preference records what somebody set indoors last week, and the clock cannot
 * tell a night drill in a lit car park from a morning one. A warden who can
 * see nothing taps a button, and that choice outlives the drill.
 *
 * The button is labelled with the theme it switches *to*, which is the one
 * thing about a toggle that is worth getting right: a control labelled with
 * the state it is already in is a control people press twice.
 */
export const THEMES = ['night', 'sunlight'];

export function themeToggle(current, t) {
  // Anything unrecognised is night, which is the default and the one this
  // stylesheet is written in. A stored value from a future version must not
  // leave the page themeless.
  const now = THEMES.includes(current) ? current : 'night';
  const next = now === 'night' ? 'sunlight' : 'night';
  return {
    theme: now,
    next,
    label: t(`warden.theme_${next}`),
    //: The browser chrome around a standalone PWA is painted from this, and a
    //: black status bar over a white app is the seam that makes a web app look
    //: like a web app.
    chrome: now === 'sunlight' ? '#ffffff' : '#0f1720',
  };
}
