import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from inspector.storage import Store
from inspector.runtime import Inspector

DAY = 86400


def capture(store, key=None):
    return store.capture('terminal', {}, 'ok', channel_id='1', user_id='2',
                         session_id='s', event_key=key)


class RetentionTests(unittest.TestCase):
    def test_expiry_boundary_and_new_records_survive(self):
        now = [100 * DAY]
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state', retention_days=30, clock=lambda: now[0])
            old = capture(store)
            assert old is not None
            now[0] += 30 * DAY - 1
            self.assertIsNotNone(store.get(old['id']))
            fresh = capture(store)
            assert fresh is not None
            now[0] += 1
            self.assertEqual(store.purge_expired(), 1)
            self.assertIsNone(store.get(old['id']))
            self.assertIsNotNone(store.get(fresh['id']))
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM call_times').fetchone()[0], 1)

    def test_reads_and_captures_purge_without_timer(self):
        now = [100 * DAY]
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state', retention_days=30, clock=lambda: now[0])
            old = capture(store)
            assert old is not None
            now[0] += 31 * DAY
            self.assertIsNone(store.get(old['id']))
            old = capture(store)
            assert old is not None
            now[0] += 31 * DAY
            capture(store)
            with store.connect() as db:
                self.assertIsNone(db.execute('SELECT id FROM calls WHERE id=?', (old['id'],)).fetchone())

    def test_legacy_migration_grace_does_not_reset_on_restart(self):
        now = [100 * DAY]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)/'state'
            directory.mkdir()
            with sqlite3.connect(directory/'calls.sqlite3') as db:
                db.execute('CREATE TABLE calls (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
                db.execute('INSERT INTO calls VALUES (?, ?)', ('legacy',json.dumps({'id':'legacy'})))
            store = Store(directory, retention_days=30, clock=lambda: now[0])
            self.assertIsNotNone(store.get('legacy'))
            now[0] += 29 * DAY
            store = Store(directory, retention_days=30, clock=lambda: now[0])
            self.assertIsNotNone(store.get('legacy'))
            now[0] += DAY
            store = Store(directory, retention_days=30, clock=lambda: now[0])
            self.assertIsNone(store.get('legacy'))
            # Old releases' two-column calls schema remains usable on rollback.
            with store.connect() as db:
                db.execute('INSERT INTO calls VALUES (?, ?)', ('rollback','{}'))

    def test_disabled_retention_and_dedup_do_not_refresh_age(self):
        now = [100 * DAY]
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state', retention_days=0, clock=lambda: now[0])
            first = capture(store, 'request')
            assert first is not None
            now[0] += 29 * DAY
            self.assertIsNone(capture(store, 'request'))
            now[0] += 100 * DAY
            self.assertEqual(store.purge_expired(), 0)
            self.assertIsNotNone(store.get(first['id']))
            store = Store(store.directory, retention_days=30, clock=lambda: now[0])
            self.assertIsNone(store.get(first['id']))

    def test_invalid_configuration_fails_before_mutating_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in (-1, True, '30', 1.5, None):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Store(Path(tmp)/'state', retention_days=value)  # pyright: ignore[reportArgumentType]
            self.assertFalse((Path(tmp)/'state').exists())


class IdleCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_worker_runs_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state', retention_days=30)
            inspector = Inspector(store, {'1'})
            queue = asyncio.Queue()
            async def timed_out_then_cancel(awaitable, timeout):
                awaitable.close()
                timed_out_then_cancel.count += 1
                self.assertEqual(timeout, 3600)
                if timed_out_then_cancel.count == 1:
                    raise asyncio.TimeoutError
                raise asyncio.CancelledError
            timed_out_then_cancel.count = 0
            with patch('inspector.runtime.asyncio.wait_for', side_effect=timed_out_then_cancel), patch.object(store, 'purge_expired', return_value=1) as purge:
                with self.assertRaises(asyncio.CancelledError):
                    await inspector._drain(queue)
                purge.assert_called_once()
