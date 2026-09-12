/**
 * Drive a real browser over the DevTools protocol and report what a page shows.
 *
 * No dependencies: Node 22 has `WebSocket` built in, and the protocol is the
 * only thing a browser offers that a unit test cannot fake. That is the whole
 * point of this file. Every frontend test injects its own `fetch`, so the one
 * line that behaves differently in a browser -- `this._fetch(...)` finding an
 * `Api` as its receiver and answering "Illegal invocation" -- passed 142 tests
 * while both screens showed nothing at all.
 *
 * It also drives the DOM glue, which `CLAUDE.md` exempts from unit testing on
 * the grounds that it is glue. Exempt from unit testing is not exempt from
 * being wrong, and a click handler bound to an element that no longer exists
 * fails silently on the screen a warden is holding.
 *
 *     node probe.mjs <cdp-port> <json-spec>
 *
 * The spec is `{ url, wait, steps, read }`. `steps` are `{click}`, `{eval}` or
 * `{wait}` applied in order; `read` maps a name to a selector. Prints one JSON
 * object of `{ text, errors }`.
 */
const [port, specJson] = process.argv.slice(2);
const spec = JSON.parse(specJson);
const settle = spec.wait ?? 4000;

// A browser context of its own, so `localStorage` starts empty. The language
// toggle persists a choice, and without this the next probe in the run opens
// in whatever language the last one left behind -- which is a test reading a
// screen the product would never show it.
const browserWs = new WebSocket(
  (await (await fetch(`http://127.0.0.1:${port}/json/version`)).json())
    .webSocketDebuggerUrl);
await new Promise((resolve) => { browserWs.onopen = resolve; });
const contextId = await (async () => {
  let n = 0;
  const ask = (method, params = {}) => new Promise((resolve) => {
    const wanted = ++n;
    const listener = (event) => {
      const message = JSON.parse(event.data);
      if (message.id === wanted) {
        browserWs.removeEventListener('message', listener);
        resolve(message.result);
      }
    };
    browserWs.addEventListener('message', listener);
    browserWs.send(JSON.stringify({ id: wanted, method, params }));
  });
  const { browserContextId } = await ask('Target.createBrowserContext');
  const { targetId } = await ask('Target.createTarget',
                                 { url: 'about:blank', browserContextId });
  return targetId;
})();

const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const target = targets.find((t) => t.id === contextId);
const ws = new WebSocket(target.webSocketDebuggerUrl);
let id = 0;
const waiting = new Map();
const errors = [];

await new Promise((resolve, reject) => {
  ws.onopen = resolve;
  ws.onerror = () => reject(new Error('could not attach to the browser'));
});
ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.id && waiting.has(message.id)) {
    waiting.get(message.id)(message.result);
    waiting.delete(message.id);
    return;
  }
  // A page that throws on load renders an empty shell, which looks like a page
  // that simply has no data. Collected so the failure says which it was.
  if (message.method === 'Runtime.exceptionThrown') {
    errors.push(message.params.exceptionDetails.text
                || message.params.exceptionDetails.exception?.description);
  }
};
const send = (method, params = {}) => new Promise((resolve) => {
  const n = ++id;
  waiting.set(n, resolve);
  ws.send(JSON.stringify({ id: n, method, params }));
});
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const evaluate = async (expression) => {
  const result = await send('Runtime.evaluate', {
    expression, returnByValue: true, awaitPromise: true,
  });
  // An expression that throws does not raise `Runtime.exceptionThrown`: the
  // protocol hands the details back in the reply instead. Without this a step
  // that threw was indistinguishable from one that ran and changed nothing,
  // which cost an afternoon.
  if (result.exceptionDetails) {
    errors.push(`${expression.slice(0, 60)}: `
                + (result.exceptionDetails.exception?.description
                   || result.exceptionDetails.text));
    return null;
  }
  return result.result?.value ?? null;
};

await send('Runtime.enable');
await send('Network.enable');
// The page's own modules must be fetched fresh: a cached copy of the file
// under test is a test of yesterday's file.
await send('Network.setCacheDisabled', { cacheDisabled: true });
await send('Page.enable');
await send('Page.navigate', { url: spec.url });
await pause(settle);

for (const step of spec.steps ?? []) {
  if (step.click) {
    // Reported rather than thrown: "the button was not there" and "the button
    // did nothing" are different failures and the test should be able to tell.
    const found = await evaluate(
      `(() => { const el = document.querySelector(${JSON.stringify(step.click)});
                if (!el) return false; el.click(); return true; })()`);
    if (!found) errors.push(`no element matched ${step.click}`);
  }
  if (step.eval) await evaluate(step.eval);
  if (step.until) {
    // Wait for the condition rather than for a duration. A fixed sleep long
    // enough for a slow machine is wasted on every fast one, and one tuned to
    // a fast machine fails on a loaded one for no reason anybody can see.
    const deadline = Date.now() + (step.timeout ?? 15000);
    let met = false;
    while (Date.now() < deadline) {
      if (await evaluate(step.until)) { met = true; break; }
      await pause(250);
    }
    if (!met) errors.push(`condition never held: ${step.until}`);
  }
  await pause(step.wait ?? (step.until ? 0 : 1500));
}

const text = {};
for (const [name, selector] of Object.entries(spec.read ?? {})) {
  // `dir:<selector>` reads the direction attribute instead of the text, which
  // is how the Arabic layout is checked: the strings changing is not the same
  // as the page turning round.
  // `visible:` asks whether the element is on screen, which `innerText` cannot
  // answer: the HTML spec has it fall back to `textContent` for an element
  // that is not being rendered, so a hidden panel reads exactly like a shown
  // one.
  // `js:` evaluates an expression instead of reading an element, which is how
  // a before-and-after is taken: a probe reads once, at the end, so anything
  // the steps changed has to be stashed by the steps themselves.
  if (selector.startsWith('js:')) {
    text[name] = await evaluate(selector.slice(3));
    continue;
  }
  const kind = selector.startsWith('dir:') ? 'dir'
    : selector.startsWith('visible:') ? 'visible' : 'text';
  const css = kind === 'text' ? selector : selector.slice(selector.indexOf(':') + 1);
  const expression = {
    dir: "el.getAttribute('dir')",
    visible: 'el.offsetParent !== null',
    text: 'el.innerText',
  }[kind];
  text[name] = await evaluate(
    `(() => { const el = document.querySelector(${JSON.stringify(css)});
              if (!el) return null;
              return ${expression}; })()`);
}

console.log(JSON.stringify({ text, errors }));
ws.close();
browserWs.close();
