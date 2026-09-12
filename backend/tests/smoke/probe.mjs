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
 *     node probe.mjs <cdp-port> <url> <json-of-selectors>
 *
 * Prints one JSON object of selector -> visible text.
 */
const [port, url, selectorsJson, waitMs = '7000'] = process.argv.slice(2);
const selectors = JSON.parse(selectorsJson);

const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,
                                  { method: 'PUT' })).json();
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

await send('Runtime.enable');
await send('Network.enable');
// The page's own modules must be fetched fresh: a cached copy of the file
// under test is a test of yesterday's file.
await send('Network.setCacheDisabled', { cacheDisabled: true });
await send('Page.enable');
await send('Page.navigate', { url });
await new Promise((resolve) => setTimeout(resolve, Number(waitMs)));

const text = {};
for (const [name, selector] of Object.entries(selectors)) {
  const result = await send('Runtime.evaluate', {
    expression: `(document.querySelector(${JSON.stringify(selector)})
                  ?.innerText ?? null)`,
    returnByValue: true,
  });
  text[name] = result.result?.value ?? null;
}

console.log(JSON.stringify({ text, errors }));
ws.close();
