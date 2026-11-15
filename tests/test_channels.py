import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from inspector.storage import Store
from inspector.runtime import Inspector


class ChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_wildcard_captures_tools_and_summaries_in_each_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Inspector(Store(Path(tmp)/'state'), {'*'})
            runtime.loop = asyncio.get_running_loop()
            runtime.queue = asyncio.Queue()
            for channel in ('111', '222'):
                route = dict(channel_id=channel, user_id='2', session_id='s')
                summary = {'codex_reasoning_items': [{'type':'reasoning', 'summary':[{'type':'summary_text', 'text':'summary'}]}]}
                with patch('inspector.runtime.route_for_call', return_value=route):
                    runtime.after_tool('terminal', {}, 'ok')
                    runtime.after_api(assistant_message=summary, api_request_id=channel)
                await asyncio.sleep(0)
                self.assertEqual(runtime.queue.qsize(), 2)
                for _ in range(2):
                    self.assertEqual(runtime.queue.get_nowait()['channel_id'], channel)
            with patch('inspector.runtime.route_for_call', return_value=None):
                runtime.after_tool('terminal', {}, 'not captured')
            await asyncio.sleep(0)
            self.assertTrue(runtime.queue.empty())

    async def test_wildcard_keeps_record_owner_message_and_origin_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state')
            record = store.capture('terminal', {}, 'ok', channel_id='111', user_id='2', session_id='s')
            assert record is not None
            store.bind(record['id'], '333')
            runtime = Inspector(store, {'*'}, owner_aliases=['2','4'])
            for user, channel, message, allowed in [(2,111,333,True),(4,111,333,True),(5,111,333,False),(2,222,333,False),(2,111,334,False)]:
                with self.subTest(user=user, channel=channel, message=message):
                    interaction = SimpleNamespace(data={'custom_id':f"hti:{record['id']}:0"}, user=SimpleNamespace(id=user), channel_id=channel, message=SimpleNamespace(id=message), response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())
                    await runtime.on_interaction(interaction)
                    self.assertEqual(interaction.edit_original_response.await_count, int(allowed))
                    self.assertEqual(interaction.followup.send.await_count, int(not allowed))

    async def test_admins_control_other_requesters_without_bypassing_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state')
            record = store.capture('terminal', {}, 'ok', channel_id='111', user_id='9', session_id='s')
            assert record is not None
            store.bind(record['id'], '333')
            for admins, user, channel, message, allowed in [
                (['2','4'],2,111,333,True),
                (['2','4'],4,111,333,True),
                (['2','4'],9,111,333,True),
                (['2','4'],5,111,333,False),
                ([],2,111,333,False),
                (['2','4'],2,222,333,False),
                (['2','4'],2,111,334,False),
            ]:
                with self.subTest(admins=admins,user=user,channel=channel,message=message):
                    runtime = Inspector(store, {'*'}, owner_aliases=['2','4'], admin_user_ids=admins)
                    interaction = SimpleNamespace(data={'custom_id':f"hti:{record['id']}:0"}, user=SimpleNamespace(id=user), channel_id=channel, message=SimpleNamespace(id=message), response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())
                    await runtime.on_interaction(interaction)
                    self.assertEqual(interaction.edit_original_response.await_count, int(allowed))
                    self.assertEqual(interaction.followup.send.await_count, int(not allowed))

    async def test_admins_cannot_expand_expired_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [0]
            store = Store(Path(tmp)/'state', retention_days=30, clock=lambda: now[0])
            record = store.capture('terminal', {}, 'ok', channel_id='111', user_id='9', session_id='s')
            assert record is not None
            store.bind(record['id'], '333')
            now[0] = 31 * 86400
            runtime = Inspector(store, {'*'}, admin_user_ids=['2'])
            interaction = SimpleNamespace(data={'custom_id':f"hti:{record['id']}:0"}, user=SimpleNamespace(id=2), channel_id=111, message=SimpleNamespace(id=333), response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())
            await runtime.on_interaction(interaction)
            interaction.edit_original_response.assert_not_awaited()
            interaction.followup.send.assert_awaited_once()

    async def test_explicit_and_empty_lists_remain_restrictive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state')
            for channels, target, allowed in [({'*'},'111',True),({'*'},'invalid',False),({'111'},'111',True),({'111'},'222',False),(set(),'111',False)]:
                with self.subTest(channels=channels,target=target):
                    self.assertEqual(Inspector(store, channels).channel_allowed(target), allowed)
