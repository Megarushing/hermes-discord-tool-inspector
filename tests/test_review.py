import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from inspector.redaction import redact, forced_redactor
from inspector.storage import Store
from inspector.runtime import Inspector
from inspector.render import render


class KeyTests(unittest.TestCase):
    def test_credentials_in_keys_redacted_on_disk_and_render(self):
        secret='unit-test-key-abcdefghijklmnop'
        key='API_KEY='+secret
        self.assertNotIn(secret,forced_redactor(key))
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state')
            for result in ({key:'ordinary'},json.dumps({key:'ordinary'})):
                r=store.capture('test',{},result,channel_id='1',user_id='2',session_id='s')
                self.assertFalse(secret.encode() in store.path.read_bytes(), 'synthetic credential persisted')
                assert r is not None
                self.assertNotIn(secret,render(r,0))

    def test_redacted_key_collisions_preserve_all_entries(self):
        policy=lambda s: '[MASKED]' if s.startswith('key-') else s
        output=redact({'key-a':1,'key-b':2,'[MASKED]':3},policy)
        assert isinstance(output,dict)
        self.assertEqual(len(output),3)
        self.assertEqual(sorted(output.values()),[1,2,3])
        self.assertNotIn('key-',json.dumps(output))


def bot():
    listeners=[]
    def remove(fn,name):
        if fn in listeners:
            listeners.remove(fn)
    return SimpleNamespace(add_listener=lambda fn,name:listeners.append(fn),remove_listener=remove,listeners=listeners)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_replacement_client_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=Inspector(Store(Path(tmp)/'state'),{'123'})
            a,b=bot(),bot()
            runtime.wire(a,None)
            old_worker=runtime.worker
            assert old_worker is not None
            runtime.wire(b,None)
            new_worker=runtime.worker
            assert new_worker is not None
            await asyncio.sleep(0)
            self.assertFalse(a.listeners)
            self.assertTrue(old_worker.cancelled())
            self.assertEqual(b.listeners,[runtime.on_interaction])
            runtime.close()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(b.listeners)
            await asyncio.wait_for(asyncio.gather(new_worker, return_exceptions=True), 1)
            self.assertTrue(new_worker.cancelled())

    async def test_delayed_unload_does_not_detach_same_client_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=Inspector(Store(Path(tmp)/'state'),{'123'})
            client=bot()
            runtime.wire(client,None)
            old_worker=runtime.worker
            assert old_worker is not None
            runtime.close()
            runtime.wire(client,None)
            worker=runtime.worker
            assert worker is not None
            await asyncio.sleep(0)
            self.assertEqual(client.listeners,[runtime.on_interaction])
            self.assertFalse(worker.cancelled())
            self.assertTrue(old_worker.cancelled())
            runtime.close()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 1)
            self.assertTrue(worker.cancelled())

    async def test_slow_ack_cannot_apply_older_expand_after_collapse(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state')
            r=store.capture('terminal',{},'ok',channel_id='123',user_id='456',session_id='s')
            assert r is not None
            store.bind(r['id'],'789')
            runtime=Inspector(store,{'123'})
            started,release=asyncio.Event(),asyncio.Event()
            edits=[]
            async def slow_ack(**kwargs):
                started.set()
                await release.wait()
            async def edit(**kwargs):
                edits.append('expanded' if len(kwargs['view'].to_components())>1 else 'collapsed')
            def interaction(page,ack):
                return SimpleNamespace(data={'custom_id':f"hti:{r['id']}:{page}"},user=SimpleNamespace(id=456),channel_id=123,message=SimpleNamespace(id=789),response=SimpleNamespace(defer=ack),edit_original_response=edit,followup=SimpleNamespace(send=AsyncMock()))
            first=asyncio.create_task(runtime.on_interaction(interaction(0,slow_ack)))
            await asyncio.wait_for(started.wait(),2)
            try:
                await runtime.on_interaction(interaction(-1,AsyncMock()))
            finally:
                release.set()
                await first
            self.assertEqual(edits,['collapsed'])

    async def test_explicit_owner_aliases_preserve_other_access_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state')
            for owner,actor,channel,message,aliases,allowed in [
                ('101',202,123,789,['101','202'],True),
                ('202',101,123,789,['101','202'],True),
                ('101',101,123,789,[],True),
                ('101',202,123,789,[],False),
                ('101',303,123,789,['101','202'],False),
                ('303',101,123,789,['101','202'],False),
                ('101',202,124,789,['101','202'],False),
                ('101',202,123,790,['101','202'],False),
            ]:
                with self.subTest(owner=owner,actor=actor,channel=channel,message=message,aliases=aliases):
                    r=store.capture('terminal',{},'ok',channel_id='123',user_id=owner,session_id='s')
                    assert r is not None
                    store.bind(r['id'],'789')
                    runtime=Inspector(Store(store.directory),{'123'},owner_aliases=aliases)
                    interaction=SimpleNamespace(data={'custom_id':f"hti:{r['id']}:0"},user=SimpleNamespace(id=actor),channel_id=channel,message=SimpleNamespace(id=message),response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock(),followup=SimpleNamespace(send=AsyncMock()))
                    await runtime.on_interaction(interaction)
                    self.assertEqual(interaction.edit_original_response.await_count,int(allowed))
                    self.assertEqual(interaction.followup.send.await_count,int(not allowed))

    async def test_sdk_serializes_v2_deferred_edit_payload(self):
        import discord
        from discord.http import handle_message_parameters
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state')
            r=store.capture('terminal',{},'ok',channel_id='123',user_id='456',session_id='s')
            assert r is not None
            runtime=Inspector(store,{'123'})
            with handle_message_parameters(content=None,embeds=[],attachments=[],view=runtime.view(r,0),allowed_mentions=discord.AllowedMentions.none()) as params:
                assert params.payload is not None
                self.assertTrue(params.payload['flags'] & 32768)
                self.assertIsNone(params.payload['content'])
                self.assertEqual(params.payload['embeds'],[])
                self.assertEqual(params.payload['attachments'],[])
                self.assertEqual(params.payload['allowed_mentions']['parse'],[])

    async def test_overlapping_clicks_finish_collapsed_and_ack_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state')
            r=store.capture('terminal',{'command':'printf ok'},{'output':'ok','exit_code':0},channel_id='123',user_id='456',session_id='s')
            assert r is not None
            store.bind(r['id'],'789')
            runtime=Inspector(store,{'123'})
            started,release=asyncio.Event(),asyncio.Event()
            edits=[]
            async def edit(**kw):
                expanded=len(kw['view'].to_components())>1
                if expanded:
                    started.set()
                    await release.wait()
                edits.append('expanded' if expanded else 'collapsed')
            def interaction(page):
                response=SimpleNamespace(defer=AsyncMock(),edit_message=edit,send_message=AsyncMock())
                return SimpleNamespace(data={'custom_id':f"hti:{r['id']}:{page}"},user=SimpleNamespace(id=456),channel_id=123,message=SimpleNamespace(id=789),response=response,edit_original_response=edit,followup=SimpleNamespace(send=AsyncMock()))
            expand,collapse=interaction(0),interaction(-1)
            first=asyncio.create_task(runtime.on_interaction(expand))
            await asyncio.wait_for(started.wait(),2)
            second=asyncio.create_task(runtime.on_interaction(collapse))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            try:
                collapse.response.defer.assert_awaited_once()
                self.assertFalse(second.done())
            finally:
                release.set()
                await asyncio.gather(first,second)
            self.assertEqual(edits,['expanded','collapsed'])
