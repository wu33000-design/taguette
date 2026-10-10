// Exercise the real event loop with simulated requests and timers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('taguette/static/js/taguette.js', 'utf8');
const events = code.slice(code.indexOf('// null: active now'));

async function test(interval) {
  const requests = [];
  const timers = new Map();
  const applied = [];
  let nextTimer = 1;
  const context = {
    console: {log() {}, error() {}}, Date,
    document: {hasFocus: () => true},
    window: {addEventListener() {}, location: {reload() {}}},
    project_id: 1, version: 'test', last_event: -1,
    setProjectMetadata: value => applied.push(value),
    updateTagCount() {},
    getJSON(url, args) {
      return new Promise((resolve, reject) => requests.push({url, args, resolve, reject}));
    },
    setTimeout(fn, delay) { const id = nextTimer++; timers.set(id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  if(interval) { context.event_poll_interval = interval; }
  vm.createContext(context);
  vm.runInContext(events, context);
  assert.equal(requests.length, 1);
  context.longPollForEvents();
  assert.equal(requests.length, 1, 'do not overlap event requests');
  if(interval) { context.requestEventRefresh(); }
  requests[0].resolve({events: []});
  await Promise.resolve();
  await Promise.resolve();
  assert.equal([...timers.values()][0].delay, 1, 'pending action refreshes immediately');
  const runNext = () => {
    const [id, timer] = timers.entries().next().value;
    timers.delete(id);
    timer.fn();
  };
  runNext();
  requests[1].resolve({events: []});
  await Promise.resolve();
  await Promise.resolve();
  assert.equal([...timers.values()][0].delay, interval ? interval * 1000 : 1);
  if(interval) {
    context.requestEventRefresh();
    assert.equal(timers.size, 1, 'mutation replaces the existing timer');
    assert.equal([...timers.values()][0].delay, 0);
  }
  runNext();
  requests[2].resolve({events: [
    {id: 1, type: 'project_meta', project_name: 'First', description: '', tag_count_changes: {'1': 2}},
    {id: 2, type: 'project_meta', project_name: 'Second', description: ''},
  ]});
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(context.last_event, 2);
  assert.equal(applied.length, 2);
}

(async () => {
  await test(30);
  await test(0);
  console.log('Event scheduling tests passed (short requests and legacy long polling)');
})().catch(error => { console.error(error); process.exitCode = 1; });
