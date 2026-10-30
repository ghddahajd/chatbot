const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function node() {
  const classes = new Set();
  return {
    children: [], value: '', handlers: {},
    get childElementCount() { return this.children.length; },
    classList: {
      add: name => classes.add(name), remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
    },
    focus() {},
    appendChild(child) { this.children.push(child); child.parent = this; },
    replaceChildren(...children) { this.children = []; children.forEach(child => this.appendChild(child)); },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    querySelectorAll(selector) {
      return this.children.flatMap(child => [child, ...child.querySelectorAll('*')])
        .filter(child => selector === '*' || selector.split(', ').some(s => (child.className || '').split(' ').includes(s.slice(1))));
    },
    addEventListener(event, handler) { this.handlers[event] = handler; },
    click() { this.handlers.click?.(); },
  };
}

const storage = new Map();
const document = { currentScript: null, hidden: false, createElement: node, createTextNode: node };
let Widget;
let timerId = 0;
const timers = new Map();
vm.runInNewContext(readFileSync(path.join(__dirname, '../widget.js'), 'utf8'), {
  document, HTMLElement: class {}, URL, console,
  window: {
    location: { origin: 'http://localhost' }, addEventListener() {},
    setTimeout(callback, delay) { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
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

function reminderWidget() {
  storage.clear(); timers.clear(); document.hidden = false;
  const w = widget();
  Object.assign(w.state, { open: true, status: 'AI_ACTIVE', idleReminderEligible: true });
  Object.assign(w.state.widgetConfig, { idle_reminder: 'on', quick_booking: 'on', idle_reminder_seconds: '90', idle_reminder_text: 'Предложение звонка' });
  w.events = [];
  w.trackEvent = event => w.events.push(event);
  return w;
}

test('reminder waits 90 seconds, activity restarts timer, only one display per session', () => {
  const w = reminderWidget();
  w.scheduleIdleReminder();
  const first = w._idleReminderTimer;
  assert.equal(timers.get(first).delay, 90000);
  w.scheduleIdleReminder();
  assert.equal(timers.has(first), false);
  timers.get(w._idleReminderTimer).callback();
  assert.equal(w.el.messages.querySelectorAll('.idle-reminder').length, 1);
  w.scheduleIdleReminder();
  assert.equal(timers.size, 0);
  assert.deepEqual(w.events, ['idle-reminder-shown']);
  const next = widget();
  Object.assign(next.state, w.state, { idleReminderShown: false });
  assert.equal(next.canShowIdleReminder(), false);
});

test('reminder is suppressed while closed, hidden, typing, sending, after lead, with form, or with operator', () => {
  for (const update of [{ open: false }, { sending: true }, { leadLeft: true }, { status: 'WAITING_OPERATOR' }, { status: 'HUMAN_ACTIVE' }, { status: 'CLOSED' }, { status: 'UNAVAILABLE' }]) {
    const w = reminderWidget(); Object.assign(w.state, update);
    w.scheduleIdleReminder(); assert.equal(timers.size, 0);
  }
  const w = reminderWidget();
  w.el.inp.value = 'пишу'; assert.equal(w.canShowIdleReminder(), false);
  w.el.inp.value = ''; document.hidden = true; assert.equal(w.canShowIdleReminder(), false);
  document.hidden = false;
  const form = node(); form.className = 'booking-card'; w.el.messages.appendChild(form);
  assert.equal(w.canShowIdleReminder(), false);
});

test('reminder buttons dismiss locally or request callback, tracked once and never reprompted', () => {
  for (const [index, event] of [[0, 'callback'], [1, 'later']]) {
    const w = reminderWidget();
    const sent = []; w.sendText = text => sent.push(text);
    w.showIdleReminder();
    w.el.messages.querySelector('.idle-reminder-actions').querySelectorAll('.quick-btn')[index].click();
    assert.equal(w.el.messages.querySelector('.idle-reminder'), null);
    assert.equal(w.el.messages.querySelector('.idle-reminder-actions'), null);
    if (event === 'later') {
      assert.equal(w.el.messages.querySelectorAll('.assistant').length, 1);
      assert.equal(w.el.messages.querySelectorAll('.system').length, 1);
      w.addMsg('user', 'Ещё вопрос');
      assert.equal(w.el.messages.querySelectorAll('.assistant').length, 1);
      assert.equal(w.el.messages.querySelectorAll('.system').length, 1);
    }
    assert.deepEqual(sent, index === 0 ? ['Перезвоните мне'] : []);
    assert.deepEqual(w.events, ['idle-reminder-shown', 'idle-reminder-' + event]);
    w.scheduleIdleReminder(); assert.equal(timers.size, 0);
  }
});

test('reminder uses standard buttons outside the bubble and preserves previous directions', () => {
  const w = reminderWidget();
  const previous = node(); previous.className = 'quick-actions';
  w.el.messages.appendChild(previous);
  w.showIdleReminder();
  const bubble = w.el.messages.querySelector('.idle-reminder');
  const actions = w.el.messages.querySelector('.idle-reminder-actions');
  assert.equal(bubble.querySelector('.quick-actions'), null);
  assert.equal(actions.parent, w.el.messages);
  const buttons = actions.querySelectorAll('.quick-btn');
  assert.equal(buttons[0].className, 'quick-btn primary');
  assert.equal(buttons[1].className, 'quick-btn');
  w.cancelIdleReminder(true);
  assert.equal(w.el.messages.querySelector('.idle-reminder-actions'), null);
  assert.equal(w.el.messages.querySelector('.quick-actions'), previous);
});

test('ordinary quick actions keep their first primary button and plain groups stay outlined', () => {
  const w = reminderWidget();
  w.addQuickActions(['Записаться', 'Лицо']);
  let buttons = w.el.messages.querySelectorAll('.quick-btn');
  assert.equal(buttons[0].className, 'quick-btn primary');
  assert.equal(buttons[1].className, 'quick-btn');
  w.addQuickActions(['Позвонить'], { plain: true });
  buttons = w.el.messages.querySelectorAll('.quick-btn');
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].className, 'quick-btn');
});

test('new user message cancels pending reminder; disabled setting prevents scheduling', () => {
  const w = reminderWidget();
  w.scheduleIdleReminder();
  w.addMsg('user', 'Ещё вопрос');
  assert.equal(timers.size, 0);
  w.addMsg('assistant', 'Ответ');
  assert.equal(timers.size, 1);
  w.state.widgetConfig.idle_reminder = '';
  w.scheduleIdleReminder();
  assert.equal(timers.size, 0);
});
