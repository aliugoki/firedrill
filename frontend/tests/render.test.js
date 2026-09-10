/**
 * What goes on screen. These are the decisions, separated from the markup.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { Freshness, ApiError } from '../js/api.js';
import { createTranslator, missingKeys, STRINGS, isRtl } from '../js/i18n.js';
import {
  filterRoster, headcountVerdict, healthLine, orderForWarden, staleness,
  syncStatus, tiles, timingLine, verdict,
} from '../js/render.js';

const t = createTranslator('en');

function board(overrides = {}) {
  return {
    expected: 6, accounted: 4, uncertain: 1, unaccounted: 1,
    currently_unobserved: 0, still_evacuating: 0, unknown_people: 0,
    all_clear: false, blocking_all_clear: ['2 of 6 people not accounted for'],
    health: { degraded: false, blind: false, open_outages: 0, total_outages: 0,
              blind_fraction: 0, longest_blind_ms: 0, caveat: null },
    rows: [], ...overrides,
  };
}

describe('the verdict', () => {
  it('never decides for itself that a building is clear', () => {
    // A screen computing all-clear from counts alone would bypass the outage
    // and sweep checks the server combines.
    const counts_look_fine = board({
      accounted: 6, unaccounted: 0, uncertain: 0,
      all_clear: false, blocking_all_clear: ['1 outage still open'],
    });
    const result = verdict(counts_look_fine, t);
    assert.equal(result.clear, false);
    assert.deepEqual(result.reasons, ['1 outage still open']);
  });

  it('shows clear only when the server says so', () => {
    const result = verdict(board({ all_clear: true, blocking_all_clear: [] }), t);
    assert.equal(result.clear, true);
    assert.equal(result.headline, 'ALL CLEAR');
  });

  it('with no data at all it is not clear', () => {
    const result = verdict(null, t);
    assert.equal(result.clear, false);
    assert.ok(result.reasons.length > 0);
  });
});

describe('tiles', () => {
  it('cover every count an operator reads', () => {
    const keys = tiles(board(), t).map((x) => x.key);
    assert.deepEqual(keys, ['expected', 'accounted', 'evacuating', 'unobserved',
                            'uncertain', 'unaccounted', 'unknown']);
  });

  it('colour the dangerous ones', () => {
    const byKey = Object.fromEntries(tiles(board(), t).map((x) => [x.key, x.tone]));
    assert.equal(byKey.accounted, 'green');
    assert.equal(byKey.unaccounted, 'red');
    assert.equal(byKey.unobserved, 'orange');
  });

  it('render nothing rather than zeroes with no board', () => {
    assert.deepEqual(tiles(null, t), []);
  });
});

describe('health', () => {
  it('separates degraded from cannot-see', () => {
    // They call for different responses: something is wrong, versus nothing on
    // this screen can be trusted over a warden's own eyes.
    const degraded = healthLine(
      { degraded: true, blind: false, open_outages: 2, caveat: null }, t);
    const blind = healthLine(
      { degraded: true, blind: true, open_outages: 1, caveat: 'x' }, t);
    assert.equal(degraded.tone, 'degraded');
    assert.equal(blind.tone, 'blind');
  });

  it('is ok when nothing is wrong', () => {
    assert.equal(healthLine(board().health, t).tone, 'ok');
  });

  it('carries the caveat through', () => {
    const line = healthLine(
      { degraded: true, blind: true, open_outages: 1, caveat: 'blind for 40%' }, t);
    assert.equal(line.caveat, 'blind for 40%');
  });
});

describe('staleness', () => {
  it('says so when no data has ever arrived', () => {
    const banner = staleness(new Freshness(1000), t, 5000);
    assert.equal(banner.level, 'empty');
  });

  it('is silent while the picture is current', () => {
    const freshness = new Freshness(10_000);
    freshness.succeed(board(), 1000);
    assert.equal(staleness(freshness, t, 3000), null);
  });

  it('names the age and the reason when it is not', () => {
    // A live board silently showing a two-minute-old picture is lying by
    // omission.
    const freshness = new Freshness(5000);
    freshness.succeed(board(), 1000);
    freshness.fail(new ApiError('the server could not be reached'));
    const banner = staleness(freshness, t, 121_000);
    assert.match(banner.text, /120s ago/);
    assert.match(banner.text, /could not be reached/);
  });
});

describe('timing', () => {
  it('reports no measurements rather than zero', () => {
    const line = timingLine({ building: { p95: null, p50: null, reliable: false,
      caveat: 'No measurements: nobody had both a start and an arrival.' } }, t);
    assert.equal(line.text, 'No measurements yet');
    assert.match(line.caveat, /No measurements/);
  });

  it('replaces the caveat when the sample is too small to be a distribution', () => {
    const line = timingLine({
      building: { p50: 60, p95: 114, reliable: false, caveat: null },
      target_p95_s: 120,
    }, t);
    assert.match(line.caveat, /Too few measurements/);
  });

  it('shows the target beside the result', () => {
    const line = timingLine({
      building: { p50: 60, p95: 114, reliable: true, caveat: null },
      target_p95_s: 120, meets_target: true,
    }, t);
    assert.match(line.text, /114\.0s/);
    assert.match(line.text, /Target 120s/);
    assert.equal(line.meetsTarget, true);
  });
});

describe('the headcount verdict', () => {
  it('words the dangerous direction as the system overcounting', () => {
    const result = headcountVerdict({
      kind: 'SYSTEM_OVERCOUNTED', severity: 'ESCALATE',
      summary: 'two people missing', recommended_action: 'Do not declare all clear',
      missing_from_the_muster_point: 2,
    }, t);
    assert.equal(result.headline, 'The system counted more than you did');
    assert.equal(result.missing, 2);
    assert.match(result.advice, /Do not declare all clear/);
  });

  it('words the harmless direction differently', () => {
    const result = headcountVerdict({
      kind: 'SYSTEM_UNDERCOUNTED', severity: 'INVESTIGATE',
      summary: 'extra people', recommended_action: 'Tag them',
      missing_from_the_muster_point: 0,
    }, t);
    assert.equal(result.headline, 'You counted more than the system knows about');
  });

  it('is null before any count is taken', () => {
    assert.equal(headcountVerdict(null, t), null);
  });
});

describe('the warden roster', () => {
  const rows = [
    { display_name: 'Ayesha Khan', person_ref: 'emp:1', department: 'Engineering',
      colour: 'GREEN', state: 'ACCOUNTED' },
    { display_name: 'Bilal Ahmed', person_ref: 'emp:2', department: 'Finance',
      colour: 'RED', state: 'UNACCOUNTED' },
    { display_name: 'Chen Wei', person_ref: 'emp:3', department: 'Engineering',
      colour: 'YELLOW', state: 'UNCERTAIN' },
  ];

  it('puts the people who need checking first', () => {
    // A warden under pressure should not scroll past forty ticks to find the
    // two names nobody has laid eyes on.
    assert.deepEqual(orderForWarden(rows).map((r) => r.colour),
      ['RED', 'YELLOW', 'GREEN']);
  });

  it('searches name, reference and department', () => {
    assert.equal(filterRoster(rows, { query: 'ayesha' }).length, 1);
    assert.equal(filterRoster(rows, { query: 'engineering' }).length, 2);
    assert.equal(filterRoster(rows, { query: 'emp:2' }).length, 1);
  });

  it('filters by status', () => {
    assert.equal(filterRoster(rows, { status: 'UNACCOUNTED' }).length, 1);
  });

  it('handles an empty roster without throwing', () => {
    assert.deepEqual(filterRoster(null, {}), []);
    assert.deepEqual(orderForWarden(undefined), []);
  });
});

describe('the sync indicator', () => {
  it('says work is saved on the device when offline', () => {
    const status = syncStatus({ online: false, pending: 3, stalenessMs: 0 }, t);
    assert.equal(status.tone, 'offline');
    assert.match(status.text, /saved on this device/);
  });

  it('shows the backlog and its age when online but behind', () => {
    const status = syncStatus({ online: true, pending: 2, stalenessMs: 45_000 }, t);
    assert.match(status.text, /2 waiting to sync/);
    assert.match(status.text, /45s/);
  });

  it('confirms when everything has landed', () => {
    const status = syncStatus({ online: true, pending: 0, stalenessMs: null }, t);
    assert.equal(status.tone, 'online');
    assert.equal(status.text, 'All work synced');
  });
});

describe('translation', () => {
  it('has every string in both languages', () => {
    assert.deepEqual(missingKeys(), {});
  });

  it('covers both languages', () => {
    assert.deepEqual(Object.keys(STRINGS).sort(), ['ar', 'en']);
  });

  it('knows Arabic is right to left', () => {
    assert.equal(isRtl('ar'), true);
    assert.equal(isRtl('en'), false);
  });

  it('shows the key rather than leaking English when a string is missing', () => {
    // An untranslated label must be obvious to whoever tests the Arabic build.
    const arabic = createTranslator('ar');
    assert.equal(arabic('nonexistent.key'), 'nonexistent.key');
  });

  it('translates every accountability state in both languages', () => {
    const states = ['ACCOUNTED', 'EVACUATING', 'NOT_EVACUATED', 'UNCERTAIN',
                    'UNACCOUNTED', 'MANUAL_VERIFICATION_REQUIRED'];
    for (const lang of ['en', 'ar']) {
      const translate = createTranslator(lang);
      for (const state of states) {
        const label = translate(`state.${state}`);
        assert.notEqual(label, `state.${state}`, `${lang} is missing ${state}`);
      }
    }
  });

  it('carries the safety notice in both languages', () => {
    assert.match(STRINGS.en['app.safety_notice'], /never replaces/);
    assert.ok(STRINGS.ar['app.safety_notice'].length > 40);
  });
});
