"""Offline compact projection, HTTP size, and committed-baseline byte parity."""
from contextlib import ExitStack, contextmanager
import copy
import http.client
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from experimental.source_memory import coding_compact as compact, deployment

BASE = 'c45d23c5fb8ff1e2c44ed3470704d2e9ec51a741'
# Lexical Add/Search never tokenizes; do not download a tokenizer to test it.
ENCODING = None


@contextmanager
def serving(module=deployment, **kwargs):
    with tempfile.TemporaryDirectory() as directory:
        memory = module.DeploymentMemory(directory, None, None, ENCODING, **kwargs)
        httpd = module.server(memory, key='offline-compact-test-placeholder')
        worker = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=.01), daemon=True)
        worker.start()
        try:
            yield httpd.server_port, memory
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join(3)


def request(port, route, payload):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    connection.request('POST', route, json.dumps(payload, ensure_ascii=False).encode(),
                       {'Authorization': 'Bearer offline-compact-test-placeholder'})
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


def query(value='cache', **extra):
    return {'user_id': 'alice', 'query': value, 'top_k': 10, **extra}


def committed_baseline():
    path = 'experimental/source_memory/deployment.py'
    source = subprocess.run(['git', '-C', str(ROOT), 'show', f'{BASE}:{path}'],
                            check=True, capture_output=True, text=True).stdout
    module = types.ModuleType('experimental.source_memory._compact_baseline')
    module.__package__ = 'experimental.source_memory'
    module.__file__ = str(ROOT / path)
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


def diff(path, before='old', after='new'):
    return f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{before}\n+{after}\n'


def row(content, ident='first', score=.8):
    return {'id': ident, 'content': content, 'score': score}


class CompactEvidenceTests(unittest.TestCase):
    def test_hunk_boundaries_counts_and_no_dialogue_tail(self):
        text = diff('src/foo.py') + 'assistant: hidden tail\n+not a diff\n'
        self.assertEqual(compact.diff_hunks(text), [('src/foo.py', '@@ -1 +1 @@\n-old\n+new')])
        self.assertEqual(compact.diff_hunks(diff('src/foo.py').replace('+new\n', '')), [])
        plain = '--- a/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n'
        self.assertEqual(compact.diff_hunks(plain), [('old.py', '@@ -1,2 +0,0 @@\n-a\n-b')])
        self.assertNotIn('dev/null', compact.extract(plain)[0])
        multiple = diff('src/foo.py') + '@@ -10,0 +11,1 @@ fn\n+added\n'
        self.assertEqual(len(compact.diff_hunks(multiple)), 2)
        self.assertEqual(len(compact.diff_hunks(multiple.replace('\n', '\r\n'))), 2)

    def test_json_arguments_and_python_literal_actions_are_evidence(self):
        action = {'file_path': 'src/foo.py', 'old_string': 'a\nb', 'new_string': 'c\nd'}
        trace = json.dumps({'tool_calls': [{'function': {'arguments': json.dumps(action)}}]})
        paths, changes, _ = compact.extract(trace)
        self.assertIn('src/foo.py', paths)
        self.assertIn('"old": "a\\nb"', changes['src/foo.py'][0])
        self.assertIn('execution unverified', changes['src/foo.py'][0])
        python = "path='src/bar.py'\nold='''before\nline'''\nnew='''after\nline'''"
        self.assertIn('src/bar.py', compact.extract(python)[1])
        self.assertIn('a/nested.py', compact.extract(json.dumps({'path': 'a/nested.py', 'old': 'x', 'new': 'y'}))[1])
        self.assertEqual(compact.extract("path='x.py'\nold='a'\npath='y.py'\nnew='b'")[1], {})
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'must-not-exist'
            compact.extract(f"path='x.py'\nold=__import__('pathlib').Path({str(target)!r}).touch()\nnew='x'")
            self.assertFalse(target.exists())

    def test_serialized_tool_output_diff_and_no_global_unescape(self):
        text = json.dumps({'output': diff('pkg/main.ts')})
        self.assertIn('pkg/main.ts', compact.extract(text)[1])
        self.assertEqual(compact.extract(r'plain text\n@@ -1 +1 @@\n-a\n+b')[1], {})

    def test_dedupe_files_in_candidate_order_and_keep_distinct_hunks(self):
        first = diff('src/foo.py') + diff('src/foo.py', 'x', 'y')
        result = {'data': [row(first), row(diff('src/foo.py') + diff('src/bar.py'), 'second', .4)]}
        before = copy.deepcopy(result)
        found = compact.compact_search(result)
        self.assertEqual(result, before)
        self.assertEqual([x['id'] for x in found['data']], ['first', 'second'])
        self.assertEqual(found['data'][0]['score'], .8)
        self.assertEqual([line for line in found['data'][0]['content'].splitlines() if line.startswith('File: ')],
                         ['File: src/foo.py'])
        self.assertIn('-x\n+y', found['data'][0]['content'])
        self.assertNotIn('src/foo.py', found['data'][1]['content'])
        self.assertIn('src/bar.py', found['data'][1]['content'])

    def test_decisions_and_raw_fallback(self):
        content = 'verbose chatter ' * 2000 + '\nFinal: fixed src/foo.py because the cache was stale.'
        found = compact.compact_search({'data': [row(content)]})['data'][0]['content']
        self.assertIn('File: src/foo.py', found)
        self.assertIn('cache was stale', found)
        self.assertIn('Raw excerpt (no recognized change)', found)
        self.assertLess(len(found.encode()), 3000)
        plain = compact.compact_search({'data': [row('无路径' * 3000)]})['data'][0]['content']
        self.assertLessEqual(len(plain.encode()), compact.FALLBACK_BYTES + 50)
        decisions = compact.extract('\n'.join(f'Final: fix number {i}' for i in range(8)))[2]
        self.assertEqual(decisions, [f'Final: fix number {i}' for i in range(4, 8)])

    def test_total_wire_budget_unicode_json_escapes_and_determinism(self):
        rng = random.Random(12)
        for count in (0, 1, 8, 100):
            with self.subTest(count=count):
                values = [row(diff(f'src/file{i}.py', after='中😀"\\\t' * rng.randint(2000, 3000)),
                              f'id{i}', 1 / (i + 1)) for i in range(count)]
                original = {'data': values}
                found = compact.compact_search(original)
                self.assertLessEqual(len(compact.wire(found)), compact.MAX_RESPONSE_BYTES)
                self.assertTrue(all(len(compact.wire(x)) <= compact.MAX_ROW_BYTES for x in found['data']))
                self.assertEqual(found, compact.compact_search(original))
                self.assertEqual(json.loads(compact.wire(found)), found)
        self.assertIn('[compact: truncated]', found['data'][0]['content'])


class CompactCompatibilityTests(unittest.TestCase):
    def test_default_and_false_search_bytes_equal_actual_107ac55(self):
        old = committed_baseline()
        with ExitStack() as stack:
            instances = [stack.enter_context(serving(old)),
                         stack.enter_context(serving()),
                         stack.enter_context(serving(coding_compact_search=False))]
            operations = []
            for i, (owner, text) in enumerate([
                ('alice', diff('src/foo.py') + '\nFinal: fixed cache because stale. 中文😀'),
                ('alice', 'routine chatter ' * 4000), ('bob', 'isolated cache record')]):
                operations.append(('/add', {'user_id': owner, 'session_id': f's{i}',
                    'request_id': f'r{i}', 'messages': [{'role': 'assistant', 'content': text,
                                                       'timestamp': 1704067200000}]}))
            operations += [('/search', value) for value in [query(), query('cache', top_k=100),
                query('中文😀', top_k=1), query('cache', user_id='bob'), query(user_id='empty'),
                query([{'type': 'text', 'text': 'cache'}]),
                query('src/foo.py', options=['fix', '中文']), query('', top_k=0),
                query('cache', unexpected=True)]]
            for route, body in operations:
                expected = request(instances[0][0], route, body)
                for port, _ in instances[1:]:
                    self.assertEqual(request(port, route, body), expected)

    def test_enabled_http_wire_cap_api_shape_and_stored_source_unchanged(self):
        with serving(coding_compact_search=True) as (port, memory):
            raw = diff('src/foo.py', after='中😀"\\' * 10000)
            payload = {'user_id': 'alice', 'session_id': 's', 'request_id': 'r',
                       'messages': [{'role': 'assistant', 'content': raw}]}
            with patch.object(deployment, 'UniqueSpanAnnotator', side_effect=AssertionError('no model annotator')):
                self.assertEqual(request(port, '/add', payload)[0], 200)
                status, body = request(port, '/search', query('foo'))
                self.assertEqual(status, 200)
                self.assertLessEqual(len(body), 65536)
                found = json.loads(body)
                self.assertEqual(set(found), {'data'})
                self.assertEqual(set(found['data'][0]), {'id', 'content', 'score'})
                self.assertIn('File: src/foo.py', found['data'][0]['content'])
                self.assertIsNone(memory.transport)
                self.assertIsNone(memory.embedder)
            memory.coding_compact_search = False
            status, baseline = request(port, '/search', query('foo'))
            self.assertEqual(status, 200)
            self.assertIn(raw.rstrip(), json.loads(baseline)['data'][0]['content'])
            self.assertGreater(len(baseline), 65536)

    def test_flag_validation_and_lexical_config_wiring(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'retrieval': 'lexical', 'data_dir': directory, 'api_key_file': '/fixture/key',
                      'host': '127.0.0.1', 'port': 8080}
            file = Path(directory) / 'config.json'
            for flag in (False, True, 1, None, 'true', []):
                file.write_text(json.dumps({**config, 'coding_compact_search': flag}))
                if type(flag) is bool:
                    self.assertIs(deployment.read_config(file)['coding_compact_search'], flag)
                else:
                    with self.assertRaises(ValueError):
                        deployment.read_config(file)
                    with self.assertRaises(ValueError):
                        deployment.DeploymentMemory(directory, None, None, ENCODING,
                                                    coding_compact_search=flag)
            file.write_text(json.dumps(config))
            self.assertNotIn('coding_compact_search', deployment.read_config(file))
            with patch.object(deployment, 'private_key', return_value='offline-fixture-placeholder'), \
                 patch('tiktoken.get_encoding', return_value=ENCODING), \
                 patch.object(deployment, 'server') as server:
                deployment.serve_lexical({**config, 'coding_compact_search': True})
            self.assertIs(server.call_args.args[0].coding_compact_search, True)


if __name__ == '__main__':
    unittest.main()
