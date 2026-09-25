/**
 * The icon set.
 *
 * Small surface, and worth testing anyway: these strings go straight into
 * `innerHTML` on both screens, and a malformed one would break the markup
 * around it rather than merely look wrong.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  DIRECTIONAL_ICONS, ICON_NAMES, colourIcon, icon, stateIcon,
} from '../js/icons.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const source = (file) => readFileSync(join(ROOT, file), 'utf8');

describe('every icon is well formed', () => {
  for (const name of ICON_NAMES) {
    it(`${name} is one closed svg element`, () => {
      const svg = icon(name);
      assert.ok(svg.startsWith('<svg '), svg.slice(0, 40));
      assert.equal(svg.match(/<svg /g).length, 1);
      assert.ok(svg.endsWith('</svg>'));
      // Unbalanced markup here would eat whatever follows it in the row.
      const opened = (svg.match(/<(path|circle|rect)\b/g) || []).length;
      const closed = (svg.match(/\/>/g) || []).length;
      assert.equal(opened, closed, `${name} has an unclosed shape`);
    });

    it(`${name} takes its colour from around it`, () => {
      // Never a literal colour: an icon inside a red chip has to be red
      // without this module knowing red exists.
      const svg = icon(name);
      assert.match(svg, /stroke="currentColor"/);
      assert.doesNotMatch(svg, /#[0-9a-fA-F]{3,6}|rgb\(/);
    });

    it(`${name} is hidden from a screen reader`, () => {
      // Decoration beside a label that already says the thing. "triangle"
      // announced before "needs attention" is worse than silence.
      assert.match(icon(name), /aria-hidden="true"/);
    });
  }
});

describe('an icon that does not exist', () => {
  it('is nothing rather than a crash or a placeholder', () => {
    // A missing glyph must never be what stops a board painting mid-drill.
    assert.equal(icon('no-such-icon'), '');
    assert.doesNotThrow(() => icon(undefined));
  });

  it('and a state nobody has heard of still gets something', () => {
    // The state machine can grow. A row with no chip glyph is worse than a
    // question mark, because the row looks like a different kind of thing.
    assert.notEqual(stateIcon('SOME_FUTURE_STATE'), '');
    assert.notEqual(colourIcon('PURPLE'), '');
  });
});

describe('the ones that mean a direction', () => {
  it('are the ones with a handedness, named here rather than inferred', () => {
    // Pinned to actual names. Comparing `icon(name)` against
    // `DIRECTIONAL_ICONS` reads like a test and is a tautology -- both sides
    // come from the same set, so emptying it passes. Verified by emptying it.
    assert.deepEqual([...DIRECTIONAL_ICONS], ['exit', 'search']);
  });

  it('an arrow out of a door mirrors', () => {
    assert.match(icon('exit'), /class="icon flip"/);
  });

  it('a tick does not, nor does a clock', () => {
    // Flipping something with no handedness only makes it look wrong.
    for (const name of ['check', 'cross', 'clock', 'people', 'alert']) {
      assert.doesNotMatch(icon(name), /flip/, name);
    }
  });

  it('and the set stays honest as icons are added', () => {
    for (const name of ICON_NAMES) {
      const flips = icon(name).includes('icon flip');
      assert.equal(flips, DIRECTIONAL_ICONS.includes(name), name);
    }
  });

  it('and the stylesheet actually mirrors them', () => {
    // The class is inert without the rule, which is the shape of defect this
    // codebase keeps finding: a value produced and nothing reading it.
    assert.match(source('css/app.css'), /\[dir="rtl"\][^{]*\.icon\.flip[^{]*\{[^}]*scaleX\(-1\)/);
  });
});

describe('every state the board can show has a glyph', () => {
  // Not a default question mark: the four colour bands are the operator's
  // whole vocabulary and each needs its own shape, because colour is the
  // channel that fails in sunlight and for about one man in twelve.
  const STATES = ['ACCOUNTED', 'UNACCOUNTED', 'UNCERTAIN',
                  'MANUAL_VERIFICATION_REQUIRED', 'CURRENTLY_UNOBSERVED'];

  it('and they are not all the same one', () => {
    const glyphs = new Set(STATES.map((s) => stateIcon(s)));
    assert.equal(glyphs.size, STATES.length, 'two states share a shape');
  });

  it('the four colour bands differ too', () => {
    const glyphs = new Set(['GREEN', 'YELLOW', 'ORANGE', 'RED'].map(colourIcon));
    assert.equal(glyphs.size, 4);
  });
});

describe('the icons ship with the app', () => {
  it('the service worker precaches the module', () => {
    // Inline SVG in a module is one file. A tablet starting from cache
    // without it loses every symbol on the screen.
    assert.match(source('sw.js'), /'\/static\/js\/icons\.js'/);
  });

  it('nothing reaches for an icon that does not exist', () => {
    // A typo'd name renders nothing at all, silently, which is exactly the
    // kind of quiet nothing that survives a review.
    const glue = ['js/command.js', 'js/warden.js', 'js/render.js'].map(source).join('\n');
    const asked = [...glue.matchAll(/\bicon\(\s*'([a-z]+)'/g)].map((m) => m[1]);
    assert.ok(asked.length > 5, `only found ${asked.length} icon calls`);
    const unknown = [...new Set(asked)].filter((n) => !ICON_NAMES.includes(n));
    assert.deepEqual(unknown, []);
  });
});
