/**
 * The HTTP client. Thin, and honest about failing.
 *
 * Two behaviours matter more than anything else here.
 *
 * A failed request **never** resolves to a plausible-looking empty result. A
 * board that renders "0 expected, 0 accounted" because a fetch failed looks
 * exactly like a building that has been safely evacuated, and that is the most
 * dangerous screen this product could show. Failures reject, and the caller
 * shows the last known good data with a staleness marker.
 *
 * Every response carries the time it was fetched, so a screen can say how old
 * what it is showing is rather than implying it is live.
 */

export class ApiError extends Error {
  constructor(message, { status = 0, detail = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

export class Api {
  /**
   * `fetchImpl` defaults to a wrapper, never to `globalThis.fetch` itself.
   *
   * The bare reference was stored on the instance and then called as
   * `this._fetch(...)`, which is a method call: the browser sees a receiver
   * that is an `Api` and answers "Failed to execute 'fetch' on 'Window':
   * Illegal invocation". `request` catches that and throws "the server could
   * not be reached", so **every request from both front ends failed** and both
   * screens said the server was down while it was answering 200 to curl.
   *
   * Nothing caught it because every test injects a plain function here, which
   * has no receiver requirement. The tests exercised the code and skipped the
   * one line that only behaves differently in a browser.
   */
  constructor({
    baseUrl = '',
    headers = {},
    fetchImpl = (...args) => globalThis.fetch(...args),
  } = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.headers = headers;
    this._fetch = fetchImpl;
  }

  withIdentity({ userId, permissions = [], zones = [] }) {
    return new Api({
      baseUrl: this.baseUrl,
      fetchImpl: this._fetch,
      headers: {
        ...this.headers,
        'X-User-Id': userId,
        'X-Permissions': permissions.join(','),
        'X-Zones': zones.join(','),
      },
    });
  }

  async request(path, { method = 'GET', body = null } = {}) {
    let response;
    try {
      response = await this._fetch(`${this.baseUrl}${path}`, {
        method,
        headers: {
          'Content-Type': 'application/json',
          ...this.headers,
        },
        body: body === null ? undefined : JSON.stringify(body),
      });
    } catch (cause) {
      throw new ApiError('the server could not be reached', { detail: String(cause) });
    }

    if (!response.ok) {
      let detail = null;
      try {
        detail = (await response.json()).detail;
      } catch {
        detail = response.statusText;
      }
      throw new ApiError(detail || `request failed (${response.status})`, {
        status: response.status, detail,
      });
    }

    if (response.status === 204) return { fetched_at_ms: Date.now() };
    // The service worker marks a response it served from cache. Without reading
    // it, `fetched_at_ms` said "just now" for a roster that could be ten minutes
    // old, and every freshness check downstream believed it.
    const fromCache = response.headers?.get?.('X-EVAC-From-Cache') === '1';
    const payload = await response.json();
    if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
      return { ...payload, fetched_at_ms: Date.now(), from_cache: fromCache };
    }
    return payload;
  }

  listDrills() { return this.request('/api/evac/drills'); }
  createDrill(body) { return this.request('/api/evac/drills', { method: 'POST', body }); }
  startDrill(id) { return this.request(`/api/evac/drills/${id}/start`, { method: 'POST' }); }
  completeDrill(id) { return this.request(`/api/evac/drills/${id}/complete`, { method: 'POST' }); }
  board(id) { return this.request(`/api/evac/drills/${id}/board`); }
  priority(id, limit = 50) { return this.request(`/api/evac/drills/${id}/priority?limit=${limit}`); }
  timing(id) { return this.request(`/api/evac/drills/${id}/timing`); }
  zones(id) { return this.request(`/api/evac/drills/${id}/zones`); }
  bottlenecks(id) { return this.request(`/api/evac/drills/${id}/bottlenecks`); }
  explain(id, ref) {
    return this.request(`/api/evac/drills/${id}/people/${encodeURIComponent(ref)}/explain`);
  }
  /**
   * A zone's roster and panel, and the device's heartbeat.
   *
   * The device id is not decoration. A warden with nothing new to report does
   * not post a sync, so this five-second refresh is the only thing that tells
   * the command centre the tablet is still there. Without it a zone whose
   * warden has walked out of range looks exactly like one still being swept.
   */
  wardenZone(id, zone, deviceId) {
    const query = deviceId ? `?device_id=${encodeURIComponent(deviceId)}` : '';
    return this.request(`/api/evac/drills/${id}/warden/${zone}${query}`);
  }
  wardenSync(id, actions) {
    return this.request(`/api/evac/drills/${id}/warden/sync`, {
      method: 'POST', body: { actions },
    });
  }
  headcount(id, body) {
    return this.request(`/api/evac/drills/${id}/warden/headcount`, {
      method: 'POST', body,
    });
  }
  health() { return this.request('/healthz'); }
}

/**
 * Holds the last successful response and how old it is.
 *
 * A live board that silently keeps showing a two-minute-old picture is lying by
 * omission. This makes the staleness explicit so the screen can say so.
 */
export class Freshness {
  constructor(staleAfterMs = 10_000) {
    this.staleAfterMs = staleAfterMs;
    this.value = null;
    this.fetchedAtMs = null;
    this.fromCache = false;
    this.lastError = null;
  }

  succeed(value, now = Date.now()) {
    this.value = value;
    this.fetchedAtMs = now;
    this.fromCache = Boolean(value && value.from_cache);
    this.lastError = null;
    return value;
  }

  fail(error) {
    this.lastError = error;
    return this.value;
  }

  ageMs(now = Date.now()) {
    return this.fetchedAtMs === null ? null : Math.max(0, now - this.fetchedAtMs);
  }

  isStale(now = Date.now()) {
    // An answer the service worker remembered is never current, however
    // recently it was handed over. Keeping it is right -- a stale roster beats
    // no roster -- but calling it fresh is not.
    if (this.fromCache) return true;
    const age = this.ageMs(now);
    return age === null || age > this.staleAfterMs;
  }

  /** True only when there has never been a successful response. */
  get isEmpty() {
    return this.value === null;
  }
}
