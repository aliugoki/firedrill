/**
 * The offline queue.
 *
 * This is the piece that loses a warden's confirmations if it is wrong, so it
 * is tested against a working IndexedDB rather than a mock of itself.
 */

import assert from 'node:assert/strict';
import { describe, it, beforeEach } from 'node:test';

import { OfflineQueue, reconcileSync } from '../js/queue.js';
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

  it('treats a duplicate as done rather than retrying it forever', () => {
    const outcome = reconcileSync(sent, { accepted: 0, duplicates: 3, rejected: [] });
    assert.deepEqual(outcome.acknowledged, [1, 2, 3]);
  });
});
