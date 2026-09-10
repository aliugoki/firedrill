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
