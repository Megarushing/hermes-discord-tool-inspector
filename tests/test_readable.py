import copy
import json
import re
import unittest

from inspector.render import sections, pages


class ReadableRenderingTests(unittest.TestCase):
    def test_display_decodes_serialization_not_literal_command_escapes(self):
        command = "echo `date`\n\tprintf 'literal \\n and \\u0060'\n# café 😀"
        record = {'tool': 'terminal', 'args': {'command': command},
                  'result': json.dumps({'output': 'café\n`done`\t😀', 'exit_code': 0})}
        original = copy.deepcopy(record)
        rendered = sections(record)
        self.assertIn(command, rendered[0][1])
        self.assertIn('café\n`done`\t😀', rendered[1][1])
        self.assertEqual(record, original)
        self.assertNotIn('\\ud83d', rendered[0][1])
        self.assertEqual(rendered[1][0], 'Output (exit code: 0)')

    def test_raw_strings_have_no_added_json_quotes(self):
        record = {'tool': 'test', 'args': 'one\ntwo', 'result': 'hello\nworld'}
        self.assertEqual(sections(record), [('Input', 'one\ntwo'), ('Output', 'hello\nworld')])

    def test_astral_character_does_not_overfill_shared_page(self):
        record = {'tool': 'test', 'args': 'a' * 14, 'result': '😀'}
        for chunk in pages(record, page_size=46):
            self.assertLessEqual(len(chunk.encode('utf-16-le')) // 2, 46)

    def test_fences_unicode_mentions_and_empty_strings_remain_lossless(self):
        for text in ('', '```\n@everyone\n```', '`' * 20, '😀é\n' * 1200):
            with self.subTest(length=len(text)):
                record = {'tool': 'test', 'args': text, 'result': text}
                chunks = pages(record)
                self.assertTrue(chunks)
                recovered = []
                for chunk in chunks:
                    self.assertLessEqual(len(chunk.encode('utf-16-le')) // 2, 1700)
                    blocks = re.findall(r'```\n(.*?)\n```', chunk, re.S)
                    self.assertIn(len(blocks), (1, 2))
                    for part in blocks:
                        self.assertNotIn('```', part)
                    recovered.extend(blocks)
                self.assertEqual(''.join(recovered), text + text)
                self.assertTrue(chunks[0].startswith('Input\n'))
                self.assertIn('Output\n', '\n'.join(chunks))
