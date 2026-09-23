/**
 * The offline queue.
 *
 * This is the piece that loses a warden's confirmations if it is wrong, so it
 * is tested against a working IndexedDB rather than a mock of itself.
 */

import assert from 'node:assert/strict';
import { describe, it, beforeEach } from 'node:test';

import {
  OfflineQueue, Settings, deviceIdentity, reconcileSync,
} from '../js/queue.js';
import { createFakeIndexedDb } from './fake-idb.js';

function makeQueue(device = 'tablet-3', { online = true } = {}) {
  return new OfflineQueue(device, {
    indexedDB: createFakeIndexedDb(),
    isOnline: () => online,
  });
}

describe('OfflineQueue', () => {
  it('refuses to exist without a device', () => {
    assert.throws(() => new OfflineQueue(''), /must belong to a device/);
  });

  it('stores an action durably and returns its sequence', async () => {
    const queue = makeQueue();
    const row = await queue.enqueue({ kind: 'CONFIRM_PRESENT', subject: 'emp:EMP-1' });
    assert.equal(row.device_seq, 1);
    assert.equal(row.device_id, 'tablet-3');
    assert.equal(await queue.depth(), 1);
  });

  it('does not collide when two actions are taken at once', async () => {
    // A warden double-tapping, or a confirm-all that fans out, must not have
    // one confirmation silently overwrite another. `device_seq` is the key, so
    // two rows that draw the same number are one row.
    const queue = makeQueue();
    await Promise.all([
      queue.enqueue({ kind: 'CONFIRM_PRESENT', subject: 'emp:EMP-1' }),
      queue.enqueue({ kind: 'CONFIRM_PRESENT', subject: 'emp:EMP-2' }),
      queue.enqueue({ kind: 'CONFIRM_PRESENT', subject: 'emp:EMP-3' }),
    ]);
    const pending = await queue.pending();
    assert.equal(pending.length, 3);
    assert.deepEqual(pending.map((r) => r.device_seq), [1, 2, 3]);
    assert.deepEqual(pending.map((r) => r.subject).sort(),
      ['emp:EMP-1', 'emp:EMP-2', 'emp:EMP-3']);
  });

  it('assigns sequences on the device, in the order taken', async () => {
    // Forty actions taken over ten minutes without signal must sync in the
    // order the warden took them.
    const queue = makeQueue();
    for (let i = 0; i < 5; i += 1) {
      await queue.enqueue({ kind: 'CONFIRM_PRESENT', subject: `emp:EMP-${i}` });
    }
    const pending = await queue.pending();
    assert.deepEqual(pending.map((r) => r.device_seq), [1, 2, 3, 4, 5]);
    assert.deepEqual(pending.map((r) => r.subject),
      ['emp:EMP-0', 'emp:EMP-1', 'emp:EMP-2', 'emp:EMP-3', 'emp:EMP-4']);
  });

  it('does not restart at one when the browser is reopened', async () => {
    // A device whose tab is closed mid-drill must not collide with sequences
    // the server has already accepted.
    const idb = createFakeIndexedDb();
    const options = { indexedDB: idb, isOnline: () => true };
    const first = new OfflineQueue('tablet-3', options);
    await first.enqueue({ kind: 'CONFIRM_PRESENT', subject: 'a' });
    await first.enqueue({ kind: 'CONFIRM_PRESENT', subject: 'b' });

    const reopened = new OfflineQueue('tablet-3', options);
    const row = await reopened.enqueue({ kind: 'CONFIRM_PRESENT', subject: 'c' });
    assert.equal(row.device_seq, 3);
  });

  it('removes only what the server confirmed', async () => {
    // Clearing the queue on a successful response would drop the refused
    // actions alongside the accepted ones, losing a warden's work silently.
    const queue = makeQueue();
    for (let i = 0; i < 4; i += 1) {
      await queue.enqueue({ kind: 'CONFIRM_PRESENT', subject: `emp:${i}` });
    }
    await queue.acknowledge([1, 3]);
    const left = await queue.pending();
    assert.deepEqual(left.map((r) => r.device_seq), [2, 4]);
  });

  it('acknowledging nothing removes nothing', async () => {
    const queue = makeQueue();
    await queue.enqueue({ kind: 'CONFIRM_PRESENT', subject: 'a' });
    assert.equal(await queue.acknowledge([]), 0);
    assert.equal(await queue.depth(), 1);
  });

  it('reports how long the oldest action has been waiting', async () => {
    const queue = makeQueue();
    assert.equal(await queue.stalenessMs(1000), null);
    await queue.enqueue({ kind: 'CONFIRM_PRESENT', subject: 'a', ts_ms: 1000 });
    assert.equal(await queue.stalenessMs(241_000), 240_000);
  });

  it('refuses an action with no kind', async () => {
    const queue = makeQueue();
    await assert.rejects(() => queue.enqueue({ subject: 'a' }), /needs a kind/);
  });

  it('marks actions taken while offline', async () => {
    const queue = makeQueue('tablet-3', { online: false });
    const row = await queue.enqueue({ kind: 'CONFIRM_PRESENT', subject: 'a' });
    assert.equal(row.queued_offline, true);
  });

  it('does not mark actions taken while online', async () => {
    const queue = makeQueue('tablet-3', { online: true });
    const row = await queue.enqueue({ kind: 'CONFIRM_PRESENT', subject: 'a' });
    assert.equal(row.queued_offline, false);
  });
});

describe('reconcileSync', () => {
  const sent = [
    { device_seq: 1, kind: 'CONFIRM_PRESENT' },
    { device_seq: 2, kind: 'WRONG_PERSON' },
    { device_seq: 3, kind: 'CONFIRM_PRESENT' },
  ];

  it('acknowledges everything the server did not refuse', () => {
    const outcome = reconcileSync(sent, { accepted: 3, duplicates: 0, rejected: [] });
    assert.deepEqual(outcome.acknowledged, [1, 2, 3]);
  });

  it('keeps refused actions out of the acknowledgement', () => {
    const outcome = reconcileSync(sent, {
      accepted: 2, duplicates: 0,
      rejected: ['seq 2: WRONG_PERSON must name the identity being rejected'],
    });
    assert.deepEqual(outcome.acknowledged, [1, 3]);
    assert.equal(outcome.refused.length, 1);
    assert.equal(outcome.refused[0].device_seq, 2);
  });

  it('surfaces the refusal message rather than swallowing it', () => {
    // A warden must never discover at the end of a drill that half their
    // confirmations never landed.
    const outcome = reconcileSync(sent, {
      accepted: 2, duplicates: 0, rejected: ['seq 2: not assigned to that zone'],
    });
    assert.match(outcome.refusalMessages[0], /not assigned/);
  });

  it('reads refusals from the structured field, not from the prose', () => {
    const outcome = reconcileSync(sent, {
      accepted: 2, duplicates: 0,
      refusals: [{ device_seq: 2, reason: 'not assigned to that zone' }],
      rejected: ['seq 2: not assigned to that zone'],
    });
    assert.deepEqual(outcome.acknowledged, [1, 3]);
    assert.match(outcome.refusalMessages[0], /not assigned/);
  });

  it('survives a server that rewords its refusals', () => {
    // The wording is a message to a human, not a wire format. A server that
    // rephrases it must not cause this device to delete the very actions the
    // server refused.
    const outcome = reconcileSync(sent, {
      accepted: 2, duplicates: 0,
      refusals: [{ device_seq: 2, reason: 'zone not yours' }],
      rejected: ['action #2 was turned down: zone not yours'],
    });
    assert.deepEqual(outcome.acknowledged, [1, 3]);
    assert.equal(outcome.refused[0].device_seq, 2);
  });

  it('still understands an older server that only sends sentences', () => {
    const outcome = reconcileSync(sent, {
      accepted: 2, duplicates: 0,
      rejected: ['seq 2: not assigned to that zone'],
    });
    assert.deepEqual(outcome.acknowledged, [1, 3]);
  });

  it('treats a duplicate as done rather than retrying it forever', () => {
    const outcome = reconcileSync(sent, { accepted: 0, duplicates: 3, rejected: [] });
    assert.deepEqual(outcome.acknowledged, [1, 2, 3]);
  });
});

describe('a device that cannot save at all', () => {
  // A tablet in private browsing, one with site data blocked, one out of
  // quota. `queue.js` rejects rather than hangs precisely so the screen can
  // say so: "a warden tapping confirm and getting no response at all is worse
  // than one who is told it failed, because the first looks like the app is
  // thinking and they move on."
  function deadStorage() {
    return new OfflineQueue('tablet-3', {
      indexedDB: {
        open() {
          const request = {};
          queueMicrotask(() => request.onerror
            && request.onerror({ target: request }));
          request.error = new Error('access to storage is denied');
          return request;
        },
      },
      isOnline: () => true,
    });
  }

  it('rejects rather than never settling', async () => {
    await assert.rejects(() => deadStorage().enqueue({ kind: 'CONFIRM_PRESENT' }),
                         /storage is denied/);
  });

  it('rejects reads too, so the screen cannot show a stale zero', async () => {
    // `depth()` returning 0 on a broken store would read as "nothing pending",
    // which is the most reassuring thing it could possibly say.
    await assert.rejects(() => deadStorage().depth(), /storage is denied/);
  });
});

describe('deleting only what the server says it holds', () => {
  /**
   * `queue.js` says "remove only what the server confirmed" and then removed
   * everything it had sent that was not explicitly refused. That is the same
   * clear-on-success the sentence forbids, wearing a filter: any response the
   * device does not understand carries no refusals, so it reads as total
   * success and empties a warden's queue.
   */
  const sent = [
    { device_seq: 1, kind: 'CONFIRM_PRESENT' },
    { device_seq: 2, kind: 'CONFIRM_PRESENT' },
    { device_seq: 3, kind: 'NOT_HERE' },
  ];

  it('acknowledges the sequences the server settled', () => {
    const out = reconcileSync(sent, {
      accepted: 2, duplicates: 0, settled: [1, 3], refusals: [],
    });
    assert.deepEqual(out.acknowledged, [1, 3]);
  });

  it('keeps one the server did not answer for', () => {
    // Neither settled nor refused. Deleting it would lose a confirmation on
    // the strength of a silence.
    const out = reconcileSync(sent, {
      accepted: 2, duplicates: 0, settled: [1, 3], refusals: [],
    });
    assert.deepEqual(out.unanswered.map((r) => r.device_seq), [2]);
  });

  it('ignores a sequence that was not in this batch', () => {
    // Acting on a number this sync did not send means acting on somebody
    // else's answer.
    const out = reconcileSync(sent, {
      accepted: 1, duplicates: 0, settled: [1, 99], refusals: [],
    });
    assert.deepEqual(out.acknowledged, [1]);
  });

  it('counts a duplicate as settled', () => {
    // The server already has it, so keeping it means resending forever.
    const out = reconcileSync(sent, {
      accepted: 0, duplicates: 3, settled: [1, 2, 3], refusals: [],
    });
    assert.deepEqual(out.acknowledged, [1, 2, 3]);
  });

  it('deletes nothing when the response is not recognisable', () => {
    // A proxy that rewrote the body, or a captive portal at the assembly point
    // answering 200. Emptying a queue on that basis is the worst outcome
    // available, so nothing goes and the next sync tries again.
    const out = reconcileSync(sent, { message: 'welcome to guest wifi' });
    assert.deepEqual(out.acknowledged, []);
    assert.equal(out.unanswered.length, 3);
  });

  it('an older server with no settled list still works', () => {
    // The count proves this came from something that knows the contract, so
    // "everything not refused" is the best reading available and losing a
    // warden's work during an upgrade is the thing being avoided.
    const out = reconcileSync(sent, {
      accepted: 2, duplicates: 0, refusals: [{ device_seq: 3, reason: 'no' }],
    });
    assert.deepEqual(out.acknowledged, [1, 2]);
    assert.deepEqual(out.refused.map((r) => r.device_seq), [3]);
  });
});

describe('settings on a device that will not keep any', () => {
  const working = () => {
    const map = new Map();
    return {
      getItem: (k) => (map.has(k) ? map.get(k) : null),
      setItem: (k, v) => { map.set(k, String(v)); },
    };
  };

  it('reads and writes through a store that works', () => {
    const settings = new Settings(working());
    assert.equal(settings.set('evac.lang', 'ar'), true);
    assert.equal(settings.get('evac.lang'), 'ar');
    assert.equal(settings.failed, false);
  });

  it('answers the fallback rather than throwing when there is no store', () => {
    // This is the tablet with site data blocked. The old code read the
    // `localStorage` getter at module scope and the whole screen went blank.
    const settings = new Settings(null);
    assert.equal(settings.get('evac.lang', 'en'), 'en');
    assert.equal(settings.failed, true);
    assert.doesNotThrow(() => settings.set('evac.lang', 'ar'));
  });

  it('survives a store whose accessor throws on every call', () => {
    const hostile = {
      getItem() { throw new DOMException('denied', 'SecurityError'); },
      setItem() { throw new DOMException('denied', 'SecurityError'); },
    };
    const settings = new Settings(hostile);
    assert.equal(settings.get('evac.lang', 'en'), 'en');
    assert.equal(settings.set('evac.lang', 'ar'), false);
    assert.equal(settings.failed, true);
  });

  it('still remembers for this session when the store refuses to', () => {
    // A warden switching to Arabic must not have it switch back on the next
    // repaint. Falling back to memory is what keeps the device usable while
    // the banner says the choice will not survive a reload.
    const settings = new Settings(null);
    settings.set('evac.lang', 'ar');
    assert.equal(settings.get('evac.lang'), 'ar');
  });

  it('notices a store that reads fine and refuses to write', () => {
    // Quota. Much commoner than a full block, and the case a screen is least
    // likely to notice, because everything looks like it worked.
    const map = new Map([['evac.lang', 'en']]);
    const full = {
      getItem: (k) => (map.has(k) ? map.get(k) : null),
      setItem() { throw new DOMException('full', 'QuotaExceededError'); },
    };
    const settings = new Settings(full);
    assert.equal(settings.failed, false, 'nothing has failed yet');
    assert.equal(settings.get('evac.lang'), 'en');
    assert.equal(settings.set('evac.lang', 'ar'), false);
    assert.equal(settings.failed, true);
  });
});

describe('the device knowing which device it is', () => {
  const store = (initial = {}) => {
    const map = new Map(Object.entries(initial));
    return {
      getItem: (k) => (map.has(k) ? map.get(k) : null),
      setItem: (k, v) => { map.set(k, String(v)); },
    };
  };

  it('keeps the id it was given last time', () => {
    const settings = new Settings(store({ 'evac.device': 'device-abc' }));
    assert.deepEqual(deviceIdentity(settings),
                     { deviceId: 'device-abc', persisted: true });
  });

  it('makes one and says it will survive', () => {
    const settings = new Settings(store());
    const first = deviceIdentity(settings, { newId: () => 'device-new' });
    assert.deepEqual(first, { deviceId: 'device-new', persisted: true });
    // And the next load finds it rather than making a second one.
    assert.equal(deviceIdentity(settings).deviceId, 'device-new');
  });

  it('says plainly when the id will not survive a reload', () => {
    // The sequence numbers are per device, and a hole in the sequence is how
    // the server tells an action that was lost from one that was late. A
    // device that gets a new id on every reload restarts the sequence at 1,
    // and that distinction quietly stops existing. It still works; it has to
    // say so.
    const settings = new Settings(null);
    let n = 0;
    const one = deviceIdentity(settings, { newId: () => `device-${++n}` });
    assert.equal(one.persisted, false);
    assert.equal(one.deviceId, 'device-1');
  });

  it('does not hand out a second id within one session', () => {
    // The memory fallback has to hold here too, or two actions taken a minute
    // apart carry different device ids and neither sequence means anything.
    const settings = new Settings(null);
    let n = 0;
    const first = deviceIdentity(settings, { newId: () => `device-${++n}` });
    const second = deviceIdentity(settings, { newId: () => `device-${++n}` });
    assert.equal(second.deviceId, first.deviceId);
  });
});
