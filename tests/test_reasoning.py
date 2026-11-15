import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from inspector.reasoning import provider_summary
from inspector.runtime import Inspector
from inspector.storage import Store
from inspector.render import render, pages


def message(text='Checking the configuration.'):
    return SimpleNamespace(codex_reasoning_items=[{'type':'reasoning', 'encrypted_content':'DO-NOT-CAPTURE',
        'summary':[{'type':'summary_text', 'text':text}]}], reasoning_content='RAW-DO-NOT-CAPTURE')


class SummaryExtractionTests(unittest.TestCase):
    def test_explicit_provider_summary_only(self):
        self.assertEqual(provider_summary(message()), 'Checking the configuration.')
        self.assertEqual(provider_summary(SimpleNamespace(reasoning_content='RAW')), '')
        self.assertEqual(provider_summary({'codex_reasoning_items':[{'type':'reasoning',
            'encrypted_content':'RAW', 'summary':[{'type':'text','text':'RAW'}]}]}), '')

    def test_empty_and_multiple_summary_parts(self):
        self.assertEqual(provider_summary(message('')), '')
        self.assertEqual(provider_summary({'codex_reasoning_items':[{'type':'reasoning','summary':[
            {'type':'summary_text','text':'First.'},{'type':'summary_text','text':'Second.'}]}]}), 'First.\n\nSecond.')


class SummaryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_before_tools_once_and_persistent(self):
        from gateway.session_context import set_session_vars, clear_session_vars
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state')
            sent=[]
            async def send(**kw):
                sent.append(kw)
                return SimpleNamespace(id=100+len(sent))
            channel=SimpleNamespace(send=send)
            bot=SimpleNamespace(add_listener=lambda *a:None,remove_listener=lambda *a:None,get_channel=lambda _:channel)
            runtime=Inspector(store,{'123'})
            runtime.wire(bot,None)
            tokens=set_session_vars(platform='discord',chat_id='123',user_id='456',session_key='key',session_id='session')
            try:
                runtime.after_api(assistant_message=message('Check API_KEY=fictional-secret-value'),api_request_id='req-one')
                runtime.after_api(assistant_message=message('duplicate'),api_request_id='req-one')
                runtime.after_api(assistant_message=message(''),api_request_id='req-empty')
                runtime.after_tool('terminal',{'command':'printf ok'},{'output':'ok','exit_code':0})
            finally:
                clear_session_vars(tokens)
            await asyncio.sleep(0)
            await runtime.queue.join()
            self.assertEqual(len(sent),2)
            headings=[x['view'].to_components()[0]['components'][0]['content'] for x in sent]
            self.assertTrue(headings[0].startswith('**Reasoning** · '))
            self.assertIn('Check',headings[0])
            self.assertIn('terminal',headings[1])
            with store.connect() as db:
                records=[json.loads(row[0]) for row in db.execute('SELECT body FROM calls ORDER BY rowid')]
            self.assertEqual(records[0]['kind'],'reasoning_summary')
            self.assertNotIn('fictional-secret',json.dumps(records))
            self.assertNotIn('DO-NOT-CAPTURE',json.dumps(records))
            restored=Inspector(Store(store.directory),{'123'})
            button=sent[0]['view'].to_components()[0]['accessory']
            response=SimpleNamespace(send_message=AsyncMock(),edit_message=AsyncMock(),defer=AsyncMock())
            interaction=SimpleNamespace(data={'custom_id':button['custom_id']},user=SimpleNamespace(id=456),channel_id=123,message=SimpleNamespace(id=101),response=response, edit_original_response=response.edit_message, followup=SimpleNamespace(send=response.send_message))
            await restored.on_interaction(interaction)
            response.edit_message.assert_awaited_once()
            self.assertIn('Check',response.edit_message.call_args.kwargs['view'].to_components()[1]['content'])
            runtime.close()
            await asyncio.sleep(0)

    async def test_summary_dedupe_survives_store_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state')
            kw=dict(channel_id='123',user_id='456',session_id='session',kind='reasoning_summary',event_key='req')
            self.assertIsNotNone(store.capture('reasoning_summary',{},'A summary',**kw))
            self.assertIsNone(Store(store.directory).capture('reasoning_summary',{},'A summary',**kw))
            kw['session_id']='another-session'
            self.assertIsNotNone(store.capture('reasoning_summary',{},'A summary',**kw))

    async def test_long_summary_pages_are_bounded(self):
        r={'kind':'reasoning_summary','tool':'reasoning_summary','args':{},'result':'😀 checking @everyone `text`\n'*500}
        self.assertTrue(render(r).startswith('**Reasoning** · '))
        self.assertIn('...',render(r))
        self.assertGreater(len(pages(r)),1)
        for page in pages(r):
            self.assertLessEqual(len(page.encode('utf-16-le'))//2,1700)
