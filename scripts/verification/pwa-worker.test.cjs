const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

// Exercise the emitted worker without a browser profile or business records.
const source = fs.readFileSync(process.env.TSCM_WORKER_FILE || '/worker.js', 'utf8');
function harness() {
  const stores = new Map(), handlers = {}, messages = [];
  const key = input => new URL(typeof input === 'string' ? input : input.url, 'https://tscm.test').pathname;
  let online = true, reply = () => new Response('live', {headers: {'X-TSCM-Offline-Clock': '1'}});
  const cache = name => {
    if (!stores.has(name)) stores.set(name, new Map());
    const values = stores.get(name);
    return {
      addAll: async assets => { for (const asset of assets) values.set(key(asset), new Response('asset')); },
      put: async (input, response) => values.set(key(input), response.clone()),
      match: async input => values.get(key(input))?.clone(),
    };
  };
  const context = {
    URL, Response, Headers, console,
    fetch: async request => { if (!online) throw new TypeError('offline'); return reply(request); },
    caches: {
      open: async name => cache(name),
      keys: async () => [...stores.keys()],
      delete: async name => stores.delete(name),
      match: async input => {
        for (const name of stores.keys()) { const found = await cache(name).match(input); if (found) return found; }
      },
    },
    self: {
      location: {origin: 'https://tscm.test'},
      clients: {claim: async () => {}, matchAll: async () => [{postMessage: message => messages.push(message)}]},
      addEventListener: (name, fn) => { handlers[name] = fn; },
    },
  };
  vm.runInNewContext(source, context);
  const lifecycle = async name => { let promise; handlers[name]({waitUntil: value => { promise = value; }}); await promise; };
  const request = async (path, method = 'GET', mode = 'navigate', origin = 'https://tscm.test') => {
    let promise;
    handlers.fetch({request: {url: origin + path, method, mode},
                    respondWith: value => { promise = value; }});
    return promise === undefined ? undefined : await promise;
  };
  return {stores, cache, lifecycle, request, messages,
    offline: () => { online = false; }, online: () => { online = true; },
    response: fn => { reply = fn; }};
}

test('installation caches only static assets, not business pages or login', async () => {
  const h = harness();
  await h.lifecycle('install');
  assert.equal(h.stores.size, 1);
  const assets = [...h.stores.values()][0];
  assert.ok([...assets.keys()].some(path => /^\/static\/js\/pwa(?:\.[a-f0-9]+)?\.js$/.test(path)));
  assert.ok([...assets.keys()].some(path => /^\/static\/js\/mobile-layout(?:\.[a-f0-9]+)?\.js$/.test(path)));
  assert.ok([...assets.keys()].some(path => /^\/static\/icons\/chevron-down(?:\.[a-f0-9]+)?\.svg$/.test(path)));
  assert.ok(!assets.has('/clock/'));
  assert.ok(!assets.has('/accounts/login/'));
});

test('activation deletes only old TSCM-owned caches', async () => {
  const h = harness();
  h.cache('unrelated-app');
  h.cache('tscm-shell-v2');
  h.cache('tscm-docs-v2');
  await h.lifecycle('install');
  await h.lifecycle('activate');
  assert.ok(h.stores.has('unrelated-app'));
  assert.ok(!h.stores.has('tscm-shell-v2'));
  assert.ok(!h.stores.has('tscm-docs-v2'));
});

test('prepared clock launches from app root offline with a visible stale marker', async () => {
  const h = harness();
  h.response(input => new Response('<section data-clock data-endpoint="/punch/"><p data-clock-offline hidden>Stale</p></section>',
    {headers: {'X-TSCM-Offline-Clock': '1', 'Content-Type': 'text/html'}}));
  await h.request('/clock/?old=1');
  h.offline();
  const response = await h.request('/?source=pwa');
  assert.equal(response.status, 200);
  const body = await response.text();
  assert.ok(body.includes('data-clock-stale="true"'));
  assert.ok(!body.includes('data-clock-offline hidden'));
  assert.equal(h.messages.length, 1);
  assert.equal((await h.request('/payroll/')).status, 503);
});

test('unprepared launch returns honest offline response, not a cached login', async () => {
  const h = harness(); h.offline();
  const response = await h.request('/');
  assert.equal(response.status, 503);
  assert.ok((await response.text()).includes('No saved clock page'));
});

test('logout and company switch remove saved documents without touching unrelated storage', async () => {
  for (const path of ['/accounts/logout/', '/organizations/select/']) {
    const h = harness();
    h.cache('unrelated-app');
    await h.request('/clock/');
    assert.ok([...h.stores.keys()].some(name => name.startsWith('tscm-docs-')));
    await h.request(path, 'POST');
    assert.ok(![...h.stores.keys()].some(name => name.startsWith('tscm-docs-')));
    assert.ok(h.stores.has('unrelated-app'));
    h.offline();
    assert.equal((await h.request('/clock/')).status, 503);
  }
});

test('redirected or unauthorized clock responses clear old officer documents', async () => {
  const h = harness();
  await h.request('/clock/');
  h.response(() => new Response('sign in', {status: 200}));
  await h.request('/clock/');
  h.offline();
  assert.equal((await h.request('/clock/')).status, 503);
});

test('cross-origin and punch writes are not intercepted', async () => {
  const h = harness();
  assert.equal(await h.request('/clock/', 'GET', 'navigate', 'https://other.test'), undefined);
  assert.equal(await h.request('/api/punch/', 'POST'), undefined);
});

test('offline shell resources fallback correctly and uncached APIs stay network-only', async () => {
  const h = harness();
  await h.lifecycle('install'); h.offline();
  const script = [...[...h.stores.values()][0].keys()].find(path => /^\/static\/js\/app(?:\.[a-f0-9]+)?\.js$/.test(path));
  assert.ok(script);
  assert.equal(await (await h.request(script, 'GET', 'cors')).text(), 'asset');
  assert.equal(await h.request('/api/private', 'GET', 'cors'), undefined);
});
