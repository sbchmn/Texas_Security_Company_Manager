const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../../static/js/pwa.js'), 'utf8');

function harness(register) {
  const events = new Map();
  const timers = new Map();
  const messages = [];
  const status = {textContent: ''};
  const context = {
    document: {querySelector: selector => selector === '[data-pwa-status]' ? status : null},
    navigator: {serviceWorker: {register}},
    console: {warn: (...args) => messages.push(args), error: (...args) => messages.push(args)},
    window: {
      isSecureContext: true,
      matchMedia: () => ({matches: false}),
      addEventListener: (name, callback) => events.set(name, callback),
      setTimeout: callback => {timers.set(1, callback); return 1;},
      clearTimeout: id => timers.delete(id),
    },
  };
  vm.runInNewContext(source, context);
  return {events, timers, messages, status};
}

test('tracks an already-installing worker and reports installation failure', async () => {
  const changes = new Map();
  const worker = {
    state: 'installing',
    addEventListener: (name, callback) => changes.set(name, callback),
  };
  const h = harness(async () => ({installing: worker, addEventListener() {}}));
  await h.events.get('load')();
  assert.equal(h.timers.size, 0);
  worker.state = 'redundant';
  changes.get('statechange')();
  assert.match(h.status.textContent, /Offline preparation failed/);
});

test('registration rejection reports an explicit error and clears its deadline', async () => {
  const h = harness(async () => {throw new Error('Certificate rejected');});
  await h.events.get('load')();
  assert.match(h.status.textContent, /Certificate rejected/);
  assert.equal(h.messages.length, 1);
  assert.equal(h.timers.size, 0);
});

test('pending registration has a visible preparation warning', () => {
  const h = harness(() => new Promise(() => {}));
  h.events.get('load')();
  h.timers.get(1)();
  assert.match(h.status.textContent, /has not finished/);
  assert.equal(h.messages.length, 1);
});

test('does not suppress the browser installation prompt without an install button', () => {
  const h = harness(async () => ({addEventListener() {}}));
  let prevented = false;
  h.events.get('beforeinstallprompt')({preventDefault() {prevented = true;}});
  assert.equal(prevented, false);
});

test('clears a pending warning when registration subsequently completes', async () => {
  let complete;
  const h = harness(() => new Promise(resolve => {complete = resolve;}));
  const loading = h.events.get('load')();
  h.timers.get(1)();
  assert.match(h.status.textContent, /has not finished/);
  complete({addEventListener() {}});
  await loading;
  assert.match(h.status.textContent, /registration completed/);
  assert.equal(h.timers.size, 0);
});
