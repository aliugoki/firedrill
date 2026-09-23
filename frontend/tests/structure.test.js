/**
 * Properties of the source that no unit test can reach.
 *
 * Each one here corresponds to a bug this codebase actually had. The DOM glue
 * is deliberately untested (see CLAUDE.md), which is defensible for behaviour
 * and leaves a gap for whole classes of mistake: a lookup that returns null, a
 * dialog that blocks the offline queue, a label that renders as its own key.
 * These are cheap, and they close the classes rather than the instances.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { escapeHtml } from '../js/render.js';
import { STRINGS } from '../js/i18n.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

function read(name) {
  return readFileSync(join(ROOT, name), 'utf8');
}

/** The source with comments removed, so prose about a bug is not the bug. */
function code(name) {
  return read(name)
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
}

/** Each page and the module that drives its DOM. */
const PAGES = [
  { page: 'index.html', script: 'js/command.js' },
  { page: 'warden.html', script: 'js/warden.js' },
];

const SHIPPED = ['js/command.js', 'js/warden.js', 'js/render.js', 'js/api.js',
                 'js/i18n.js', 'js/queue.js', 'sw.js'];

describe('nothing blocks the page', () => {
  // `prompt()` blocked the page, and the page runs the timers that drain the
  // offline queue. Worse, a standalone PWA on iOS may refuse to show one and
  // return null, which the caller read as "cancelled" -- so a warden's
  // escalation silently did nothing.
  for (const file of SHIPPED) {
    it(`${file} calls no modal dialog`, () => {
      const source = code(file);
      for (const blocking of ['prompt(', 'alert(', 'confirm(']) {
        assert.ok(!source.includes(blocking),
          `${file} calls ${blocking}, which blocks the offline queue`);
      }
    });
  }
});

describe('every element the glue reaches for exists', () => {
  // `getElementById` returns null on a typo, and the next property access
  // throws mid-paint. On the command centre that blanks the board during a
  // drill, which looks exactly like a building that has emptied.
  for (const { page, script } of PAGES) {
    it(`${script} only asks ${page} for ids it has`, () => {
      const markup = read(page);
      const present = new Set(
        [...markup.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
      const wanted = [...code(script)
        .matchAll(/getElementById\(\s*'([^']+)'/g)].map((m) => m[1]);

      assert.ok(wanted.length > 5, 'the scan found nothing to check');
      for (const id of wanted) {
        assert.ok(present.has(id), `${page} has no #${id}`);
      }
    });
  }
});

describe('nothing reaches for browser storage unguarded', () => {
  // `localStorage` is not a property you can read safely: on a device with
  // site data blocked the getter itself throws, and both screens read it at
  // module scope, so neither rendered anything at all -- not even the banner
  // this codebase built for that device. Every access goes through
  // `Settings`, which is the one place allowed to touch it.
  for (const file of SHIPPED) {
    if (file === 'js/queue.js') continue;
    it(`${file} goes through Settings`, () => {
      const source = code(file)
        // Not the prose. A comment naming the API is how the next reader finds
        // out why this rule exists.
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/^\s*\/\/.*$/gm, '');
      for (const api of ['localStorage', 'sessionStorage']) {
        assert.ok(!source.includes(api),
          `${file} touches ${api} directly; use Settings from queue.js`);
      }
    });
  }

  it('Settings itself is the only thing that does', () => {
    const source = code('js/queue.js');
    // Once, inside the try. A second reach is a second thing that can throw
    // somewhere the guard does not cover.
    const reaches = source.match(/globalThis\.localStorage/g) || [];
    assert.equal(reaches.length, 1, 'queue.js reaches for the store more than once');
  });
});

describe('the glue imports every function it calls', () => {
  // `CLAUDE.md` exempts `command.js` and `warden.js` from unit testing on the
  // grounds that they are DOM glue, so a call to a function nobody imported is
  // a `ReferenceError` at paint time that no unit test can see. The browser
  // gate catches it and the browser gate skips wherever Chrome is absent,
  // which is the normal state of an edge node -- so the check that runs
  // everywhere is this one.
  const exported = new Set(
    [...code('js/render.js').matchAll(/^export (?:function|const) (\w+)/gm)]
      .map((m) => m[1]));

  it('the scan found render.js', () => {
    assert.ok(exported.size > 20, `only found ${exported.size} exports`);
  });

  for (const file of ['js/command.js', 'js/warden.js']) {
    it(`${file} imports everything it uses from render.js`, () => {
      const source = code(file);
      const imported = new Set();
      for (const block of source.matchAll(
        /import\s*\{([^}]*)\}\s*from\s*'\.\/render\.js'/g)) {
        for (const name of block[1].split(',')) {
          const trimmed = name.trim();
          if (trimmed) imported.add(trimmed);
        }
      }
      // Only the names this file actually calls. A render export it has no use
      // for is not a defect.
      const body = source.slice(source.indexOf("from './render.js'"));
      const missing = [...exported].filter(
        (name) => !imported.has(name)
          && new RegExp(`\\b${name}\\s*\\(`).test(body));
      assert.deepEqual(missing, [],
        `${file} calls these without importing them: ${missing.join(', ')}`);
    });
  }
});

describe('every string the screens ask for is written', () => {
  // `t()` returns the key itself when a string is missing, which is ugly on
  // purpose -- but only if somebody looks at that screen in that language.
  const keys = new Set();
  for (const file of ['js/command.js', 'js/warden.js', 'js/render.js']) {
    for (const match of code(file).matchAll(/\bt\(\s*'([a-z0-9_.]+)'/g)) {
      // A key ending in a dot is a prefix built at runtime -- `t('state.' +
      // row.state)` -- and the suffixes are covered by the parity check.
      if (!match[1].endsWith('.')) keys.add(match[1]);
    }
  }

  it('the scan found the strings', () => {
    assert.ok(keys.size > 30, `only found ${keys.size}`);
  });

  for (const lang of Object.keys(STRINGS)) {
    it(`${lang} has all of them`, () => {
      const missing = [...keys].filter((key) => !(key in STRINGS[lang]));
      assert.deepEqual(missing, []);
    });
  }

  it('the two tables say the same things', () => {
    assert.deepEqual(Object.keys(STRINGS.en).sort(),
                     Object.keys(STRINGS.ar).sort());
  });
});

describe('the safety notice is on both pages', () => {
  // "This system supplements, never replaces, certified fire and life-safety
  // systems. Say so in the UI and docs."
  for (const { page } of PAGES) {
    it(`${page} has somewhere to put it`, () => {
      assert.match(read(page), /id="safety"/);
    });
  }

  for (const { script } of PAGES) {
    it(`${script} fills it in`, () => {
      assert.match(read(script), /app\.safety_notice/);
    });
  }
});

describe('the escaping is the right escaping', () => {
  // `escape` is a deprecated global that percent-encodes. `warden.js` called
  // it 26 times without defining or importing one, so every name on a warden's
  // tablet went through it: "Ali Khan" rendered as "Ali%20Khan", and an Arabic
  // name as "%u0639%u0644%u064A". It stopped injection and destroyed
  // legibility, on the screen whose job is letting a warden read names.
  for (const file of ['js/command.js', 'js/warden.js']) {
    it(`${file} never reaches the global escape at all`, () => {
      // Named rather than called: this looked only for `escape(`, and
      // `blocking.map(escape)` passes it as a reference, so the deprecated
      // global survived in the command centre's blocking list -- percent-
      // encoding every reason an all-clear was refused. Any bare mention of
      // the identifier is the failure.
      const source = code(file);
      const bare = [...source.matchAll(/(^|[^A-Za-z0-9_$.])escape\b(?!Html)/g)];
      assert.deepEqual(bare.map((m) => m.index), [],
        `${file} names the global escape, which percent-encodes`);
    });

    it(`${file} imports the one that escapes HTML`, () => {
      assert.match(code(file), /import \{[\s\S]*?escapeHtml[\s\S]*?\} from '\.\/render\.js'/);
    });
  }

  it('escapes markup without touching the words', () => {
    assert.equal(escapeHtml('<img src=x onerror=1>'),
      '&lt;img src=x onerror=1&gt;');
    // The part the global got wrong.
    assert.equal(escapeHtml('علي خان'), 'علي خان');
    assert.equal(escapeHtml('Ali Khan'), 'Ali Khan');
  });
});

describe('anything interpolated into an attribute is escaped', () => {
  // Breaking out of a quoted attribute is the easy injection, and the values
  // are server enums. Escaping every one of them removes the judgement call.
  for (const file of ['js/command.js', 'js/warden.js']) {
    it(`${file} escapes every attribute hole`, () => {
      const unescaped = [];
      for (const block of code(file).matchAll(/`([^`]*<[a-z][^`]*)`/g)) {
        for (const hole of block[1].matchAll(/="[^"]*\$\{([^}]*)\}/g)) {
          if (!hole[1].includes('escapeHtml(')) unescaped.push(hole[1].trim());
        }
      }
      assert.deepEqual(unescaped, []);
    });
  }
});
