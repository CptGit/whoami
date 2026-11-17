const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync('static/index.html', 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];

function client({autoConnect = true, mode = 'join'} = {}) {
    const elements = new Map();
    function element(id) {
        const classes = new Set(id === 'game-screen' ? ['hidden'] : []);
        if (!elements.has(id)) elements.set(id, {
            id, value: '', textContent: '', innerText: '', innerHTML: '', disabled: false,
            classList: {
                add(...values) { values.forEach(value => classes.add(value)); },
                remove(...values) { values.forEach(value => classes.delete(value)); },
                contains(value) { return classes.has(value); },
                toggle(value, force = !classes.has(value)) { if (force) classes.add(value); else classes.delete(value); },
            },
            setAttribute(key, value) { this[key] = value; },
            removeAttribute(key) { delete this[key]; },
            showModal() { this.open = true; }, close() { this.open = false; },
            addEventListener() {},
            append() {}, replaceChildren() {},
        });
        return elements.get(id);
    }
    const listeners = {};
    const timers = new Map();
    const intervals = new Map();
    const sockets = [];
    const responses = new Map([
        ['/api/rooms', {rooms: []}],
        ['/api/libraries', {libraries: ['默认历史人物']}],
    ]);
    const fetchCalls = [];
    const storage = new Map();
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
        querySelector: selector => element(selector),
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
        localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
        fetch: async url => {
            fetchCalls.push(url);
            const response = responses.get(url);
            if (response instanceof Error) throw response;
            return {ok: true, json: async () => response};
        },
    });
    vm.runInContext(source, context);
    const run = code => vm.runInContext(code, context);
    element('room-id').value = 'room / one';
    element('user-name').value = 'A';
    element('library-select').value = '默认历史人物';
    if (autoConnect) {
        run(`connect('${mode}')`);
        sockets[0].open();
    }
    return {
        run, sockets, element, timers, intervals, listeners, document, responses, fetchCalls, storage,
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
        room_name: '一起猜猜看', host_id: 'a', max_players: 8,
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

function lobbyRoom(overrides = {}) {
    return {
        id: '123456', name: '一起猜猜看', host_name: 'A', library_name: '默认历史人物',
        player_count: 2, online_count: 2, max_players: 8, started: false, joinable: true,
        ...overrides,
    };
}

test('browses live rooms without joining and filters by availability, status, and search', async () => {
    const c = client({autoConnect: false});
    c.responses.set('/api/rooms', {rooms: [
        lobbyRoom(),
        lobbyRoom({id: '234567', name: '动物世界', started: true, joinable: false}),
        lobbyRoom({id: '345678', name: '满员小队', player_count: 8, joinable: false}),
    ]});
    await c.run('refreshRooms()');
    assert.equal(c.sockets.length, 0);
    assert.match(c.element('lobby-summary').textContent, /3 个房间 · 6 位玩家在线/);
    assert.match(c.element('rooms-list').innerHTML, /动物世界/);
    c.run("setRoomFilter('waiting')");
    assert.match(c.element('rooms-list').innerHTML, /一起猜猜看/);
    assert.doesNotMatch(c.element('rooms-list').innerHTML, /动物世界|满员小队/);
    c.run("setRoomFilter('playing')");
    assert.match(c.element('rooms-list').innerHTML, /动物世界/);
    assert.match(c.element('rooms-list').innerHTML, /disabled/);
    c.run("setRoomFilter('all')");
    c.element('room-search').value = '345678';
    c.run('renderRooms()');
    assert.match(c.element('rooms-list').innerHTML, /满员小队/);
    assert.doesNotMatch(c.element('rooms-list').innerHTML, /一起猜猜看|动物世界/);
    c.element('room-search').value = '不存在';
    c.run('renderRooms()');
    assert.equal(c.element('lobby-empty').classList.contains('hidden'), false);
    assert.match(c.element('empty-title').textContent, /没有找到/);
});

test('room names, host names, library names, and room IDs cannot inject lobby markup', async () => {
    const c = client({autoConnect: false});
    c.responses.set('/api/rooms', {rooms: [lobbyRoom({
        id: '\"><img src=x onerror=alert(1)>', name: '<svg onload=alert(1)>',
        host_name: '<script>alert(1)</script>', library_name: '<b>角色库</b>',
    })]});
    await c.run('refreshRooms()');
    const html = c.element('rooms-list').innerHTML;
    assert.doesNotMatch(html, /<img|<svg|<script|<b>角色库/);
    assert.match(html, /&lt;svg/);
    assert.match(html, /data-room-id="&quot;&gt;&lt;img/);
});

test('failed lobby refresh preserves the last rooms and can recover on the next refresh', async () => {
    const c = client({autoConnect: false});
    c.responses.set('/api/rooms', {rooms: [lobbyRoom()]});
    await c.run('refreshRooms()');
    c.responses.set('/api/rooms', new Error('offline'));
    await c.run('refreshRooms()');
    assert.match(c.element('rooms-list').innerHTML, /一起猜猜看/);
    assert.match(c.element('lobby-update').textContent, /更新失败/);
    assert.equal(c.element('refresh-rooms').disabled, false);
    c.responses.set('/api/rooms', {rooms: []});
    await c.run('refreshRooms()');
    assert.equal(c.element('rooms-list').innerHTML, '');
    assert.match(c.element('empty-title').textContent, /大厅还很安静/);
});

test('creating a room sends its settings once and reconnects by joining the same room', () => {
    const c = client({autoConnect: false});
    c.element('room-name').value = '周末 & 好朋友';
    c.element('room-capacity').value = '4';
    c.run('createRoom()');
    const first = c.sockets[0];
    const url = new URL(first.url);
    assert.equal(url.searchParams.get('mode'), 'create');
    assert.equal(url.searchParams.get('title'), '周末 & 好朋友');
    assert.equal(url.searchParams.get('capacity'), '4');
    assert.match(url.pathname, /\/ws\/\d{6}\/A$/);
    c.run('createRoom()');
    assert.equal(c.sockets.length, 1);
    first.open();
    first.receive(state({started: false}));
    assert.equal(c.element('create-modal').open, false);
    first.close();
    c.fireTimer(1000);
    const reconnect = new URL(c.sockets[1].url);
    assert.equal(reconnect.searchParams.get('mode'), 'join');
    assert.equal(reconnect.pathname, url.pathname);
});

test('joining by room number rejects missing rooms without creating one or retrying', () => {
    const c = client({autoConnect: false});
    c.run('openJoinRoom()');
    c.element('room-id').value = '999999';
    c.element('join-user-name').value = '朋友';
    c.run('joinSelectedRoom()');
    const socket = c.sockets[0];
    assert.equal(new URL(socket.url).searchParams.get('mode'), 'join');
    socket.open();
    socket.receive({type: 'ERROR', message: '这个房间已解散'});
    assert.equal(c.run('shouldReconnect'), false);
    assert.equal(c.element('join-modal').open, true);
    assert.equal(c.element('join-submit').disabled, false);
    assert.match(c.element('join-error').textContent, /已解散/);
    assert.equal(c.element('game-screen').classList.contains('hidden'), true);
    socket.receive(state());
    assert.equal(c.run('currentState'), null);
});

test('only the host can start after at least two connected players are ready', () => {
    const c = client();
    const single = state({started: false});
    single.players = [single.players[0]];
    c.sockets[0].receive(single);
    assert.equal(c.element('start-btn').disabled, true);
    c.sockets[0].receive(state({revision: 2, started: false, host_id: 'b'}));
    assert.equal(c.element('start-btn').classList.contains('hidden'), true);
    assert.equal(c.element('start-btn').disabled, true);
    c.sockets[0].receive(state({revision: 3, started: false}));
    assert.equal(c.element('start-btn').classList.contains('hidden'), false);
    assert.equal(c.element('start-btn').disabled, false);
});

test('leaving a game confirms first, returns to the lobby, and ignores the old socket', () => {
    const c = client();
    const old = c.sockets[0];
    old.receive(state());
    c.run('requestLeave()');
    assert.equal(c.element('leave-modal').open, true);
    assert.ok(old.sent.every(message => message.action !== 'LEAVE_ROOM'));
    c.run('leaveRoom()');
    assert.equal(old.sent.at(-1).action, 'LEAVE_ROOM');
    assert.equal(c.run('shouldReconnect'), false);
    assert.equal(c.element('game-screen').classList.contains('hidden'), true);
    assert.equal(c.element('login-screen').classList.contains('hidden'), false);
    assert.equal(c.element('leave-modal').open, false);
    old.receive(state({revision: 10}));
    c.listeners.visibilitychange();
    assert.equal(c.sockets.length, 1);
    assert.equal(c.run('currentState'), null);
});

test('canceling an entry closes its socket and does not reconnect in the background', () => {
    const c = client();
    c.run('closeEntryDialogs()');
    assert.equal(c.sockets[0].sent.at(-1).action, 'LEAVE_ROOM');
    assert.equal(c.run('entryPending'), false);
    assert.equal(c.run('shouldReconnect'), false);
    c.listeners.visibilitychange();
    assert.equal(c.sockets.length, 1);
});
