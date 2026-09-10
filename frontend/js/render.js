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

/** Percentiles, with the caveat that belongs beside them. */
export function timingLine(timing, t) {
  if (!timing || !timing.building) return { text: t('timing.none'), caveat: null };
  const b = timing.building;
  if (b.p95 === null || b.p95 === undefined) {
    return { text: t('timing.none'), caveat: b.caveat };
  }
  const p50 = b.p50 === null ? '—' : `${b.p50.toFixed(1)}s`;
  const p95 = `${b.p95.toFixed(1)}s`;
  return {
    text: `${t('timing.p50')} ${p50} · ${t('timing.p95')} ${p95} · ` +
          `${t('timing.target')} ${timing.target_p95_s}s`,
    meetsTarget: timing.meets_target,
    caveat: b.reliable ? b.caveat : t('timing.unreliable'),
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
