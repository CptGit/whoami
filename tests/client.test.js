const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync('static/index.html', 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];

function client() {
    const elements = new Map();
    function element(id) {
        if (!elements.has(id)) elements.set(id, {
            id, value: '', textContent: '', innerText: '', innerHTML: '', disabled: false,
            classList: {add() {}, remove() {}},
            setAttribute(key, value) { this[key] = value; },
            removeAttribute(key) { delete this[key]; },
            append() {}, replaceChildren() {},
        });
        return elements.get(id);
    }
    const listeners = {};
    const timers = new Map();
    const intervals = new Map();
    const sockets = [];
    let timerId = 0;
    let now = 100000;
    class FakeDate extends Date { static now() { return now; } }
    class WebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSING = 2;
        static CLOSED = 3;
        constructor(url) {
            this.url = url;
            this.readyState = WebSocket.CONNECTING;
            this.sent = [];
            sockets.push(this);
        }
        open() { this.readyState = WebSocket.OPEN; this.onopen(); }
        send(message) { this.sent.push(JSON.parse(message)); }
        close() { this.readyState = WebSocket.CLOSED; this.onclose?.({code: 1000}); }
        receive(message) { this.onmessage({data: JSON.stringify(message)}); }
    }
    const document = {
        visibilityState: 'visible',
        getElementById: element,
        querySelectorAll: () => ['ready-btn', 'start-btn', 'question', 'guess-input', 'chat-input'].map(element),
        createElement: () => element(Symbol()),
        createDocumentFragment: () => element(Symbol()),
        addEventListener: (name, callback) => { listeners[name] = callback; },
    };
    const context = vm.createContext({
        document, WebSocket, Date: FakeDate, performance: {now: () => now}, console,
        window: {location: {protocol: 'https:', host: 'game.test'}, addEventListener: (name, callback) => { listeners[name] = callback; }},
        setTimeout: (callback, delay) => { timers.set(++timerId, {callback, delay}); return timerId; },
        clearTimeout: id => timers.delete(id),
        setInterval: (callback, delay) => { intervals.set(++timerId, {callback, delay}); return timerId; },
        clearInterval: id => intervals.delete(id),
        alert: message => { throw new Error(message); },
    });
    vm.runInContext(source, context);
    const run = code => vm.runInContext(code, context);
    element('room-id').value = 'room / one';
    element('user-name').value = 'A';
    element('library-select').value = '默认历史人物';
    run('connect()');
    sockets[0].open();
    return {
        run, sockets, element, timers, intervals, listeners, document,
        advance(ms) { now += ms; },
        fireTimer(delay) {
            const entry = [...timers].find(([, timer]) => timer.delay === delay);
            assert.ok(entry, `expected a ${delay}ms timeout`);
            timers.delete(entry[0]);
            entry[1].callback();
        },
    };
}

function state(overrides = {}) {
    return {
        type: 'ROOM_STATE', revision: 1, room_instance_id: 'room-1', turn_id: 'turn-1',
        server_time: 100, started: true, turn_index: 0,
        players: [
            {id: 'a', name: 'A', ready: true, finished: false, connected: true, isMe: true, targetPerson: '???'},
            {id: 'b', name: 'B', ready: true, finished: false, connected: true, isMe: false, targetPerson: '孙悟空'},
        ],
        current_question: null, question_history: [], events: [], chat_messages: [],
        ...overrides,
    };
}

function guessing(overrides = {}) {
    return {id: 'q-1', asker_id: 'a', asker_name: 'A', text: '我是人吗？', answers: {b: '是'}, stage: 'guessing', guess_deadline: 130, ...overrides};
}

test('waits for room synchronization before sending actions and preserves drafts', () => {
    const c = client();
    c.element('question').value = '我是人吗？';
    c.run('askQuestion()');
    assert.equal(c.element('question').value, '我是人吗？');
    assert.ok(c.sockets[0].sent.every(message => message.action === 'PING'));
    c.sockets[0].receive(state());
    c.run('askQuestion()');
    const message = c.sockets[0].sent.at(-1);
    assert.equal(message.action, 'ASK_QUESTION');
    assert.equal(message.turn_id, 'turn-1');
    assert.equal(c.element('question').value, '');
});

test('shows authoritative remaining time after reconnect without resetting to 30', () => {
    const c = client();
    c.sockets[0].receive(state({server_time: 118, current_question: guessing()}));
    assert.match(c.element('aq-status').textContent, /12 秒/);
    c.advance(4000);
    c.run('updateCountdown()');
    assert.match(c.element('aq-status').textContent, /8 秒/);
    c.run('skipGuess()');
    assert.equal(c.sockets[0].sent.at(-1).question_id, 'q-1');
    const sent = c.sockets[0].sent.length;
    c.advance(9000);
    c.run('updateCountdown()');
    assert.match(c.element('aq-status').textContent, /正在跳过/);
    assert.equal(c.sockets[0].sent.length, sent);
});

test('returning from background replaces a stale socket and ignores all its late events', () => {
    const c = client();
    const old = c.sockets[0];
    old.receive(state());
    c.advance(60000);
    c.listeners.visibilitychange();
    assert.equal(c.sockets.length, 2);
    assert.equal(c.run('connectionReady'), false);
    const replacement = c.sockets[1];
    replacement.open();
    replacement.receive(state({revision: 5, current_question: guessing()}));
    old.receive(state({revision: 10, current_question: null}));
    old.onclose({code: 1000});
    assert.equal(c.run('currentState.revision'), 5);
    assert.equal(c.run('connectionReady'), true);
    assert.equal(c.sockets.length, 2);
    assert.match(replacement.url, /room%20%2F%20one/);
});

test('a half-open connection is replaced if its foreground probe gets no reply', () => {
    const c = client();
    c.sockets[0].receive(state());
    c.advance(1000);
    c.listeners.visibilitychange();
    assert.equal(c.sockets[0].sent.at(-1).action, 'PING');
    c.fireTimer(5000);
    assert.equal(c.sockets.length, 2);
});

test('a closed socket retries with the original room details and restores controls', () => {
    const c = client();
    c.sockets[0].receive(state());
    c.sockets[0].close();
    assert.equal(c.element('question').disabled, true);
    c.element('room-id').value = 'changed';
    c.fireTimer(1000);
    const replacement = c.sockets[1];
    assert.match(replacement.url, /room%20%2F%20one/);
    replacement.open();
    replacement.receive(state({revision: 2}));
    assert.equal(c.element('question').disabled, false);
});

test('a superseded tab stops reconnecting until the player explicitly enters again', () => {
    const c = client();
    c.sockets[0].receive(state());
    c.sockets[0].onclose({code: 4001});
    assert.equal(c.run('shouldReconnect'), false);
    assert.match(c.element('connection-status').textContent, /其他页面/);
    c.listeners.visibilitychange();
    assert.equal(c.sockets.length, 1);
});
