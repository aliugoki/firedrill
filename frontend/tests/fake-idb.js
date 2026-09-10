/**
 * A minimal in-memory IndexedDB, enough to exercise OfflineQueue for real.
 *
 * Worth the ~90 lines: the queue is the piece that loses a warden's
 * confirmations if it is wrong, and testing it against a mock of itself would
 * prove nothing. This implements the actual transaction and request shapes the
 * queue uses, so the code under test is the code that ships.
 */

class FakeRequest {
  constructor(run) {
    this.onsuccess = null;
    this.onerror = null;
    this.result = undefined;
    this.error = null;
    queueMicrotask(() => {
      try {
        this.result = run();
        this.onsuccess?.();
      } catch (error) {
        this.error = error;
        this.onerror?.();
      }
    });
  }
}

class FakeStore {
  constructor(name, keyPath, rows) {
    this.name = name;
    this.keyPath = keyPath;
    this.rows = rows;
  }
  put(row) { this.rows.set(row[this.keyPath], structuredClone(row)); return new FakeRequest(() => undefined); }
  get(key) { return new FakeRequest(() => structuredClone(this.rows.get(key))); }
  getAll() { return new FakeRequest(() => [...this.rows.values()].map((row) => structuredClone(row))); }
  delete(key) { this.rows.delete(key); return new FakeRequest(() => undefined); }
  createIndex() { return {}; }
}

class FakeTransaction {
  constructor(db, names) {
    this.db = db;
    this.names = names;
    this.oncomplete = null;
    this.onerror = null;
    this.onabort = null;
    this.error = null;
    queueMicrotask(() => queueMicrotask(() => queueMicrotask(() => this.oncomplete?.())));
  }
  objectStore(name) {
    if (!this.db.stores.has(name)) throw new Error(`no store ${name}`);
    const meta = this.db.stores.get(name);
    return new FakeStore(name, meta.keyPath, meta.rows);
  }
}

class FakeDatabase {
  constructor() {
    this.stores = new Map();
    this.objectStoreNames = {
      contains: (name) => this.stores.has(name),
    };
  }
  createObjectStore(name, { keyPath }) {
    this.stores.set(name, { keyPath, rows: new Map() });
    return new FakeStore(name, keyPath, this.stores.get(name).rows);
  }
  transaction(names) {
    return new FakeTransaction(this, Array.isArray(names) ? names : [names]);
  }
}

/** One factory per test, so tests cannot leak state into each other. */
export function createFakeIndexedDb() {
  const databases = new Map();
  return {
    open(name) {
      const request = { onupgradeneeded: null, onsuccess: null, onerror: null };
      queueMicrotask(() => {
        let db = databases.get(name);
        const isNew = !db;
        if (isNew) {
          db = new FakeDatabase();
          databases.set(name, db);
        }
        request.result = db;
        if (isNew) request.onupgradeneeded?.();
        request.onsuccess?.();
      });
      return request;
    },
  };
}
