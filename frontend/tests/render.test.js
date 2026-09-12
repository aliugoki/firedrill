/**
 * What goes on screen. These are the decisions, separated from the markup.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { Api, Freshness, ApiError } from '../js/api.js';
import { createTranslator, missingKeys, STRINGS, isRtl } from '../js/i18n.js';
import {
  filterRoster, headcountVerdict, healthLine, orderForWarden, staleness,
  syncStatus, tiles, timingLine, verdict,
  exitPressure,
  drillControl,
  composer,
  wardenContact,
  explainSummary,
  needsAHuman,
  lostSightBecause,
  rowNotes,
  syncProblems,
  describeReason,
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
    const line = timingLine({
      building: { p95: null, p50: null, reliable: false, coverage: 0 } }, t);
    assert.equal(line.text, 'No measurements yet');
  });

  it('says the sample is too small to be a distribution', () => {
    const line = timingLine({
      building: { p50: 60, p95: 114, reliable: false, coverage: 1 },
      target_p95_s: 120,
    }, t);
    assert.ok(line.caveats.some((c) => /Too few measurements/.test(c)));
  });

  it('says both when the sample is small and most people are missing', () => {
    // Different problems: one says the percentile is weak, the other says it
    // describes almost nobody and may have improved by losing the slow people.
    const line = timingLine({
      building: { p50: 60, p95: 114, reliable: false, coverage: 0.08 },
      target_p95_s: 120,
    }, t);
    assert.equal(line.caveats.length, 2);
    assert.ok(line.caveats.some((c) => /Too few measurements/.test(c)));
    assert.ok(line.caveats.some((c) => /92%/.test(c)));
  });

  it('is silent when the numbers stand on their own', () => {
    const line = timingLine({
      building: { p50: 60, p95: 114, reliable: true, coverage: 0.98 },
      target_p95_s: 120,
    }, t);
    assert.deepEqual(line.caveats, []);
  });

  it('words them in the reader language', () => {
    const arabic = createTranslator('ar');
    const line = timingLine({
      building: { p50: 60, p95: 114, reliable: false, coverage: 0.5 },
      target_p95_s: 120,
    }, arabic);
    assert.ok(line.caveats.every((c) => !/measurements/.test(c)));
  });

  it('shows the target beside the result', () => {
    const line = timingLine({
      building: { p50: 60, p95: 114, reliable: true, coverage: 1 },
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

describe('a remembered answer', () => {
  it('is never reported as current, however recently it arrived', () => {
    const fresh = new Freshness(10_000);
    fresh.succeed({ from_cache: true, fetched_at_ms: 1000 }, 1000);
    assert.equal(fresh.isStale(1001), true);
  });

  it('does not make a live answer look stale afterwards', () => {
    const fresh = new Freshness(10_000);
    fresh.succeed({ from_cache: true }, 1000);
    fresh.succeed({ from_cache: false }, 2000);
    assert.equal(fresh.isStale(2001), false);
  });

  it('says so on the warden bar, distinctly from being offline', () => {
    // The device may have signal and still be reading a roster the service
    // worker remembered, which is the case a warden is least likely to guess.
    const remembered = syncStatus(
      { online: true, pending: 0, fromCache: true }, t);
    const offline = syncStatus(
      { online: false, pending: 0, fromCache: false }, t);
    assert.notEqual(remembered.text, offline.text);
    assert.match(remembered.text, /Remembered/);
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

describe('which exit is holding the evacuation up', () => {
  const panel = {
    limiting_zone_id: 'exit-main',
    total_through: 140,
    measured_over_s: 300,
    caveats: ['no capacity is recorded for exit-fire, so no density'],
    exits: [
      { zone_id: 'exit-fire', completed: 60, queue: 2,
        throughput_per_min: 12, median_dwell_s: 4.0, density: null },
      { zone_id: 'exit-main', completed: 80, queue: 14,
        throughput_per_min: 16, median_dwell_s: 31.5, density: 0.8 },
    ],
  };

  it('names the exit the server said is limiting', () => {
    assert.match(exitPressure(panel, t).headline, /exit-main/);
  });

  it('puts the limiting exit first, then the longest queue', () => {
    // An operator reading under pressure should not have to scan for it.
    const rows = exitPressure(panel, t).rows;
    assert.deepEqual(rows.map((r) => r.zoneId), ['exit-main', 'exit-fire']);
    assert.equal(rows[0].limiting, true);
  });

  it('keeps an unmeasured density null rather than inventing one', () => {
    // Density needs a capacity most sites never record, and a crowding figure
    // against an invented denominator is worse than none.
    const rows = exitPressure(panel, t).rows;
    assert.equal(rows[1].density, null);
    assert.equal(rows[0].density, 0.8);
  });

  it('carries the caveats through', () => {
    assert.deepEqual(exitPressure(panel, t).caveats, panel.caveats);
  });

  it('says nothing has been measured rather than showing an empty table', () => {
    for (const empty of [null, {}, { exits: [] }]) {
      const result = exitPressure(empty, t);
      assert.deepEqual(result.rows, []);
      assert.match(result.headline, /No exit has been measured/);
    }
  });

  it('says so when no exit is limiting', () => {
    const clear = { ...panel, limiting_zone_id: null };
    assert.match(exitPressure(clear, t).headline, /No exit is holding anyone up/);
    assert.equal(exitPressure(clear, t).rows[0].limiting, false);
  });
});

describe('what an operator can do to the drill', () => {
  it('offers nothing when there is no drill', () => {
    const control = drillControl(null, t);
    assert.equal(control.action, null);
    assert.equal(control.status, 'No drill');
  });

  it('offers to start a drill that has not started', () => {
    const control = drillControl({ status: 'DRAFT' }, t);
    assert.equal(control.action.kind, 'start');
    assert.equal(control.status, 'Not started');
  });

  it('offers nothing on a drill that is over', () => {
    assert.equal(drillControl({ status: 'COMPLETE' }, t).action, null);
  });

  it('arms before it ends a running drill', () => {
    // Ending stops accountability, so the first press only arms.
    const first = drillControl({ status: 'RUNNING' }, t);
    assert.equal(first.action.kind, 'complete');
    assert.equal(first.action.armed, false);
    assert.match(first.action.label, /End drill/);

    const second = drillControl({ status: 'RUNNING' }, t, { armed: true });
    assert.equal(second.action.armed, true);
    assert.match(second.action.label, /press again/);
  });

  it('says what is outstanding only once the operator reaches for it', () => {
    const drill = {
      status: 'RUNNING', all_clear: false,
      blocking_all_clear: ['3 of 40 people not accounted for'],
    };
    assert.deepEqual(drillControl(drill, t).blocking, []);
    assert.deepEqual(drillControl(drill, t, { armed: true }).blocking,
      ['3 of 40 people not accounted for']);
  });

  it('has nothing outstanding to report when the board is clear', () => {
    const clear = { status: 'RUNNING', all_clear: true,
                    blocking_all_clear: [] };
    assert.deepEqual(drillControl(clear, t, { armed: true }).blocking, []);
  });

  it('takes the blocking reasons from the server, never its own view', () => {
    // A screen that decided for itself would bypass the outage and sweep
    // checks entirely.
    const lying = { status: 'RUNNING', all_clear: false, blocking_all_clear: [] };
    assert.deepEqual(drillControl(lying, t, { armed: true }).blocking, []);
  });
});

describe('when the commander can stand down', () => {
  it('is shown beside the evacuation times', () => {
    // Separate from the percentiles and usually much longer: the building
    // empties in two minutes, and establishing that nobody is left takes as
    // long as the last uncertain person takes to resolve.
    const line = timingLine({
      building: { p50: 60, p95: 114, reliable: true, coverage: 1 },
      target_p95_s: 120, accountability_completion_s: 418.5,
    }, t);
    assert.match(line.settled, /Accountability settled after 418\.5s/);
  });

  it('is null while it is still unsettled rather than zero', () => {
    const line = timingLine({
      building: { p50: 60, p95: 114, reliable: true, coverage: 1 },
      target_p95_s: 120, accountability_completion_s: null,
    }, t);
    assert.equal(line.settled, null);
  });

  it('is reported even when no percentile could be produced', () => {
    const line = timingLine({
      building: { p95: null, p50: null, reliable: false, coverage: 0 },
      accountability_completion_s: 300.0,
    }, t);
    assert.match(line.settled, /300\.0s/);
  });
});

describe('what a warden types an escalation into', () => {
  it('is closed until something opens it', () => {
    const state = composer(null, '', t);
    assert.equal(state.open, false);
    assert.equal(state.canSend, false);
  });

  it('will not send an escalation with no words', () => {
    // `sweep.escalate` would record "no reason recorded", and the drill report
    // promises escalations in the warden's own words.
    for (const blank of ['', '   ', '\n']) {
      const state = composer('ESCALATE', blank, t);
      assert.equal(state.canSend, false);
      assert.match(state.hint, /Say what is wrong/);
    }
  });

  it('sends once there are words, and trims them', () => {
    const state = composer('ESCALATE', '  smoke in the west stairwell  ', t);
    assert.equal(state.canSend, true);
    assert.equal(state.text, 'smoke in the west stairwell');
    assert.equal(state.hint, null);
  });

  it('titles itself by what is being written', () => {
    assert.match(composer('ESCALATE', 'x', t).title, /Escalate/);
    assert.match(composer('NOTE', 'x', t).title, /Add a note/);
  });

  it('carries the kind through so the caller does not re-derive it', () => {
    assert.equal(composer('NOTE', 'x', t).kind, 'NOTE');
  });
});

describe('a warden device that has gone quiet', () => {
  // A zone with a warden still walking it and a zone whose warden has walked
  // out of range look identical on the board: neither is swept. During an
  // evacuation one means wait and the other means send somebody.
  const t = (key) => key;

  it('says nothing while the tablet is talking', () => {
    assert.equal(wardenContact({ warden_silent_ms: 4_000 }, t), null);
  });

  it('speaks up once the silence is long enough', () => {
    const contact = wardenContact({ warden_silent_ms: 300_000 }, t);
    assert.equal(contact.tone, 'silent');
    assert.match(contact.text, /300s/);
  });

  it('distinguishes never connected from gone quiet', () => {
    // A zone whose warden never arrived is a different problem from one whose
    // warden arrived and stopped answering, and the same blank panel serves
    // for both unless this says which.
    const never = wardenContact({ warden_silent_ms: null }, t);
    assert.equal(never.tone, 'unheard');
    assert.notEqual(never.text, wardenContact({ warden_silent_ms: 300_000 }, t).text);
  });

  it('treats a missing panel as never connected rather than fine', () => {
    assert.equal(wardenContact(undefined, t).tone, 'unheard');
  });

  it('takes the threshold from the caller', () => {
    // How long a tablet may be quiet before somebody walks over to it is a
    // site's decision, not a number derived from anything.
    assert.equal(wardenContact({ warden_silent_ms: 20_000 }, t,
                               { silenceMs: 10_000 }).tone, 'silent');
    assert.equal(wardenContact({ warden_silent_ms: 20_000 }, t,
                               { silenceMs: 60_000 }), null);
  });
});

describe('what the explain drawer was only implying', () => {
  const t = (key) => key;

  it('names the competing claims rather than saying disputed', () => {
    // Invariant 3: a conflict is reported and never adjudicated, and reporting
    // it means saying what it is. The drawer had a boolean.
    const { disputes } = explainSummary({
      is_disputed: true,
      disputes: [{ subject: 'gp-7', identities: ['EMP-001', 'EMP-002'] }],
    }, t);
    assert.equal(disputes.length, 1);
    assert.match(disputes[0].text, /EMP-001 \/ EMP-002/);
  });

  it('lifts blindness out of the ordinary observations', () => {
    // "The camera covering their floor was down" is the answer to the question
    // a warden asks, and it arrived as one line among thirty.
    const { blindness } = explainSummary({
      blindness: [{ summary: 'cam-4 offline for 2 minutes' }],
      context: [{ summary: 'cam-4 offline for 2 minutes' }],
    }, t);
    assert.deepEqual(blindness.map((b) => b.text),
                     ['cam-4 offline for 2 minutes']);
  });

  it('says nothing about a person with neither', () => {
    const summary = explainSummary({ narrative: ['seen at 10:42'] }, t);
    assert.deepEqual(summary, { disputes: [], blindness: [] });
  });

  it('survives an explanation that never arrived', () => {
    assert.deepEqual(explainSummary(null, t), { disputes: [], blindness: [] });
  });
});

describe('the people no camera can settle', () => {
  // `needs_human_to_account` reached the API on every row and no screen read
  // it. `roster.py` says it must be surfaced at drill start rather than
  // discovered at minute four while an operator waits for a match that can
  // never arrive, and the flag travelled to the browser and stopped.
  const t = (key) => key;

  it('marks somebody with no gallery entry who is not accounted for', () => {
    const mark = needsAHuman(
      { needs_human_to_account: true, state: 'UNCERTAIN' }, t);
    assert.equal(mark.tone, 'yellow');
  });

  it('says nothing once a warden has confirmed them', () => {
    // The fact has done its work; a badge on a settled row is clutter on a
    // screen that cannot afford any.
    assert.equal(needsAHuman(
      { needs_human_to_account: true, state: 'ACCOUNTED' }, t), null);
  });

  it('says nothing about an ordinary person', () => {
    assert.equal(needsAHuman({ state: 'UNCERTAIN' }, t), null);
  });

  it('puts them first inside their colour band', () => {
    // Waiting changes nothing for them, so the warden is the only way they get
    // accounted for and they belong at the top of the band.
    const ordered = orderForWarden([
      { colour: 'RED', display_name: 'Aaa' },
      { colour: 'RED', display_name: 'Zzz', needs_human_to_account: true },
      { colour: 'GREEN', display_name: 'Bbb', needs_human_to_account: true },
    ]);
    assert.deepEqual(ordered.map((r) => r.display_name), ['Zzz', 'Aaa', 'Bbb']);
  });
});

describe('why the system lost sight of somebody', () => {
  // A row that says "not seen for 90 seconds" invites the reading that the
  // person moved. Often they did not and the camera stopped working, and those
  // call for opposite responses: one is a search, the other is a caveat.
  // `health.py` names this the question that matters at the assembly point,
  // and `blinded_targets_at` answered it while no screen asked.
  const t = (key) => key;

  it('names the cameras that were dark', () => {
    const why = lostSightBecause(
      { state: 'UNCERTAIN', blinded_by: ['cam-4', 'cam-9'] }, t);
    assert.match(why.text, /cam-4, cam-9/);
    assert.equal(why.tone, 'orange');
  });

  it('says nothing about somebody already accounted for', () => {
    // It lost sight of them and then found them; the reason has stopped
    // changing what anybody does.
    assert.equal(lostSightBecause(
      { state: 'ACCOUNTED', blinded_by: ['cam-4'] }, t), null);
  });

  it('says nothing when every camera was working', () => {
    assert.equal(lostSightBecause({ state: 'UNACCOUNTED' }, t), null);
  });

  it('both notes appear under one row, worst first', () => {
    // One renderer for both screens: a second copy of the loop is where the
    // warden's view and the commander's view start to disagree.
    const notes = rowNotes({
      state: 'UNACCOUNTED', needs_human_to_account: true,
      blinded_by: ['cam-4'],
    }, t);
    assert.deepEqual(notes.map((n) => n.tone), ['yellow', 'orange']);
  });

  it('an ordinary row gets no notes at all', () => {
    assert.deepEqual(rowNotes({ state: 'UNCERTAIN' }, t), []);
  });
});

describe('a warden device that cannot save the work', () => {
  const t = (key) => key;

  it('outranks everything else the strip can say', () => {
    // Offline with forty pending is a warden who will sync later. A device
    // that cannot write is a warden whose work does not exist, and the two
    // must not share a tone.
    const status = syncStatus({
      storageFailed: true, online: false, pending: 40, fromCache: true,
    }, t);
    assert.equal(status.tone, 'blind');
    assert.equal(status.text, 'warden.cannot_save');
  });

  it('an ordinary offline device still reads as offline', () => {
    const status = syncStatus({ online: false, pending: 2 }, t);
    assert.equal(status.tone, 'offline');
  });

  it('the message tells them what to do instead', () => {
    // A banner that says something is wrong and not what to do about it is a
    // banner a warden reads once.
    for (const lang of ['en', 'ar']) {
      assert.ok(STRINGS[lang]['warden.cannot_save'],
                `${lang} has no cannot_save string`);
    }
    assert.match(STRINGS.en['warden.cannot_save'], /radio/);
  });
});

describe('what a warden reads after a sync', () => {
  // The sync route says a device "that syncs forty actions and has one refused
  // must be able to tell which, or a warden's screen shows work that never
  // landed". The server said which, the queue turned it into sentences, and
  // the screen printed "· 1 refused" and dropped them.
  const t = (key) => key;

  it('shows each refusal in full', () => {
    const lines = syncProblems({
      refusals: ['seq 3: not assigned to assembly-south'],
    }, t);
    assert.deepEqual(lines.map((l) => l.text),
                     ['seq 3: not assigned to assembly-south']);
    assert.equal(lines[0].tone, 'red');
  });

  it('keeps a refusal and an unanswered action apart', () => {
    // A refusal is final and needs the warden to act. An unanswered action is
    // still queued and goes again on the next sync. One number for both would
    // make the first look survivable.
    const lines = syncProblems({
      refusals: ['seq 3: no'], unanswered: [{ device_seq: 4 }],
    }, t);
    assert.deepEqual(lines.map((l) => l.tone), ['red', 'yellow']);
  });

  it('says nothing when the sync was clean', () => {
    assert.deepEqual(syncProblems({}, t), []);
  });

  it('counts the unanswered rather than listing them', () => {
    // They are transient by construction, so which ones matters less than that
    // there are any.
    const lines = syncProblems({ unanswered: [{}, {}, {}] }, t);
    assert.match(lines[0].text, /^3 /);
  });
});

describe('the default fetch, which no test had ever used', () => {
  /**
   * `fetchImpl` defaulted to `globalThis.fetch` itself, stored on the instance
   * and then called as `this._fetch(...)`. That is a method call, so a browser
   * sees a receiver that is an `Api` and answers "Failed to execute 'fetch' on
   * 'Window': Illegal invocation". `request` catches it and throws "the server
   * could not be reached", so every request from both front ends failed and
   * both screens said the server was down while it answered 200 to curl.
   *
   * Every other test injects a plain function here, which has no receiver
   * requirement, so the suite passed while the product did not work at all.
   * Node is lenient about the receiver, so this asserts the receiver directly
   * rather than waiting for a throw that only happens in a browser.
   */
  it('is never called with the Api instance as its receiver', async () => {
    const original = globalThis.fetch;
    const receivers = [];
    globalThis.fetch = function (url) {
      receivers.push(this);
      return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null },
        json: () => Promise.resolve({ ok: true }),
      });
    };
    try {
      const api = new Api().withIdentity({ userId: 'c1', permissions: [] });
      await api.request('/api/evac/drills');
      assert.equal(receivers.length, 1);
      assert.ok(receivers[0] === globalThis || receivers[0] === undefined,
                `fetch was called on ${receivers[0]?.constructor?.name}`);
    } finally {
      globalThis.fetch = original;
    }
  });

  it('survives being passed through withIdentity', () => {
    // The identity wrapper builds a second Api from the first one's handle, so
    // a correct default that the wrapper unwraps would be no fix at all.
    const api = new Api().withIdentity({ userId: 'c1', permissions: [] });
    assert.notEqual(api._fetch, globalThis.fetch);
  });
});

describe('the reason under a name, in the language being read', () => {
  // The server sends prose and a code. Prose is right for the post-drill
  // report, which is a document; this line is read in Arabic on a tablet at an
  // assembly point and it is what tells a warden what to do about the person
  // in front of them.
  const t = createTranslator('en');
  const ar = createTranslator('ar');

  it('words a code rather than showing the server sentence', () => {
    const text = describeReason({
      reason: 'track went stale; last seen in FLOOR zone floor-2 on cam-4',
      reason_code: 'TRACK_LOST',
      reason_detail: { zone_kind: 'FLOOR', zone_id: 'floor-2', camera_id: 'cam-4' },
    }, t);
    assert.match(text, /lost track/);
    assert.match(text, /floor-2/);
    assert.match(text, /cam-4/);
  });

  it('words the same code in Arabic', () => {
    const text = describeReason({
      reason: 'track went stale', reason_code: 'TRACK_LOST',
      reason_detail: { zone_id: 'floor-2' },
    }, ar);
    assert.ok(!/lost track/.test(text), text);
    assert.match(text, /floor-2/, 'the zone id is an identifier, not a word');
  });

  it('falls back to the server sentence when there is no code', () => {
    // An older edge node. An English sentence under somebody's name beats a
    // blank line.
    const text = describeReason({ reason: 'on the roster, not yet observed' }, t);
    assert.equal(text, 'on the roster, not yet observed');
  });

  it('falls back when the code is one this device does not know', () => {
    // A code added in a version this tablet has not been updated to. `t`
    // renders a missing key as the key itself, which is useful in testing and
    // useless at an assembly point.
    const text = describeReason({
      reason: 'something the server explained',
      reason_code: 'INVENTED_LATER',
    }, t);
    assert.equal(text, 'something the server explained');
  });

  it('says when a confirmed face is no longer visible', () => {
    // The same code, two meanings. "Identified" and "identified earlier, face
    // not visible now" are different things to tell somebody.
    const fresh = describeReason({
      reason_code: 'ASSEMBLY_WITH_IDENTITY',
      reason_detail: { face_visible: true },
    }, t);
    const stale = describeReason({
      reason_code: 'ASSEMBLY_WITH_IDENTITY',
      reason_detail: { face_visible: false },
    }, t);
    assert.notEqual(fresh, stale);
    assert.match(stale, /not visible/);
  });

  it('names the warden who confirmed somebody', () => {
    const text = describeReason({
      reason_code: 'WARDEN_CONFIRMED_AT_ASSEMBLY',
      reason_detail: { warden_id: 'warden-7', zone_id: 'assembly-north' },
    }, t);
    assert.match(text, /warden-7/);
    assert.match(text, /assembly-north/);
  });

  it('says nothing about a location nobody has one for', () => {
    const text = describeReason({
      reason_code: 'NEVER_OBSERVED_OVERDUE', reason_detail: { seconds: 240 },
    }, t);
    assert.match(text, /240/);
    assert.ok(!/last seen/.test(text), text);
  });
});
