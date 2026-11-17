import asyncio
import json
import time
import unittest
from unittest.mock import patch

from fastapi import WebSocketDisconnect

import main


class Socket:
    """Drive the real endpoint, including delayed disconnects from replaced sockets."""

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.close_code = None

    async def accept(self):
        pass

    async def receive_text(self):
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def send_text(self, value):
        await self.outgoing.put(json.loads(value))

    async def close(self, code=1000):
        # Keep receive pending to simulate the old socket closing after a reconnect.
        self.close_code = code

    async def receive(self):
        return await asyncio.wait_for(self.outgoing.get(), timeout=2)


class GameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.rooms.clear()
        main.connections.clear()
        self.sockets = []
        self.tasks = {}

    async def asyncTearDown(self):
        for socket in self.sockets:
            socket.incoming.put_nowait(WebSocketDisconnect())
        await asyncio.wait_for(asyncio.gather(*self.tasks.values()), timeout=2)
        main.rooms.clear()
        main.connections.clear()

    @property
    def room(self):
        return main.rooms['test']

    async def connect(self, name, room_id='test', expected='ROOM_STATE', **options):
        socket = Socket()
        self.sockets.append(socket)
        self.tasks[socket] = asyncio.create_task(main.websocket_endpoint(socket, room_id, name, **options))
        state = await socket.receive()
        self.assertEqual(state['type'], expected)
        return socket, state

    async def send(self, socket, data):
        socket.incoming.put_nowait(json.dumps(data))
        # The pong is a barrier: the endpoint completed the preceding action/broadcast.
        socket.incoming.put_nowait(json.dumps({'action': 'PING'}))
        while (await socket.receive())['type'] != 'PONG':
            pass

    async def disconnect(self, socket):
        socket.incoming.put_nowait(WebSocketDisconnect())
        await asyncio.wait_for(self.tasks[socket], timeout=2)

    async def leave(self, socket):
        socket.incoming.put_nowait(json.dumps({'action': 'LEAVE_ROOM'}))
        await asyncio.wait_for(self.tasks[socket], timeout=2)

    async def start(self, names=('A', 'B', 'C')):
        sockets = [(await self.connect(name))[0] for name in names]
        for socket in sockets:
            await self.send(socket, {'action': 'TOGGLE_READY'})
        await self.send(sockets[0], {'action': 'START_GAME'})
        self.assertTrue(self.room['started'])
        return sockets

    async def ask(self, socket, text='我是古代人吗？'):
        await self.send(socket, {
            'action': 'ASK_QUESTION', 'text': text, 'turn_id': self.room['turn_id'],
        })
        return self.room['current_question']

    async def answer(self, socket, q, answer='是'):
        await self.send(socket, {
            'action': 'ANSWER_QUESTION', 'answer': answer, 'question_id': q['id'],
        })

    async def guess(self, socket, q, guess=''):
        await self.send(socket, {
            'action': 'MAKE_GUESS', 'guess': guess, 'question_id': q['id'],
        })

    async def test_bystander_disconnect_never_changes_turn(self):
        a, b, c = await self.start()
        turn = self.room['turn_id']
        await self.disconnect(c)
        self.assertEqual(self.room['turn_id'], turn)
        self.assertEqual(self.room['turn_index'], 0)
        main.reconcile_room('test', time.time() + main.RECONNECT_GRACE_SECONDS + 1)
        self.assertEqual(self.room['turn_id'], turn)

    async def test_asker_reconnect_restores_question_answers_identity_and_order(self):
        a, b, c = await self.start()
        q = await self.ask(a)
        await self.answer(b, q)
        player_id = self.room['players'][0]['id']
        turn = self.room['turn_id']
        await self.disconnect(a)
        self.assertIs(self.room['current_question'], q)
        replacement, state = await self.connect('A')
        self.assertEqual(state['current_question']['id'], q['id'])
        self.assertEqual(state['current_question']['answers'], q['answers'])
        self.assertEqual(next(p['id'] for p in state['players'] if p['isMe']), player_id)
        self.assertEqual(self.room['turn_id'], turn)
        self.assertTrue(next(p['connected'] for p in state['players'] if p['isMe']))

    async def test_responder_disconnect_waits_for_grace_then_excludes_them(self):
        a, b, c = await self.start()
        q = await self.ask(a)
        await self.disconnect(c)
        await self.answer(b, q)
        self.assertEqual(q['stage'], 'answering')
        now = time.time() + main.RECONNECT_GRACE_SECONDS + 1
        main.reconcile_room('test', now)
        self.assertEqual(q['stage'], 'guessing')
        self.assertEqual(q['guess_deadline'], now + 30)

    async def test_replaced_socket_cannot_act_or_disconnect_replacement(self):
        a, b, c = await self.start()
        q = await self.ask(a)
        replacement, state = await self.connect('A')
        self.assertEqual(a.close_code, 4001)
        a.incoming.put_nowait(json.dumps({'action': 'SEND_CHAT', 'text': 'old socket'}))
        await asyncio.wait_for(self.tasks[a], timeout=2)
        self.assertEqual(self.room['chat_messages'], [])
        self.assertIs(self.room['current_question'], q)
        self.assertNotIn('disconnected_at', self.room['players'][0])
        self.assertIn(self.room['players'][0]['id'], main.connected_player_ids('test'))
        await self.send(replacement, {'action': 'SEND_CHAT', 'text': 'new socket'})
        self.assertEqual(self.room['chat_messages'][0]['text'], 'new socket')

    async def test_guess_clock_starts_after_answers_and_does_not_restart_on_reconnect(self):
        a, b = await self.start(('A', 'B'))
        q = await self.ask(a)
        self.assertNotIn('guess_deadline', q)
        await self.answer(b, q)
        self.assertAlmostEqual(q['guess_deadline'] - time.time(), 30, delta=1)
        deadline = q['guess_deadline']
        await self.disconnect(a)
        replacement, state = await self.connect('A')
        self.assertEqual(state['current_question']['guess_deadline'], deadline)
        main.reconcile_room('test', deadline - 0.01)
        self.assertIs(self.room['current_question'], q)
        main.reconcile_room('test', deadline)
        self.assertIsNone(self.room['current_question'])
        self.assertEqual(self.room['turn_index'], 1)
        self.assertEqual(self.room['question_history'][0]['outcome'], 'timeout')
        main.reconcile_room('test', deadline + 1)
        self.assertEqual(len(self.room['question_history']), 1)

    async def test_background_clock_skips_without_client_messages(self):
        a, b = await self.start(('A', 'B'))
        q = await self.ask(a)
        await self.answer(b, q)
        q['guess_deadline'] = time.time() - 1
        async with main.lifespan(main.app):
            while True:
                state = await a.receive()
                if state['type'] == 'ROOM_STATE' and state['question_history']:
                    break
        self.assertEqual(state['question_history'][0]['outcome'], 'timeout')
        self.assertEqual(state['turn_index'], 1)

    async def test_stale_and_duplicate_answers_do_not_change_question(self):
        a, b, c = await self.start()
        q = await self.ask(a)
        await self.send(b, {'action': 'ANSWER_QUESTION', 'answer': '否', 'question_id': 'old'})
        self.assertEqual(q['answers'], {})
        await self.answer(b, q, '是')
        await self.answer(b, q, '否')
        self.assertEqual(q['answers'][self.room['players'][1]['id']], '是')

    async def test_stale_guess_cannot_skip_later_question(self):
        a, b = await self.start(('A', 'B'))
        first = await self.ask(a)
        await self.answer(b, first)
        await self.guess(a, first)
        second = await self.ask(b)
        await self.answer(a, second)
        await self.guess(b, first)
        self.assertIs(self.room['current_question'], second)
        self.assertEqual(len(self.room['question_history']), 1)

    async def test_late_guess_is_treated_as_timeout(self):
        a, b = await self.start(('A', 'B'))
        q = await self.ask(a)
        await self.answer(b, q)
        q['guess_deadline'] = time.time() - 1
        await self.guess(a, q, self.room['players'][0]['target'])
        self.assertFalse(self.room['players'][0]['finished'])
        self.assertEqual(self.room['question_history'][0]['outcome'], 'timeout')
        self.assertEqual(self.room['turn_index'], 1)

    async def test_correct_guess_skips_finished_player_and_final_win_resets(self):
        a, b = await self.start(('A', 'B'))
        q = await self.ask(a)
        await self.answer(b, q)
        await self.guess(a, q, self.room['players'][0]['target'])
        self.assertTrue(self.room['players'][0]['finished'])
        q = await self.ask(b)
        await self.answer(a, q)
        await self.guess(b, q)
        self.assertEqual(self.room['turn_index'], 1)
        q = await self.ask(b)
        await self.answer(a, q)
        await self.guess(b, q, self.room['players'][1]['target'])
        self.assertFalse(self.room['started'])
        self.assertIsNone(self.room['current_question'])
        self.assertTrue(all(not p['ready'] for p in self.room['players']))

    async def test_lobby_lists_real_rooms_without_exposing_game_content(self):
        self.assertEqual(await main.get_rooms(), {'rooms': []})
        a, state = await self.connect('A', mode='create', title='朋友的桌子', capacity=4)
        await self.connect('B', mode='join')
        await self.send(a, {'action': 'SEND_CHAT', 'text': '只有房间里才能看到'})
        activity = self.room['last_activity']
        listing = (await main.get_rooms())['rooms']
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0], {
            'id': 'test', 'name': '朋友的桌子', 'host_name': 'A',
            'library_name': '默认历史人物', 'player_count': 2, 'online_count': 2,
            'max_players': 4, 'started': False, 'joinable': True,
        })
        self.assertEqual(self.room['last_activity'], activity)
        self.assertEqual(state['room_name'], '朋友的桌子')
        self.assertEqual(state['host_id'], self.room['players'][0]['id'])
        self.assertNotIn('characters', listing[0])
        self.assertNotIn('chat_messages', listing[0])

    async def test_joining_a_missing_room_never_creates_it(self):
        socket, message = await self.connect('A', mode='join', expected='ERROR')
        await asyncio.wait_for(self.tasks[socket], timeout=2)
        self.assertIn('已解散', message['message'])
        self.assertNotIn('test', main.rooms)

    async def test_creating_an_existing_room_does_not_replace_it(self):
        await self.connect('A', mode='create', title='原房间')
        instance = self.room['instance_id']
        socket, message = await self.connect('B', mode='create', title='替换房间', expected='ERROR')
        await asyncio.wait_for(self.tasks[socket], timeout=2)
        self.assertIn('已被占用', message['message'])
        self.assertEqual(self.room['instance_id'], instance)
        self.assertEqual(self.room['name'], '原房间')
        self.assertEqual(len(self.room['players']), 1)

    async def test_invalid_creation_settings_leave_no_room_behind(self):
        for options in [{'capacity': 1}, {'title': '长' * 31}, {'library': '不存在的角色库'}]:
            with self.subTest(options=options):
                socket, _ = await self.connect('A', mode='create', expected='ERROR', **options)
                await asyncio.wait_for(self.tasks[socket], timeout=2)
                self.assertNotIn('test', main.rooms)

    async def test_full_rooms_reject_new_players_but_allow_reconnection(self):
        a, _ = await self.connect('A', mode='create', capacity=2)
        await self.connect('B', mode='join')
        socket, message = await self.connect('C', mode='join', expected='ERROR')
        await asyncio.wait_for(self.tasks[socket], timeout=2)
        self.assertIn('房间已满', message['message'])
        self.assertFalse((await main.get_rooms())['rooms'][0]['joinable'])
        player_id = self.room['players'][0]['id']
        await self.disconnect(a)
        _, state = await self.connect('A', mode='join')
        self.assertEqual(next(p['id'] for p in state['players'] if p['isMe']), player_id)
        self.assertEqual(len(self.room['players']), 2)

    async def test_started_rooms_are_listed_but_reject_new_players(self):
        await self.start(('A', 'B'))
        listing = (await main.get_rooms())['rooms'][0]
        self.assertTrue(listing['started'])
        self.assertFalse(listing['joinable'])
        socket, message = await self.connect('C', mode='join', expected='ERROR')
        await asyncio.wait_for(self.tasks[socket], timeout=2)
        self.assertIn('正在游戏中', message['message'])
        self.assertEqual(len(self.room['players']), 2)

    async def test_only_host_can_start_and_at_least_two_ready_players_are_required(self):
        a, _ = await self.connect('A', mode='create')
        await self.send(a, {'action': 'TOGGLE_READY'})
        await self.send(a, {'action': 'START_GAME'})
        self.assertFalse(self.room['started'])
        b, _ = await self.connect('B', mode='join')
        await self.send(b, {'action': 'TOGGLE_READY'})
        await self.send(b, {'action': 'START_GAME'})
        self.assertFalse(self.room['started'])
        await self.send(a, {'action': 'START_GAME'})
        self.assertTrue(self.room['started'])

    async def test_explicit_leave_transfers_host_and_last_player_dissolves_room(self):
        a, _ = await self.connect('A', mode='create')
        b, state = await self.connect('B', mode='join')
        b_id = next(p['id'] for p in state['players'] if p['isMe'])
        await self.leave(a)
        self.assertEqual(self.room['host_id'], b_id)
        self.assertEqual([p['name'] for p in self.room['players']], ['B'])
        self.assertEqual((await main.get_rooms())['rooms'][0]['host_name'], 'B')
        await self.leave(b)
        self.assertNotIn('test', main.rooms)
        self.assertNotIn('test', main.connections)
        self.assertEqual(await main.get_rooms(), {'rooms': []})

    async def test_host_disconnect_preserves_grace_then_transfers_host(self):
        a, b = await self.start(('A', 'B'))
        a_id, b_id = [p['id'] for p in self.room['players']]
        await self.disconnect(a)
        self.assertEqual(self.room['host_id'], a_id)
        main.reconcile_room('test', time.time() + main.RECONNECT_GRACE_SECONDS + 1)
        self.assertEqual(self.room['host_id'], b_id)

    async def test_leaving_during_a_question_advances_immediately_and_remaining_player_can_finish(self):
        a, b = await self.start(('A', 'B'))
        question = await self.ask(a)
        await self.leave(a)
        self.assertIsNone(self.room['current_question'])
        self.assertEqual(self.room['turn_index'], 1)
        self.assertEqual(question['outcome'], 'disconnected')
        rejected, _ = await self.connect('A', mode='join', expected='ERROR')
        await asyncio.wait_for(self.tasks[rejected], timeout=2)
        final_question = await self.ask(b)
        self.assertEqual(final_question['stage'], 'guessing')
        await self.guess(b, final_question, self.room['players'][1]['target'])
        self.assertFalse(self.room['started'])
        main.reconcile_room('test')
        self.assertEqual([p['name'] for p in self.room['players']], ['B'])

    async def test_expired_empty_rooms_are_hidden_and_polling_does_not_keep_them_alive(self):
        a, _ = await self.connect('A')
        await self.disconnect(a)
        player = self.room['players'][0]
        player['disconnected_at'] -= main.RECONNECT_GRACE_SECONDS + 1
        self.room['last_activity'] -= main.ROOM_TIMEOUT + 1
        self.assertEqual(await main.get_rooms(), {'rooms': []})
        self.assertNotIn('test', main.rooms)

    async def test_queued_broadcast_cannot_send_a_dissolved_rooms_state_to_a_new_room(self):
        a, first_state = await self.connect('A', mode='create')
        old_lock = self.room['broadcast_lock']
        await old_lock.acquire()
        pending = asyncio.create_task(main.broadcast('test'))
        await asyncio.sleep(0)
        try:
            await self.leave(a)
            b, new_state = await self.connect('B', mode='create')
        finally:
            old_lock.release()
            await pending
        self.assertNotEqual(first_state['room_instance_id'], new_state['room_instance_id'])
        self.assertTrue(b.outgoing.empty())


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_returns_running_commit_identity(self):
        messages = []
        scope = {
            'type': 'http', 'method': 'GET', 'path': '/healthz',
            'query_string': b'', 'headers': [], 'root_path': '',
        }

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message):
            messages.append(message)

        with patch.dict(main.os.environ, {'WHOAMI_RELEASE': 'a' * 40}):
            await main.app(scope, receive, send)
        body = b''.join(m.get('body', b'') for m in messages)
        self.assertEqual(json.loads(body), {'status': 'ok', 'release': 'a' * 40})


if __name__ == '__main__':
    unittest.main()
