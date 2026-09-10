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
  constructor({ baseUrl = '', headers = {}, fetchImpl = globalThis.fetch } = {}) {
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
    const payload = await response.json();
    if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
      return { ...payload, fetched_at_ms: Date.now() };
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
  explain(id, ref) {
    return this.request(`/api/evac/drills/${id}/people/${encodeURIComponent(ref)}/explain`);
  }
  wardenZone(id, zone) { return this.request(`/api/evac/drills/${id}/warden/${zone}`); }
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
    this.lastError = null;
  }

  succeed(value, now = Date.now()) {
    this.value = value;
    this.fetchedAtMs = now;
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
    const age = this.ageMs(now);
    return age === null || age > this.staleAfterMs;
  }

  /** True only when there has never been a successful response. */
  get isEmpty() {
    return this.value === null;
  }
}
