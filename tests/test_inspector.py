import json
import os
from pathlib import Path
import tempfile
import unittest

from inspector.storage import Store
from inspector.render import render, pages


class StorageRenderingTests(unittest.TestCase):
    def test_individual_call_roundtrip_and_compact_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'profile' / 'inspector')
            record = store.capture('terminal', {'command': "printf 'Hello, world.\\n'"},
                                   json.dumps({'output': 'Hello, world.\n', 'exit_code': 0}),
                                   channel_id='123', user_id='456', session_id='session')
            store.bind(record['id'], '789')
            reopened = Store(store.directory)
            saved = reopened.get(record['id'])
            self.assertEqual(saved['message_id'], '789')
            self.assertNotIn('completed', render(saved))
            self.assertIn('printf', render(saved))
            expanded = render(saved, 0)
            self.assertIn("printf 'Hello, world.\\n'", expanded)
            self.assertIn('\nInput\n```', expanded)
            self.assertIn('\nOutput (exit code: 0)\n```', expanded)
            self.assertIn('Hello, world.', expanded)
            self.assertEqual(len(pages(saved)), 1)
            self.assertEqual(store.directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_redaction_precedes_disk_and_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'private')
            record = store.capture('terminal', {'password': 'fictional-secret-one'},
                                   json.dumps({'token': 'fictional-secret-two',
                                               'output': 'API_KEY=fictional-secret-three'}),
                                   channel_id='123', user_id='456', session_id='session')
            self.assertNotIn('fictional-secret', json.dumps(record))
            self.assertNotIn(b'fictional-secret', store.path.read_bytes())
            self.assertEqual(store.get(record['id']), record)

    def test_redactor_failure_is_fail_closed(self):
        def broken(text):
            raise RuntimeError('redactor unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'private', redactor=broken)
            with self.assertRaises(RuntimeError):
                store.capture('terminal', {}, 'never-persist-this',
                              channel_id='1', user_id='2', session_id='s')
            self.assertNotIn(b'never-persist-this', store.path.read_bytes())
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0], 0)

    def test_rejects_symlinks_without_touching_targets(self):
        for kind in ('directory', 'ancestor', 'database', 'journal', 'wal', 'shm'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / 'target'
                target.mkdir(mode=0o755)
                marker = target / 'marker'
                marker.write_text('untouched')
                directory = root / 'private'
                if kind == 'directory':
                    directory.symlink_to(target, target_is_directory=True)
                elif kind == 'ancestor':
                    link = root / 'link'
                    link.symlink_to(target, target_is_directory=True)
                    directory = link / 'private'
                else:
                    directory.mkdir()
                    suffix = '' if kind == 'database' else '-' + kind
                    (directory / ('calls.sqlite3' + suffix)).symlink_to(marker)
                with self.assertRaises((ValueError, OSError)):
                    Store(directory)
                self.assertEqual(target.stat().st_mode & 0o777, 0o755)
                self.assertEqual(marker.read_text(), 'untouched')

    def test_rejects_database_replacement_after_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'private')
            target = Path(tmp) / 'target'
            target.write_text('untouched')
            store.path.unlink()
            store.path.symlink_to(target)
            with self.assertRaises((ValueError, OSError)):
                store.get('missing')
            self.assertEqual(target.read_text(), 'untouched')

    def test_hardlinked_database_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'private'
            directory.mkdir()
            target = Path(tmp) / 'target'
            target.write_text('untouched')
            os.link(target, directory / 'calls.sqlite3')
            with self.assertRaises((ValueError, OSError)):
                Store(directory)
            self.assertEqual(target.read_text(), 'untouched')

    def test_missing_record_binding_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'private')
            self.assertIsNone(store.get('absent'))
            with self.assertRaises(KeyError):
                store.bind('absent', '123')

    def test_pagination_lossless_bounded_and_injection_safe(self):
        from inspector.render import document
        record = {'tool': '@everyone```', 'args': {'command': '😀`@everyone' * 900},
                  'result': json.dumps({'output': 'line\n' * 1200, 'exit_code': 0,
                                        'extra': 'retained'})}
        chunks = pages(record)
        self.assertGreater(len(chunks), 1)
        import re
        from inspector.render import sections
        payload = ''.join(part for chunk in chunks for part in re.findall(r'```\n(.*?)\n```', chunk, re.S))
        self.assertEqual(payload, ''.join(text for label, text in sections(record)))
        self.assertIn('retained', ''.join(chunks))
        for index in range(len(chunks)):
            text = render(record, index)
            self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 2000)
            self.assertIn(text.count('```'), (2, 4))
            # Mentions in code are preserved; transport must suppress notifications.
            self.assertNotIn('\\u0060', text.split('Page ')[-1])
        for invalid in (-1, len(chunks)):
            with self.assertRaises(ValueError):
                render(record, invalid)
        for invalid in (0, 1701, -1):
            with self.assertRaises(ValueError):
                pages(record, invalid)


if __name__ == '__main__':
    unittest.main()
