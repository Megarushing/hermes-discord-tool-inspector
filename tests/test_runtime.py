import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from inspector.runtime import Inspector, route_for_call
from inspector.storage import Store


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_buttons_owner_check_and_persistent_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state')
            channel = SimpleNamespace(id=123, send=AsyncMock(return_value=SimpleNamespace(id=789)))
            bot = SimpleNamespace(get_channel=lambda _: channel, fetch_channel=AsyncMock(return_value=channel))
            runtime = Inspector(store, {'123'})
            runtime.bot = bot
            record = store.capture('terminal', {'command': 'printf hello'},
                                   json.dumps({'output': 'hello', 'exit_code': 0}),
                                   channel_id='123', user_id='456', session_id='test')
            await runtime.publish(record)
            payload = channel.send.call_args.kwargs
            self.assertFalse(payload['allowed_mentions'].everyone)
            layout = payload['view'].to_components()
            self.assertEqual(layout[0]['type'], 9)
            self.assertEqual(layout[0]['accessory']['label'], '⋁')
            self.assertNotIn('content', payload)
            button = next(item for item in payload['view'].walk_children() if hasattr(item, 'custom_id'))
            self.assertEqual(button.label, '⋁')
            # Simulate a restarted plugin: no in-memory view lookup is needed.
            runtime = Inspector(Store(store.directory), {'123'})
            response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock())
            interaction = SimpleNamespace(data={'custom_id': button.custom_id}, user=SimpleNamespace(id=999),
                                          channel_id=123, message=SimpleNamespace(id=789), response=response, edit_original_response=response.edit_message, followup=SimpleNamespace(send=response.send_message))
            await runtime.on_interaction(interaction)
            response.send_message.assert_awaited_once()
            response.edit_message.assert_not_awaited()
            interaction.user.id = 456
            await runtime.on_interaction(interaction)
            edit = response.edit_message.call_args.kwargs
            self.assertIsNone(edit['content'])
            self.assertEqual(edit['embeds'], [])
            text = '\n'.join(item.content for item in edit['view'].walk_children() if hasattr(item, 'content'))
            self.assertIn('Output (exit code: 0)', text)
            self.assertIn('hello', text)
            controls = [item for item in response.edit_message.call_args.kwargs['view'].walk_children() if hasattr(item, 'custom_id')]
            self.assertEqual(controls[0].label, '⋀')
            interaction.data['custom_id'] = controls[0].custom_id
            await runtime.on_interaction(interaction)
            self.assertEqual(len(response.edit_message.call_args.kwargs['view'].to_components()), 1)

    async def test_actual_hermes_adapter_rewires_after_plugin_reload(self):
        from gateway.platforms.base import BasePlatformAdapter
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            listeners = []
            bot = SimpleNamespace(add_listener=lambda fn, name: listeners.append(fn),
                                  remove_listener=lambda fn, name: listeners.remove(fn))
            adapter = SimpleNamespace(platform=SimpleNamespace(value='discord'), name='Discord',
                                      _plugin_handler_native=None, _plugin_handlers_wired=None)
            first = Inspector(Store(Path(tmp)/'state'), {'123'})
            factories = [(first.platform_factory(), 'discord-tool-inspector')]
            manager = SimpleNamespace(get_platform_handler_factories=lambda _: factories)
            with patch('hermes_cli.plugins.get_plugin_manager', return_value=manager):
                BasePlatformAdapter._wire_plugin_handlers(adapter, bot)
                self.assertEqual(listeners, [first.on_interaction])
                first.close()
                await asyncio.sleep(0)
                second = Inspector(Store(Path(tmp)/'state'), {'123'})
                factories[:] = [(second.platform_factory(), 'discord-tool-inspector')]
                BasePlatformAdapter._wire_plugin_handlers(adapter, bot)
                self.assertEqual(listeners, [second.on_interaction])
                self.assertIsNotNone(second.loop)
                # Repeated rewiring of this same generation must remain idempotent.
                BasePlatformAdapter._wire_plugin_handlers(adapter, bot)
                self.assertEqual(listeners, [second.on_interaction])
                second.close()
                await asyncio.sleep(0)

    async def test_rewire_replaces_old_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            listeners = []
            bot = SimpleNamespace(add_listener=lambda fn, name: listeners.append(fn),
                                  remove_listener=lambda fn, name: listeners.remove(fn))
            first = Inspector(Store(Path(tmp)/'state'), {'123'})
            first.wire(bot, None)
            second = Inspector(Store(Path(tmp)/'state'), {'123'})
            second.wire(bot, None)
            self.assertEqual(listeners, [second.on_interaction])
            await asyncio.sleep(0)
            self.assertTrue(first.worker.cancelled())
            second.worker.cancel()
            await asyncio.sleep(0)

    async def test_unload_removes_listener_and_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            listeners = []
            bot = SimpleNamespace(add_listener=lambda fn, name: listeners.append(fn),
                                  remove_listener=lambda fn, name: listeners.remove(fn))
            runtime = Inspector(Store(Path(tmp)/'state'), {'123'})
            runtime.wire(bot, None)
            runtime.close()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(listeners)
            await asyncio.wait_for(asyncio.gather(runtime.worker, return_exceptions=True), 1)
            self.assertTrue(runtime.worker.cancelled())
            self.assertFalse(hasattr(bot, '_hermes_tool_inspector'))

    async def test_hook_captures_and_publishes_only_allowed_route(self):
        from gateway.session_context import set_session_vars, clear_session_vars
        with tempfile.TemporaryDirectory() as tmp:
            channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=789)))
            bot = SimpleNamespace(add_listener=lambda *a: None, remove_listener=lambda *a: None,
                                  get_channel=lambda _: channel)
            runtime = Inspector(Store(Path(tmp)/'state'), {'123'})
            runtime.wire(bot, None)
            tokens = set_session_vars(platform='discord', chat_id='123', user_id='456',
                                      session_key='route', session_id='session', cron_session='')
            try:
                runtime.after_tool('terminal', {'command': 'printf hello'}, {'output': 'hello', 'exit_code': 0})
            finally:
                clear_session_vars(tokens)
            await asyncio.sleep(0)
            await runtime.queue.join()
            channel.send.assert_awaited_once()
            tokens = set_session_vars(platform='discord', chat_id='124', user_id='456',
                                      session_key='other', session_id='session', cron_session='')
            try:
                runtime.after_tool('terminal', {}, 'no')
            finally:
                clear_session_vars(tokens)
            await asyncio.sleep(0)
            channel.send.assert_awaited_once()
            runtime.close()
            await asyncio.sleep(0)

    async def test_wrong_channel_or_message_cannot_reveal_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state')
            record = store.capture('terminal', {}, 'private', channel_id='123', user_id='456', session_id='test')
            store.bind(record['id'], '789')
            runtime = Inspector(store, {'123'})
            for channel, message in [(123, 888), (124, 789)]:
                response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock())
                interaction = SimpleNamespace(data={'custom_id': f"hti:{record['id']}:0"},
                    user=SimpleNamespace(id=456), channel_id=channel, message=SimpleNamespace(id=message), response=response, edit_original_response=response.edit_message, followup=SimpleNamespace(send=response.send_message))
                await runtime.on_interaction(interaction)
                response.edit_message.assert_not_awaited()
                response.send_message.assert_awaited_once()

class RouteTests(unittest.TestCase):
    def test_route_is_task_scoped_and_excludes_cron_children(self):
        from gateway.session_context import set_session_vars, clear_session_vars
        from unittest.mock import patch
        tokens = set_session_vars(platform='discord', chat_id='123', user_id='456',
                                  session_key='route', session_id='session', cron_session='')
        try:
            self.assertEqual(route_for_call()['channel_id'], '123')
            with patch('agent.delegation_context.is_delegated_child_context', return_value=True):
                self.assertIsNone(route_for_call())
        finally:
            clear_session_vars(tokens)
        tokens = set_session_vars(platform='discord', chat_id='123', user_id='456',
                                  session_key='route', session_id='session', cron_session='1')
        try:
            self.assertIsNone(route_for_call())
        finally:
            clear_session_vars(tokens)

    def test_unbound_context_never_uses_process_environment(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'HERMES_SESSION_PLATFORM': 'discord','HERMES_SESSION_CHAT_ID': '123'}):
            self.assertIsNone(route_for_call())

if __name__ == '__main__':
    unittest.main()
