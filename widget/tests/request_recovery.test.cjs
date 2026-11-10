const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const { webcrypto } = require('node:crypto');

function node() {
  const classes = new Set();
  return {
    children: [], value: '', handlers: {}, disabled: false,
    set innerHTML(html) {
      this.children = [];
      // Only the card's named controls are needed; retain the phone's wrapper.
      for (const match of html.matchAll(/class="([^"]+)"/g)) {
        const child = node(); child.className = match[1]; this.appendChild(child);
      }
      const phone = this.querySelector('.bc-phone');
      if (phone) phone.parentElement = this.querySelector('.bc-phone-wrap');
    },
    get childElementCount() { return this.children.length; },
    classList: {
      add: name => classes.add(name), remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
    },
    focus() {}, setAttribute() {},
    appendChild(child) { this.children.push(child); child.parent = this; return child; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    querySelectorAll(selector) {
      return this.children.flatMap(child => [child, ...child.querySelectorAll('*')])
        .filter(child => selector === '*' || selector.split(', ').some(s => (child.className || '').split(' ').includes(s.slice(1))));
    },
    addEventListener(event, handler) { this.handlers[event] = handler; },
    click() { if (!this.disabled) return this.handlers.click?.(); },
  };
}

function setup(handler, crypto = webcrypto, localStorage) {
  let Widget, now = 0, timerId = 0;
  const timers = new Map(), calls = [], stored = [];
  const setTimeout = (callback, delay) => { timers.set(++timerId, { at: now + delay, callback }); return timerId; };
  const clearTimeout = id => timers.delete(id);
  vm.runInNewContext(readFileSync(path.join(__dirname, '../widget.js'), 'utf8'), {
    document: { currentScript: null, createElement: node, createTextNode: node, removeEventListener() {} },
    HTMLElement: class {}, URL, console, AbortController, Uint8Array, setTimeout, clearTimeout,
    window: {
      crypto, location: { origin: 'http://localhost', pathname: '/original' }, addEventListener() {},
      setTimeout, clearTimeout,
      customElements: { get() {}, define(name, value) { Widget = value; } },
      localStorage: localStorage || { setItem: (k, v) => stored.push([k, v]), getItem: () => null, removeItem() {} },
    },
    fetch: async (url, options) => {
      const call = { url, body: JSON.parse(options.body), signal: options.signal };
      calls.push(call);
      return handler(call, calls.length);
    },
  });
  const w = Object.create(Widget.prototype);
  w.state = { companyId: 'clinic', sessionId: 'original-session', status: 'AI_ACTIVE', widgetConfig: { quick_booking: 'on' } };
  w.el = Object.fromEntries(['messages', 'inp', 'send', 'operatorNotice', 'unread', 'dot', 'statusLabel', 'composer', 'closedNote'].map(k => [k, node()]));
  w.events = []; w.trackEvent = event => w.events.push(event);
  for (const name of ['acceptConsent', 'updateBookBar', 'scheduleIdleReminder', 'cancelIdleReminder', 'scrollBottom',
    'updateWaitCard', 'connectWS', 'loadOperatorReadState', 'dismissTeaser', 'clearGreeting']) w[name] = () => {};
  w.showTyping = () => {};
  async function flush() { for (let i = 0; i < 30; i++) await Promise.resolve(); }
  async function tick(ms) {
    await flush();
    const target = now + ms;
    while (true) {
      const next = [...timers].filter(([, v]) => v.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      now = next[1].at; timers.delete(next[0]); next[1].callback(); await flush();
    }
    now = target; await flush();
  }
  return { w, calls, timers, stored, tick, flush };
}
const ok = data => ({ ok: true, status: 200, json: async () => data });
const response = { session_id: 'new-session', status: 'AI_ACTIVE', answer: 'Получено', lead_created: true, quick_actions: [] };
const lost = () => { throw new TypeError('lost response'); };
const unknown = durable => ({ state: 'unknown', durable, retry_allowed: durable });
const messageCalls = h => h.calls.filter(c => !c.url.endsWith('/result'));

test('lost response checks the immutable original body, applies done and callback once, stores only session', async () => {
  const h = setup(c => c.url.endsWith('/result') ? ok({ state: 'done', response, durable: true }) : lost());
  let successes = 0;
  const pending = h.w.sendText('private phone', { bookingDay: 'Завтра', onSuccess: () => successes++ });
  await h.tick(1000); await pending;
  assert.equal(h.calls.length, 2);
  assert.deepEqual(h.calls[0].body, h.calls[1].body);
  assert.match(h.calls[0].body.request_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(h.calls[1].body.session_id, 'original-session');
  assert.equal(h.w.state.sessionId, 'new-session');
  h.w.applyChatResponse(h.w._request, response);
  assert.equal(successes, 1);
  assert.equal(h.w.el.messages.querySelectorAll('.user').length, 1);
  assert.equal(h.w.el.messages.querySelectorAll('.assistant').length, 1);
  assert.equal(h.w.el.messages.querySelector('.request-recovery'), null);
  assert.equal(h.stored.length, 1);
  assert.equal(h.stored[0][1], 'new-session');
});

test('durable unknown permits one automatic resend and one fresh safe resend per explicit click', async () => {
  const h = setup(c => c.url.endsWith('/result') ? ok(unknown(true)) : lost());
  const pending = h.w.sendText('hello');
  await h.tick(11000); await pending;
  const sends = messageCalls(h);
  assert.equal(sends.length, 2);
  assert.deepEqual(sends[1].body, { ...sends[0].body, retry: true });
  const btn = h.w.el.messages.querySelector('.quick-btn');
  assert.equal(btn.textContent, 'Повторить отправку');
  btn.click(); await h.tick(11000);
  assert.equal(messageCalls(h).length, 3);
  assert.deepEqual(messageCalls(h)[2].body, sends[1].body);
  assert.equal(h.w.el.messages.querySelectorAll('.user').length, 1);
});

test('memory unknown, uncertain, and known processing never resend and checks are bounded', async () => {
  for (const state of [unknown(false), { state: 'uncertain', durable: true, retry_allowed: false }, { state: 'processing', durable: true }]) {
    const h = setup(c => c.url.endsWith('/result') ? ok(state) : lost());
    const pending = h.w.sendText('hello'); await h.tick(20000); await pending;
    assert.equal(messageCalls(h).length, 1);
    assert.equal(h.calls.length, 4);
    assert.equal(h.timers.size, 0);
    assert.equal(h.w.el.messages.querySelector('.quick-btn').textContent, 'Проверить отправку');
  }
  let checks = 0;
  const h = setup(c => c.url.endsWith('/result') ? ok(++checks === 1 ? { state: 'processing' } : unknown(true)) : lost());
  h.w.sendText('hello'); await h.tick(20000);
  assert.equal(messageCalls(h).length, 1);
});

test('initial 202 processing never resends even if later durable unknown', async () => {
  const h = setup(c => c.url.endsWith('/result') ? ok(unknown(true)) : { ...ok({ state: 'processing', durable: true }), status: 202 });
  h.w.sendText('hello'); await h.tick(11000);
  assert.equal(messageCalls(h).length, 1);
});

test('all initial 4xx are final; old result route 404 offers only another status check', async () => {
  for (const status of [400, 401, 403, 404, 409, 422, 429]) {
    const h = setup(() => ({ ok: false, status }));
    await h.w.sendText('hello'); await h.tick(20000);
    assert.equal(h.calls.length, 1);
    assert.equal(h.w.el.messages.querySelector('.quick-btn'), null);
  }
  const h = setup(c => c.url.endsWith('/result') ? { ok: false, status: 404 } : lost());
  h.w.sendText('hello'); await h.tick(11000);
  assert.equal(h.calls.length, 2);
  assert.equal(h.w.el.messages.querySelector('.quick-btn').textContent, 'Проверить отправку');
  assert.match(h.w.el.messages.children.at(-2).textContent, /недоступна/);
  h.w.el.messages.querySelector('.quick-btn').click(); await h.tick(11000);
  assert.equal(messageCalls(h).length, 1);
});

test('5xx recovers, status authorization failures never resend', async () => {
  const h = setup(c => c.url.endsWith('/result') ? ok({ state: 'done', response }) : { ok: false, status: 503 });
  h.w.sendText('hello'); await h.tick(1000);
  assert.equal(h.w.state.leadLeft, true);
  for (const status of [401, 403, 409, 422]) {
    const h = setup(c => c.url.endsWith('/result') ? { ok: false, status } : lost());
    h.w.sendText('hello'); await h.tick(11000);
    assert.equal(h.calls.length, 2);
    assert.equal(messageCalls(h).length, 1);
  }
});

test('reset, disconnect, company switch, HUMAN and CLOSED invalidate in-flight responses', async () => {
  for (const stop of [w => w.clearLocalSession(), w => w.disconnectedCallback(), w => { w.state.companyId = 'other'; },
    w => w.setStatus('HUMAN_ACTIVE'), w => w.setStatus('CLOSED')]) {
    let resolve;
    const h = setup(() => new Promise(r => { resolve = r; }));
    const pending = h.w.sendText('hello'); const signal = h.calls[0].signal;
    stop(h.w); resolve(ok(response)); await pending; await h.tick(20000);
    assert.equal(h.w.state.leadLeft || false, false);
    assert.equal(h.w.el.messages.querySelectorAll('.assistant').length, 0);
    assert.equal(h.calls.length, 1);
    if (h.w.state.companyId === 'clinic') assert.equal(signal.aborted, true);
  }
});

test('new submit cancels prior generation, retry UI and stale responses; creates new UUID', async () => {
  let first;
  const h = setup((c, i) => i === 1 ? lost() : i === 2 ? new Promise(r => { first = r; }) : ok(response));
  const old = h.w.sendText('old'); await h.tick(1000);
  const current = h.w.sendText('new');
  await current; first(ok({ state: 'done', response: { ...response, answer: 'stale' } })); await old;
  assert.notEqual(h.calls[0].body.request_id, h.calls[2].body.request_id);
  assert.equal(h.calls[1].signal.aborted, true);
  assert.equal(h.w.el.messages.querySelectorAll('.assistant').length, 1);
  const fallback = setup(c => c.url.endsWith('/result') ? ok(unknown(false)) : lost());
  fallback.w.sendText('old'); await fallback.tick(11000);
  assert.ok(fallback.w.el.messages.querySelector('.request-recovery'));
  fallback.w.sendText('new'); await fallback.flush();
  assert.equal(fallback.w.el.messages.querySelector('.request-recovery'), null);
});

test('WAITING allows cached handoff but forbids resend after AI transitioned; HUMAN uses only WS', async () => {
  const h = setup(c => c.url.endsWith('/result') ? ok(unknown(true)) : lost());
  h.w.sendText('hello'); await h.flush(); h.w.setStatus('WAITING_OPERATOR'); await h.tick(11000);
  assert.equal(messageCalls(h).length, 1);
  const cached = setup(c => c.url.endsWith('/result') ? ok({ state: 'done', response: { ...response, status: 'WAITING_OPERATOR' } }) : lost());
  cached.w.sendText('hello'); await cached.flush(); cached.w.setStatus('WAITING_OPERATOR'); await cached.tick(1000);
  assert.equal(cached.w.state.leadLeft, true);
  const ws = setup(() => { throw Error('HTTP in HUMAN'); });
  ws.w.setStatus('HUMAN_ACTIVE'); let sent;
  ws.w.state.ws = { send: text => { sent = text; } };
  await ws.w.sendText('operator text');
  assert.equal(sent, 'operator text'); assert.equal(ws.calls.length, 0);
});

test('booking card preserves phone/day on failure and counts late manual success once', async () => {
  let done = false;
  const h = setup(c => c.url.endsWith('/result') ? ok(done ? { state: 'done', response } : unknown(false)) : lost());
  h.w.addBookingCard({ days: ['Завтра'], purpose: 'callback' }, []);
  const card = h.w.el.messages.querySelector('.booking-card');
  const phone = card.querySelector('.bc-phone'); phone.value = '9991234567'; phone.handlers.input({});
  card.querySelector('.bc-day').click(); card.querySelector('.bc-submit').click();
  await h.tick(11000);
  assert.equal(h.w.events.includes('booking-card-sent'), false);
  assert.equal(phone.value, '(999) 123-45-67');
  assert.equal(h.w.el.messages.querySelector('.booking-card'), card);
  assert.equal(card.querySelector('.bc-submit').disabled, false);
  assert.equal(h.calls[0].body.booking_day, 'Завтра');
  done = true; h.w.el.messages.querySelector('.request-recovery').children[0].click(); await h.tick(1000);
  h.w.applyChatResponse(h.w._request, response);
  assert.equal(h.w.events.filter(e => e === 'booking-card-sent').length, 1);
  assert.equal(h.w.el.messages.querySelectorAll('.user').length, 1);
});

test('cryptographic UUID fallback and own recovery deadline abort hung fetches', async () => {
  const h = setup(c => {
    if (!c.url.endsWith('/result')) return lost();
    return new Promise((resolve, reject) => c.signal.addEventListener('abort', () => reject(new Error('aborted'))));
  }, { getRandomValues: a => webcrypto.getRandomValues(a) });
  const pending = h.w.sendText('hello'); await h.tick(20000); await pending;
  assert.match(h.calls[0].body.request_id, /^[0-9a-f-]{36}$/);
  assert.equal(h.timers.size, 0);
  assert.ok(h.w.el.messages.querySelector('.request-recovery'));
});

test('initial timeout checks original result; reset during recovery clears waits and aborts status fetch', async () => {
  const h = setup(c => c.url.endsWith('/result') ? ok({ state: 'done', response }) :
    new Promise((resolve, reject) => c.signal.addEventListener('abort', () => reject(new Error('timeout')))));
  const pending = h.w.sendText('timeout'); await h.tick(31000); await pending;
  assert.equal(h.calls[0].signal.aborted, true);
  assert.equal(h.calls.length, 2);
  assert.equal(h.w.state.leadLeft, true);
  for (const duringFetch of [false, true]) {
    let resolve;
    const h = setup(c => c.url.endsWith('/result') ? new Promise(r => { resolve = r; }) : lost());
    const pending = h.w.sendText('reset'); await h.tick(duringFetch ? 1000 : 0);
    const signal = h.calls.at(-1).signal;
    h.w.clearLocalSession();
    if (resolve) resolve(ok({ state: 'done', response }));
    await h.tick(20000); await pending;
    assert.equal(h.timers.size, 0);
    assert.equal(signal.aborted, true);
    assert.equal(h.w.state.leadLeft, false);
    assert.equal(h.w.el.messages.querySelector('.request-recovery'), null);
  }
});

test('booking success through automatic resend counts sent once', async () => {
  const h = setup(c => c.url.endsWith('/result') ? ok(unknown(true)) : c.body.retry ? ok(response) : lost());
  h.w.addBookingCard({ days: [], purpose: 'callback' }, []);
  const card = h.w.el.messages.querySelector('.booking-card');
  const phone = card.querySelector('.bc-phone'); phone.value = '9991234567'; phone.handlers.input({});
  card.querySelector('.bc-submit').click(); await h.tick(1000);
  assert.equal(h.w.events.filter(e => e === 'booking-card-sent').length, 1);
  assert.equal(h.w.el.messages.querySelectorAll('.user').length, 1);
  assert.equal(messageCalls(h).length, 2);
});

test('manual repeat rechecks permission and cannot send on stale safe proof or double click', async () => {
  let safe = true;
  const h = setup(c => c.url.endsWith('/result') ? ok(safe ? unknown(true) : { state: 'uncertain', durable: true, retry_allowed: false }) : lost());
  h.w.sendText('hello'); await h.tick(11000);
  const button = h.w.el.messages.querySelector('.quick-btn');
  assert.equal(button.textContent, 'Повторить отправку');
  safe = false;
  button.click(); button.click(); await h.tick(11000);
  assert.equal(messageCalls(h).length, 2);
  assert.equal(h.w.el.messages.querySelector('.quick-btn').textContent, 'Проверить отправку');
});

test('double click on the same booking card keeps one in-flight POST and request ID', async () => {
  let resolve;
  const h = setup(() => new Promise(r => { resolve = r; }));
  h.w.addBookingCard({ days: [], purpose: 'callback' }, []);
  const card = h.w.el.messages.querySelector('.booking-card');
  const phone = card.querySelector('.bc-phone'); phone.value = '9991234567'; phone.handlers.input({});
  const button = card.querySelector('.bc-submit'); button.click();
  const op = h.w._request;
  button.click();
  assert.equal(h.calls.length, 1);
  assert.equal(h.w._request, op);
  assert.equal(h.calls[0].body.request_id, op.body.request_id);
  assert.equal(h.calls[0].signal.aborted, false);
  assert.equal(h.w.el.messages.querySelectorAll('.user').length, 1);
  resolve(ok(response)); await h.flush();
  assert.equal(h.w.events.filter(e => e === 'booking-card-sent').length, 1);
});

test('HUMAN without WS uses one initial REST request and applies its response', async () => {
  const h = setup(() => ok({ ...response, status: 'HUMAN_ACTIVE' }));
  h.w.setStatus('HUMAN_ACTIVE');
  const data = await h.w.sendText('operator text');
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url.endsWith('/message'), true);
  assert.equal(data.answer, response.answer);
  assert.equal(h.w.el.messages.querySelectorAll('.assistant').length, 1);
  assert.equal(h.w.state.status, 'HUMAN_ACTIVE');
  assert.equal(h.w.state.sending, false);
});

test('HUMAN REST recovery checks only, never resends automatically or manually', async () => {
  const h = setup(c => c.url.endsWith('/result') ? ok(unknown(true)) : lost());
  h.w.setStatus('HUMAN_ACTIVE');
  h.w.sendText('operator text'); await h.tick(11000);
  assert.equal(h.calls.length, 4);
  assert.equal(messageCalls(h).length, 1);
  const button = h.w.el.messages.querySelector('.quick-btn');
  assert.equal(button.textContent, 'Проверить отправку');
  button.click(); await h.tick(11000);
  assert.equal(messageCalls(h).length, 1);
});

test('HUMAN REST response is ignored if WS connects while HTTP is in-flight', async () => {
  let resolve;
  const h = setup(() => new Promise(r => { resolve = r; }));
  h.w.setStatus('HUMAN_ACTIVE');
  const pending = h.w.sendText('operator text');
  assert.equal(h.calls.length, 1);
  h.w.state.ws = { send() {} };
  resolve(ok({ ...response, status: 'WAITING_OPERATOR' })); await pending;
  assert.equal(h.w.el.messages.querySelectorAll('.assistant').length, 0);
  assert.equal(h.w.state.status, 'HUMAN_ACTIVE');
  assert.equal(h.w.state.sending, false);
});

test('same card reuses its operation during recovery and fallback, and never submits again after success', async () => {
  let done = false;
  const h = setup(c => c.url.endsWith('/result') ? ok(done ? { state: 'done', response } : unknown(false)) : lost());
  h.w.addBookingCard({ days: ['Завтра'], purpose: 'callback' }, []);
  const card = h.w.el.messages.querySelector('.booking-card');
  const phone = card.querySelector('.bc-phone'); phone.value = '9991234567'; phone.handlers.input({});
  card.querySelector('.bc-day').click();
  const button = card.querySelector('.bc-submit'); button.click();
  await h.flush(); const op = h.w._request;
  button.click(); await h.tick(11000);
  assert.equal(h.w._request, op);
  assert.equal(messageCalls(h).length, 1);
  done = true; button.click(); await h.tick(1000);
  button.click(); await h.tick(11000);
  assert.equal(messageCalls(h).length, 1);
  assert.equal(h.w.el.messages.querySelectorAll('.user').length, 1);
  assert.equal(h.w.events.filter(e => e === 'booking-card-sent').length, 1);
  assert.ok(h.calls.every(c => c.body.request_id === op.body.request_id));
});

test('changed normalized number or day in the same card starts a new operation', async () => {
  const h = setup(c => c.url.endsWith('/result') ? ok(unknown(false)) : lost());
  h.w.addBookingCard({ days: ['Завтра'], purpose: 'callback' }, []);
  const card = h.w.el.messages.querySelector('.booking-card');
  const phone = card.querySelector('.bc-phone'); phone.value = '9991234567'; phone.handlers.input({});
  const button = card.querySelector('.bc-submit'); button.click(); await h.tick(11000);
  card.querySelector('.bc-day').click(); button.click(); await h.tick(11000);
  phone.value = '9991234568'; phone.handlers.input({ inputType: 'insertFromPaste' }); button.click(); await h.tick(11000);
  const sends = messageCalls(h);
  assert.equal(sends.length, 3);
  assert.equal(new Set(sends.map(c => c.body.request_id)).size, 3);
  assert.equal(sends[1].body.booking_day, 'Завтра');
  assert.equal(sends[2].body.message, '+7 999 123-45-68');
});

test('late FAQ result or error after reset cannot change new session, messages or typing', async () => {
  for (const fail of [false, true]) {
    let resolve, reject;
    const h = setup(c => c.url.endsWith('/faq-answer') ? new Promise((a, b) => { resolve = a; reject = b; }) :
      new Promise(() => {}));
    const old = h.w.sendFaqAnswer({ id: 'faq', question: 'old FAQ' }, node());
    assert.equal(h.calls.length, 1);
    const signal = h.calls[0].signal;
    h.w.clearLocalSession();
    h.w.sendText('new message');
    const typing = node(); h.w.state.typingNode = typing;
    const newOp = h.w._request;
    if (fail) reject(new Error('old FAQ error')); else resolve(ok(response));
    await old; await h.flush();
    assert.equal(signal.aborted, true);
    assert.equal(h.w.state.sessionId, '');
    assert.equal(h.w.state.typingNode, typing);
    assert.equal(h.w.state.sending, true);
    assert.equal(h.w._request, newOp);
    assert.equal(h.w.el.messages.querySelectorAll('.assistant').length, 0);
    h.w.cancelRequest();
  }
});

test('FAQ success applies normally without leaving sending locked', async () => {
  const h = setup(() => ok(response));
  await h.w.sendFaqAnswer({ id: 'faq', question: 'FAQ' }, node());
  assert.equal(h.w.state.sessionId, response.session_id);
  assert.equal(h.w.el.messages.querySelectorAll('.assistant').length, 1);
  assert.equal(h.w.state.sending, false);
});

test('invalid cached responses never mutate or consume the operation; manual check can later succeed', async () => {
  for (const bad of [null, {}, { ...response, session_id: '' }, { ...response, status: 'BAD' },
    { ...response, quick_actions: [{ label: 'bad', type: 'execute', value: 'bad' }] },
    { ...response, booking_form: { contacts: [{}] } }, { ...response, action: {} }]) {
    let done = false, successes = 0;
    const h = setup(c => c.url.endsWith('/result') ? ok({ state: 'done', response: done ? response : bad }) : lost());
    h.w.sendText('hello', { onSuccess: () => successes++ }); await h.tick(11000);
    assert.equal(h.w._request.applied, false);
    assert.equal(h.w.state.sessionId, 'original-session');
    assert.equal(h.w.state.status, 'AI_ACTIVE');
    assert.equal(h.w.state.leadLeft || false, false);
    assert.equal(h.w.el.messages.querySelectorAll('.assistant').length, 0);
    assert.equal(h.stored.length, 0);
    const button = h.w.el.messages.querySelector('.quick-btn');
    assert.equal(button.textContent, 'Проверить отправку');
    done = true; button.click(); await h.tick(1000);
    assert.equal(successes, 1);
    assert.equal(h.w._request.applied, true);
  }
});

test('denied localStorage still renders confirmed initial or cached answer and calls success exactly once', async () => {
  for (const cached of [false, true]) {
    let reads = 0, writes = 0, successes = 0;
    const h = setup(c => c.url.endsWith('/result') ? ok({ state: 'done', response }) : cached ? lost() : ok(response), webcrypto, {
      getItem() { reads++; throw Object.assign(new Error('denied'), { name: 'SecurityError' }); },
      setItem() { writes++; throw Object.assign(new Error('full'), { name: 'QuotaExceededError' }); },
    });
    delete h.w.loadOperatorReadState;
    const pending = h.w.sendText('hello', { onSuccess: () => successes++ });
    if (cached) await h.tick(1000);
    await pending;
    const op = h.w._request;
    h.w.applyChatResponse(op, response);
    assert.equal(op.applied, true);
    assert.equal(h.w.state.sessionId, response.session_id);
    assert.equal(h.w.el.messages.querySelectorAll('.assistant').length, 1);
    assert.equal(h.w.el.messages.querySelector('.assistant').children.at(-1).textContent, response.answer);
    assert.equal(successes, 1);
    assert.equal(h.w.state.sending, false);
    assert.equal(reads, 1);
    assert.equal(writes, 2);
    assert.equal(h.w.el.messages.querySelector('.request-recovery'), null);
  }
});
