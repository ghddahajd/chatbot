const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function node() {
  const classes = new Set();
  return {
    classList: {
      add: name => classes.add(name), remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
    },
    focus() {},
    appendChild() {}, querySelectorAll: () => [],
  };
}

const storage = new Map();
const document = { currentScript: null, hidden: false, createElement: node, createTextNode: node };
let Widget;
vm.runInNewContext(readFileSync(path.join(__dirname, '../widget.js'), 'utf8'), {
  document, HTMLElement: class {}, URL, console,
  window: {
    location: { origin: 'http://localhost' }, addEventListener() {},
    customElements: { get() {}, define(name, value) { Widget = value; } },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
  },
});

function widget() {
  const w = Object.create(Widget.prototype);
  w.state = { companyId: 'clinic', sessionId: 'dialog', open: false, status: 'HUMAN_ACTIVE', operatorMessageCount: 0, operatorReadCount: 0, operatorNoticeDismissed: false, widgetConfig: {} };
  w.el = Object.fromEntries(['operatorNotice', 'unread', 'launcher', 'messages', 'panel', 'inp'].map(key => [key, node()]));
  w.dismissTeaser = () => { w.promotionHidden = true; };
  w.scrollBottom = () => {};
  w.trackEvent = () => {};
  w.clearGreeting = () => {};
  w.updateBookBar = () => {};
  return w;
}

test('closed chat: notice and dot, repeated messages keep one notice, no message text stored', () => {
  storage.clear();
  const w = widget();
  w.recordOperatorMessage();
  w.recordOperatorMessage();
  assert.equal(w.state.operatorMessageCount, 2);
  assert.equal(w.el.operatorNotice.classList.contains('visible'), true);
  assert.equal(w.el.unread.classList.contains('visible'), true);
  assert.equal(w.promotionHidden, true);
  assert.equal(w.canShowTeaser(), false);
});

test('opening reads messages; a later reply after closing can notify again', () => {
  storage.clear();
  const w = widget();
  w.recordOperatorMessage();
  w.toggle();
  assert.equal(w.el.operatorNotice.classList.contains('visible'), false);
  assert.equal(w.state.operatorReadCount, 1);
  w.toggle();
  w.recordOperatorMessage();
  assert.equal(w.el.operatorNotice.classList.contains('visible'), true);
});

test('dismissal survives navigation and further replies until chat is opened', () => {
  storage.clear();
  const w = widget();
  w.recordOperatorMessage();
  w.state.operatorNoticeDismissed = true;
  w.saveOperatorReadState();
  const next = widget();
  next.loadOperatorReadState();
  next.renderHistory([{ role: 'operator', text: 'private' }]);
  next.recordOperatorMessage();
  assert.equal(next.el.operatorNotice.classList.contains('visible'), false);
  assert.equal(next.el.unread.classList.contains('visible'), true);
  assert.deepEqual(JSON.parse(storage.get(next.operatorReadKey())), { read: 0, dismissed: true });
});

test('read replies stay read on another page; an unseen reply restores notice', () => {
  storage.clear();
  const w = widget();
  w.state.open = true;
  w.recordOperatorMessage();
  const next = widget();
  next.loadOperatorReadState();
  next.renderHistory([{ role: 'operator' }]);
  assert.equal(next.el.operatorNotice.classList.contains('visible'), false);
  next.renderHistory([{ role: 'operator' }, { role: 'operator' }]);
  assert.equal(next.el.operatorNotice.classList.contains('visible'), true);
});

test('hidden tab does not mark reply read even with panel open', () => {
  storage.clear();
  const w = widget();
  w.state.open = true;
  document.hidden = true;
  w.recordOperatorMessage();
  assert.equal(w.state.operatorReadCount, 0);
  document.hidden = false;
  w.updateOperatorNotice();
  assert.equal(w.state.operatorReadCount, 1);
});

test('read markers are isolated by clinic and session; reset hides notice', () => {
  storage.clear();
  const w = widget();
  w.state.open = true;
  w.recordOperatorMessage();
  const other = widget();
  other.state.sessionId = 'another';
  other.loadOperatorReadState();
  assert.equal(other.state.operatorReadCount, 0);
  other.state.sessionId = 'dialog';
  other.state.companyId = 'another';
  other.loadOperatorReadState();
  assert.equal(other.state.operatorReadCount, 0);
  w.clearLocalSession();
  assert.equal(w.state.operatorMessageCount, 0);
  assert.equal(w.el.operatorNotice.classList.contains('visible'), false);
});

test('legacy history becomes a baseline instead of a false new-reply notification', () => {
  storage.clear();
  const w = widget();
  w.loadOperatorReadState(2);
  w.renderHistory([{ role: 'operator' }, { role: 'operator' }]);
  assert.equal(w.el.operatorNotice.classList.contains('visible'), false);
  w.recordOperatorMessage();
  assert.equal(w.el.operatorNotice.classList.contains('visible'), true);
});

test('only a live operator message triggers the notice, not bot replies or joining', () => {
  storage.clear();
  const w = widget();
  w.addMsg('system', 'Администратор подключился');
  w.addMsg('assistant', 'Ответ бота');
  assert.equal(w.state.operatorMessageCount, 0);
  assert.equal(w.el.operatorNotice.classList.contains('visible'), false);
  w.addMsg('operator', 'Здравствуйте');
  assert.equal(w.state.operatorMessageCount, 1);
  assert.equal(w.el.operatorNotice.classList.contains('visible'), true);
});
