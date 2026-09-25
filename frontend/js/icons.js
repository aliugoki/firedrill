/**
 * Inline SVG icons.
 *
 * Inline, and not a font or a sprite file, for the same reason the rest of this
 * frontend has no build step. The warden PWA starts from cache on a tablet with
 * no Internet: an icon font is one more file the service worker has to have
 * cached correctly or every label loses its symbol, and a sprite sheet is one
 * more request before the first paint. A string in a module that is already in
 * the shell cannot be half-loaded.
 *
 * Every icon is 24x24, `currentColor`, stroke-based. Colour comes from the
 * element around it, so an icon inside a red chip is red without anything here
 * knowing that red exists.
 *
 * **They are decoration, never the message.** Each one sits beside a label that
 * already says the thing. `aria-hidden` on all of them: a screen reader that
 * announced "triangle" before "needs attention" would be worse than silent, and
 * a warden who cannot tell a magnifier from a flag has still read the heading.
 */

//: 24x24, stroked, no fills. Kept as bare path data so the wrapper below is the
//: only place that decides size, stroke width or accessibility.
const PATHS = {
  //: Where to look. A search, because the panel is a dispatch list.
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  //: An assembly point: people gathered at a place.
  zone: '<path d="M12 21s7-5.6 7-11a7 7 0 1 0-14 0c0 5.4 7 11 7 11z"/>'
      + '<circle cx="12" cy="10" r="2.5"/>',
  //: A way out of the building.
  exit: '<path d="M14 3H5v18h9"/><path d="M10 12h10"/><path d="M17 8l4 4-4 4"/>',
  //: Elapsed and measured time.
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
  //: Something a person has to deal with.
  alert: '<path d="M12 4L2.5 20h19L12 4z"/><path d="M12 10v4"/>'
       + '<circle cx="12" cy="17.2" r=".9"/>',
  //: Accounted for.
  check: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.7 2.7L16 9.5"/>',
  //: Not accounted for.
  cross: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
  //: Uncertain, or an explanation to open.
  question: '<circle cx="12" cy="12" r="9"/>'
          + '<path d="M9.6 9.3a2.5 2.5 0 1 1 3.2 2.9c-.6.2-.9.8-.9 1.4v.4"/>'
          + '<circle cx="12" cy="17" r=".9"/>',
  //: Currently unobserved: the cameras cannot see them.
  unseen: '<path d="M3 3l18 18"/>'
        + '<path d="M10.6 6.3A9.6 9.6 0 0 1 12 6.2c5 0 9 5.8 9 5.8a17 17 0 0 1-2.6 3.1"/>'
        + '<path d="M6.5 8.1A17 17 0 0 0 3 12s4 5.8 9 5.8c1.3 0 2.5-.4 3.5-.9"/>'
        + '<path d="M9.9 10.2a3 3 0 0 0 4.1 4.2"/>',
  //: A warden. A person carrying authority, not a generic user.
  warden: '<circle cx="12" cy="8" r="3.4"/>'
        + '<path d="M5.5 20a6.5 6.5 0 0 1 9.4-5.8"/>'
        + '<path d="M15.5 18.6l1.8 1.8 3.2-3.6"/>',
  //: Several people: a headcount, a roster.
  people: '<circle cx="9" cy="8" r="3.2"/>'
        + '<path d="M3 19.5a6 6 0 0 1 12 0"/>'
        + '<path d="M16 5.6a3.2 3.2 0 0 1 0 6.3"/>'
        + '<path d="M17.5 14.2A6 6 0 0 1 21 19.5"/>',
  //: A tablet talking, or not.
  device: '<rect x="6" y="3" width="12" height="18" rx="2"/>'
        + '<path d="M10.8 17.6h2.4"/>',
};

//: Icons that mean a direction and must mirror when the page turns round. An
//: arrow leaving a door points the way the language reads; a clock and a tick
//: do not, and flipping them would only make them look wrong.
//:
//: Decided here rather than at each call site, because a caller reaching for
//: `exit` has no reason to remember which of these has a handedness.
const DIRECTIONAL = new Set(['exit', 'search']);

/**
 * One icon, as an SVG string ready to drop into markup.
 *
 * Returns `''` for a name that does not exist rather than throwing or drawing a
 * placeholder. A missing icon must never be what stops a board painting during
 * an evacuation, and the label beside it still says the thing.
 */
export function icon(name, { size = 18, stroke = 1.8 } = {}) {
  const path = PATHS[name];
  if (!path) return '';
  const classes = DIRECTIONAL.has(name) ? 'icon flip' : 'icon';
  return `<svg class="${classes}" width="${size}" height="${size}" viewBox="0 0 24 24"`
    + ` fill="none" stroke="currentColor" stroke-width="${stroke}"`
    + ` stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"`
    + ` focusable="false">${path}</svg>`;
}

/** Every name this build knows, so a test can hold the set to what is used. */
export const ICON_NAMES = Object.freeze(Object.keys(PATHS).sort());

/** The ones that mirror in a right-to-left layout. */
export const DIRECTIONAL_ICONS = Object.freeze([...DIRECTIONAL].sort());

/**
 * The icon for an accountability state.
 *
 * A second channel beside the colour. The four states are a colour scale and
 * roughly one man in twelve cannot read a red/green difference reliably, so the
 * shape carries the same information -- and on a sunlit tablet, where the whole
 * palette washes out, the shape is what survives.
 */
const FOR_STATE = {
  ACCOUNTED: 'check',
  UNACCOUNTED: 'cross',
  UNCERTAIN: 'question',
  MANUAL_VERIFICATION_REQUIRED: 'warden',
  CURRENTLY_UNOBSERVED: 'unseen',
  NOT_EVACUATED: 'alert',
  EVACUATING: 'people',
};

export function stateIcon(state, options) {
  return icon(FOR_STATE[state] || 'question', options);
}

/** Which icon a colour band gets, for rows that carry a colour and no state. */
const FOR_COLOUR = {
  GREEN: 'check', YELLOW: 'question', ORANGE: 'unseen', RED: 'cross',
};

export function colourIcon(colour, options) {
  return icon(FOR_COLOUR[colour] || 'question', options);
}
