/**
 * The warden device's offline queue.
 *
 * A warden at an assembly point may have no signal at all. Every action they
 * take is written to IndexedDB *before* the UI acknowledges it, and only
 * removed once the server has confirmed that specific action. The ordering of
 * those two facts is the whole design: acknowledging first and storing later
 * means a tab closed at the wrong moment silently loses a confirmation, and a
 * confirmation is the highest-trust evidence in the system.
 *
 * Sequence numbers are assigned here, on the device, at the moment of the
 * action. That is what makes offline work orderable: forty actions taken over
 * ten minutes without signal sync in the order they were taken, and a hole in
 * the sequence means one was genuinely lost rather than merely late.
 *
 * No framework, and no build step. See CLAUDE.md for why.
 */

/**
 * Turn an IndexedDB request into a promise that **rejects** on error.
 *
 * Wiring only `onsuccess` leaves a failed request hanging forever, and the
 * screen that awaits it simply never updates. A warden tapping confirm and
 * getting no response at all is worse than one who is told it failed: the first
 * looks like the app is thinking, and they move on.
 */
function settle(request, extract = (result) => result) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(extract(request.result));
    request.onerror = () => reject(request.error || new Error('IndexedDB request failed'));
  });
}

const DB_NAME = 'evac120';
const DB_VERSION = 1;
const STORE = 'pending';
const META = 'meta';

export class OfflineQueue {
  /**
   * `isOnline` is injected rather than read from `navigator` inside the class.
   * A queue that reaches for a global is a queue that cannot be tested without
   * a browser, and this is the one component whose failure loses a warden's
   * confirmations without trace.
   */
  constructor(deviceId, {
    indexedDB: idb = globalThis.indexedDB,
    isOnline = () => globalThis.navigator?.onLine ?? true,
  } = {}) {
    if (!deviceId) throw new Error('a queue must belong to a device');
    this.deviceId = deviceId;
    this._idb = idb;
    this._isOnline = isOnline;
    this._db = null;
  }

  async open() {
    if (this._db) return this._db;
    this._db = await new Promise((resolve, reject) => {
      const request = this._idb.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE)) {
          const store = db.createObjectStore(STORE, { keyPath: 'device_seq' });
          store.createIndex('ts', 'ts_ms');
        }
        if (!db.objectStoreNames.contains(META)) {
          db.createObjectStore(META, { keyPath: 'key' });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    return this._db;
  }

  /**
   * Run one transaction and resolve when it has committed, not when the last
   * request inside it succeeded.
   *
   * `fn` may return a value, a promise, or a thunk. The thunk is for the case
   * where what to resolve with is only known once a request inside the
   * transaction has run: it is called at `oncomplete`, so the caller is told it
   * worked only after the data is durable.
   */
  async _tx(stores, mode, fn) {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(stores, mode);
      let result;
      tx.oncomplete = () => resolve(typeof result === 'function' ? result() : result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
      result = fn(...stores.map((name) => tx.objectStore(name)));
    });
  }

  /**
   * The next sequence number this device will use.
   *
   * Persisted, so a device whose browser is closed and reopened mid-drill does
   * not restart at 1 and collide with actions the server has already accepted.
   */
  async nextSeq() {
    return this._tx([META], 'readonly', (meta) =>
      settle(meta.get('next_seq'), (result) => result?.value ?? 1));
  }

  /**
   * Record an action. Returns the stored row, including its sequence number.
   *
   * Durable before the caller is told it worked. A caller that renders a tick
   * on the strength of this promise is rendering something that survives the
   * tab being closed.
   */
  async enqueue(action) {
    if (!action || !action.kind) throw new Error('an action needs a kind');
    const base = {
      ...action,
      device_id: this.deviceId,
      ts_ms: action.ts_ms ?? Date.now(),
      queued_offline: action.queued_offline ?? !this._isOnline(),
    };

    // Reading the sequence, storing the row and advancing the counter happen in
    // one transaction, because `device_seq` is the key: two actions that draw
    // the same number are one row, and the second silently replaces the first.
    // Three separate transactions lost a confirmation two ways -- a warden
    // double-tapping, and a tab closed between the row landing and the counter
    // moving, after which the next action overwrote the last one.
    return this._tx([META, STORE], 'readwrite', (meta, store) => {
      let row;
      const request = meta.get('next_seq');
      request.onsuccess = () => {
        const seq = request.result?.value ?? 1;
        row = { ...base, device_seq: seq };
        store.put(row);
        meta.put({ key: 'next_seq', value: seq + 1 });
      };
      // No `onerror` here on purpose: an unhandled request error aborts the
      // transaction, which is what `onabort` above turns into a rejection.
      // Handling it locally would swallow the abort and leave the caller with
      // a row that was never written.
      return () => row;
    });
  }

  async pending() {
    const rows = await this._tx([STORE], 'readonly', (store) =>
      settle(store.getAll(), (result) => result || []));
    return rows.sort((a, b) => a.device_seq - b.device_seq);
  }

  async depth() {
    return (await this.pending()).length;
  }

  /**
   * Remove only what the server confirmed.
   *
   * Never "clear the queue on a successful response": a partial success is the
   * normal case, and dropping the refused actions along with the accepted ones
   * would lose a warden's work without telling them.
   */
  async acknowledge(seqs) {
    if (!seqs || seqs.length === 0) return 0;
    await this._tx([STORE], 'readwrite', (store) => {
      for (const seq of seqs) store.delete(seq);
    });
    return seqs.length;
  }

  async oldestPendingMs() {
    const rows = await this.pending();
    return rows.length ? rows[0].ts_ms : null;
  }

  /** How long the oldest unsent action has been waiting. */
  async stalenessMs(now = Date.now()) {
    const oldest = await this.oldestPendingMs();
    return oldest === null ? null : Math.max(0, now - oldest);
  }
}

/**
 * Decide which queued actions the server accepted.
 *
 * The server reports counts and a list of refusals keyed by sequence number. A
 * refused action is *removed* rather than retried forever: it was malformed or
 * out of scope, and retrying it every thirty seconds would bury the warden's
 * real work behind a permanent error. It is surfaced to the warden instead.
 *
 * A duplicate is acknowledged, not retried. The server already has it.
 */
export function reconcileSync(sent, response) {
  const refusedSeqs = new Set();
  if (Array.isArray(response.refusals)) {
    for (const refusal of response.refusals) refusedSeqs.add(refusal.device_seq);
  } else {
    // An older edge node, which reports refusals only as sentences. Reading the
    // sequence back out of the prose makes the wording of an error message a
    // wire contract: rephrase it and every refusal reads as an acceptance, and
    // this function's caller deletes a warden's confirmation while telling them
    // it synced. Kept only so an upgrade in progress does not lose work.
    for (const message of response.rejected || []) {
      const match = /seq (\d+)/.exec(message);
      if (match) refusedSeqs.add(Number(match[1]));
    }
  }

  const sentSeqs = new Set(sent.map((row) => row.device_seq));
  let acknowledged;

  if (Array.isArray(response.settled)) {
    // What the server says it is holding. Intersected with what this sync
    // sent, because deleting a row on the strength of a number that was not in
    // this batch would act on somebody else's answer.
    acknowledged = response.settled.filter((seq) => sentSeqs.has(seq));
  } else if (typeof response.accepted === 'number') {
    // An older edge node: no per-action answer exists, so the best available
    // reading is everything not explicitly refused. Reachable only because the
    // count proves this came from a server that knows this contract.
    acknowledged = sent
      .map((row) => row.device_seq)
      .filter((seq) => !refusedSeqs.has(seq));
  } else {
    // Nothing recognisable. A proxy that rewrote the body, a captive portal at
    // the assembly point answering 200, a field renamed in a version nobody
    // told this device about. Acknowledging on that basis empties a warden's
    // queue on the strength of a response that may not be from the API at all,
    // so nothing is deleted and the next sync tries again.
    acknowledged = [];
  }

  return {
    acknowledged,
    unanswered: sent.filter((row) => !acknowledged.includes(row.device_seq)
                                     && !refusedSeqs.has(row.device_seq)),
    refused: sent.filter((row) => refusedSeqs.has(row.device_seq)),
    refusalMessages: (response.refusals || []).map(
      (refusal) => `seq ${refusal.device_seq}: ${refusal.reason}`,
    ).concat(response.refusals ? [] : response.rejected || []),
  };
}

