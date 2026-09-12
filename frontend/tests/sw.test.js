/**
 * The service worker's precache list.
 *
 * The warden PWA exists to open at an assembly point with no network. That is
 * true only if every module it imports was precached, and the list is written
 * by hand because there is no build step to generate it. `render.js` was
 * missing, so a cold start offline failed to load the module graph and the app
 * did not open at all -- the one failure the PWA is for.
 *
 * This walks the imports the way the browser does and checks the list against
 * them, so the next module anybody adds cannot be forgotten silently.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { readFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..');

function precached() {
  const source = readFileSync(join(ROOT, 'sw.js'), 'utf8');
  const block = /const SHELL = \[([\s\S]*?)\];/.exec(source);
  assert.ok(block, 'sw.js must declare a SHELL array');
  return new Set([...block[1].matchAll(/'([^']+)'/g)].map((m) => m[1]));
}

/** Every module reachable from an entry point, as `/static/...` paths. */
function moduleGraph(entry) {
  const seen = new Set();
  const queue = [resolve(ROOT, entry)];
  while (queue.length) {
    const file = queue.pop();
    if (seen.has(file)) continue;
    seen.add(file);
    const source = readFileSync(file, 'utf8');
    for (const match of source.matchAll(/from\s+'(\.[^']+)'/g)) {
      queue.push(resolve(dirname(file), match[1]));
    }
  }
  return new Set([...seen].map((file) => `/static/${relative(ROOT, file)}`));
}

describe('the service worker precache', () => {
  it('lists every module the warden PWA imports', () => {
    const shell = precached();
    for (const path of moduleGraph('js/warden.js')) {
      assert.ok(shell.has(path), `${path} is imported but never precached`);
    }
  });

  it('lists the page itself and its stylesheet', () => {
    const shell = precached();
    for (const path of ['/static/warden.html', '/static/css/app.css']) {
      assert.ok(shell.has(path), `${path} is not precached`);
    }
  });

  it('precaches nothing that does not exist', () => {
    for (const path of precached()) {
      if (!path.startsWith('/static/')) continue;  // a route, not a file
      assert.doesNotThrow(
        () => readFileSync(join(ROOT, path.replace('/static/', ''))),
        `${path} is precached but is not in the repository`);
    }
  });
});

describe('a fix reaching a tablet that already has the shell', () => {
  /**
   * `cacheFirst` used to return the cached copy and stop. A service worker
   * only reinstalls when `sw.js` itself changes, so shipping a corrected
   * `warden.js` without touching `sw.js` left every device on the old copy
   * indefinitely -- on the one surface a warden uses during an evacuation.
   *
   * Checked against the source rather than by running a worker: there is no
   * service-worker runtime here, and what matters is structural.
   */
  function source() {
    return readFileSync(join(ROOT, 'sw.js'), 'utf8');
  }

  it('the cached answer is followed by a refresh', () => {
    const body = /async function cacheFirst\([\s\S]*?\n}/.exec(source());
    assert.ok(body, 'sw.js must define cacheFirst');
    assert.match(body[0], /revalidate\(request\)/);
  });

  it('the refresh is not awaited before answering', () => {
    // A warden opening the app on a weak signal must not wait on a refresh
    // they do not need yet.
    const body = /async function cacheFirst\([\s\S]*?\n}/.exec(source())[0];
    assert.ok(!/await revalidate/.test(body),
              'the refresh must not block the cached answer');
  });

  it('a failed refresh is swallowed rather than surfaced', () => {
    // Offline is this device's normal state and the cached copy already went
    // back, so a rejection here is noise with nowhere useful to go.
    const body = /function revalidate\([\s\S]*?\n}/.exec(source())[0];
    assert.match(body, /\.catch\(/);
  });

  it('no message handler promises a warm-up it cannot do', () => {
    // `cache.add` fetches without the app's Authorization header, so an
    // authenticated route answers 401 and the add rejects. The zone poll
    // caches the roster through networkFirst on its first success.
    assert.ok(!/CACHE_ZONE'/.test(source().replace(/\/\/[^\n]*/g, '')),
              'a live CACHE_ZONE handler cannot work for an authenticated route');
  });
});
