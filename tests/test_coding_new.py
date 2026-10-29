"""Synthetic contract tests of the integrated strict coding candidate."""
import ast
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_coding_compact import serving, request, query, diff, row, ROOT
from experimental.source_memory import deployment, coding_compact as compact


class CodingNewTests(unittest.TestCase):
    def test_approved_defaults_remain_opt_in(self):
        self.assertEqual(compact.MAX_ROW_BYTES, 8192)
        self.assertEqual(compact.MAX_RESPONSE_BYTES, 65536)
        self.assertEqual(deployment.compact_options({}), dict(
            coding_compact_search=False, coding_compact_max_row_bytes=8192,
            coding_compact_max_response_bytes=65536))
        config = deployment.read_config(ROOT/'config.coding-new.example.json')
        self.assertTrue(config['coding_compact_search'])
        self.assertEqual(config['coding_compact_max_row_bytes'], 8192)
        self.assertEqual(config['coding_compact_max_response_bytes'], 65536)

    def test_cut_file_line_never_claims_a_shorter_fresh_path(self):
        first = row(diff('a/bc.py') + diff('a/b'), 'x' * 90)
        source = {'data': [first, row(diff('a/b'), 'second')]}
        # Force the first row to end inside a/bc.py, exactly at another fresh
        # path in the same candidate. The marker supplies a synthetic newline.
        cap = len(compact.wire({**first, 'content': 'File: a/b' + compact.TRUNCATED}))
        result = compact.compact_search(source, max_row_bytes=cap)
        self.assertEqual(result['data'][0]['content'], 'File: a/b' + compact.TRUNCATED)
        self.assertEqual([r['id'] for r in result['data']], [first['id'], 'second'])
        self.assertIn('File: a/b\n', result['data'][1]['content'])
        # A path fully retained at its original line boundary does claim it.
        cap = len(compact.wire({**first, 'content': 'File: a/bc.py' + compact.TRUNCATED}))
        result = compact.compact_search({'data': [first, row(diff('a/bc.py'), 'duplicate'),
                                                   row(diff('a/b'), 'second')]}, max_row_bytes=cap)
        self.assertEqual([r['id'] for r in result['data']], [first['id'], 'second'])

    def test_actual_http_json_bytes_include_metadata_and_marker_at_both_caps(self):
        source = {'data': [{**row(diff(f'src/f{i}.py', after='中😀"\\\t' * 3000), f'id{i}'),
                            'created_at': '2026-10-29', 'metadata': {'note': '中😀"\\\n'}}
                           for i in range(12)]}
        for row_cap, total_cap in ((512, 1200), (8192, 65536)):
            with self.subTest(row_cap=row_cap), serving(coding_compact_search=True,
                    coding_compact_max_row_bytes=row_cap,
                    coding_compact_max_response_bytes=total_cap) as (port, _):
                with patch.object(deployment.Memory, 'search', return_value=source):
                    status, body = request(port, '/search', query(top_k=12))
                self.assertEqual(status, 200)
                result = json.loads(body.decode('utf-8', errors='strict'))
                self.assertEqual(body, compact.wire(result))
                self.assertLessEqual(len(body), total_cap)
                self.assertIn(compact.TRUNCATED, result['data'][0]['content'])
                for original, found in zip(source['data'], result['data']):
                    self.assertLessEqual(len(compact.wire(found)), row_cap)
                    self.assertEqual({k:v for k,v in original.items() if k != 'content'},
                                     {k:v for k,v in found.items() if k != 'content'})
                self.assertLessEqual(len(result['data']), 12)
        # Outer fields, punctuation and escapes count toward the total too.
        result = compact.compact_search({**source, 'note': '中😀"\\\n' * 10},
                                        max_row_bytes=512, max_response_bytes=1200)
        self.assertLessEqual(len(compact.wire(result)), 1200)
        self.assertEqual(json.loads(compact.wire(result)), result)

    def test_projection_only_uses_supplied_evidence_and_omits_duplicates(self):
        action = json.dumps({'path': 'src/known.py', 'old': 'before', 'new': 'after'})
        result = compact.compact_search({'data': [row(action), row(action, 'duplicate')]})
        self.assertEqual(len(result['data']), 1)
        content = result['data'][0]['content']
        self.assertEqual(compact.extract(action)[0], ['src/known.py'])
        self.assertIn('execution unverified', content)
        self.assertIn('"old": "before", "new": "after"', content)
        self.assertNotIn('success', content.lower())
        self.assertEqual(compact.extract('ordinary words without file evidence')[:2], ([], {}))

    def test_configurable_limits_and_startup_wiring(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(retrieval='lexical', data_dir=directory,
                          api_key_file='/fixture/unused', host='127.0.0.1', port=8080,
                          coding_compact_search=True, coding_compact_max_row_bytes=512,
                          coding_compact_max_response_bytes=1200)
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(config))
            self.assertEqual(deployment.read_config(path), config)
            with patch.object(deployment, 'private_key', return_value='offline-unused-placeholder'), \
                 patch('tiktoken.get_encoding', return_value=None), \
                 patch.object(deployment, 'server') as server:
                deployment.serve_lexical(config)
            memory = server.call_args.args[0]
            self.assertTrue(memory.coding_compact_search)
            self.assertEqual(memory.coding_compact_max_row_bytes, 512)
            self.assertEqual(memory.coding_compact_max_response_bytes, 1200)
            for name in ('coding_compact_max_row_bytes', 'coding_compact_max_response_bytes'):
                for value in (None, True, '512', 512.0, -1, 0, 127):
                    path.write_text(json.dumps({**config, name: value}))
                    with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                        deployment.read_config(path)
                    with self.assertRaises(ValueError):
                        deployment.DeploymentMemory(directory, None, None, None, **{name: value})
            path.write_text(json.dumps({**config, 'coding_search_tolerant': True}))
            with self.assertRaises(ValueError):
                deployment.read_config(path)

    def test_strict_http_validation_query_preservation_and_owner_isolation(self):
        with serving(coding_compact_search=True) as (port, memory):
            payload = dict(user_id='alice', session_id='s', request_id='r',
                           messages=[dict(role='user', content=diff('src/private.py'))])
            self.assertEqual(request(port, '/add', payload)[0], 200)
            for top in (1, 100, 101, 500, 1000):
                status, raw = request(port, '/search', query('private', top_k=top))
                self.assertEqual(status, 200)
                self.assertIn('src/private.py', raw.decode())
                self.assertLessEqual(len(raw), 65536)
                self.assertLessEqual(len(json.loads(raw)['data']), top)
            invalid = [query(top_k=x) for x in (0, 1001, -1, True, False, 2.0, '2', None, [], {})]
            invalid += [{k:v for k,v in query().items() if k != field}
                        for field in ('user_id', 'query', 'top_k')]
            invalid += [query(''), query(' '), query('x'*20001), query(user_id=''),
                        query(unexpected=True), query(options='bad')]
            for body in invalid:
                self.assertEqual(request(port, '/search', body)[0], 422)
            self.assertEqual(json.loads(request(port, '/search', query(user_id='bob'))[1]), {'data': []})
            text = 'x' * 19980 + '尾部保持完整'
            with patch.object(deployment.Memory, 'search', return_value={'data': []}) as search:
                self.assertEqual(request(port, '/search', query(text))[0], 200)
            self.assertEqual(search.call_args.args[0]['query'], text)

    def test_all_budget_cuts_marked_order_and_metadata_preserved(self):
        raw = 'No path here ' * 5000 + '\nFinal: because ' + '中' * 1000
        result = compact.compact_search({'data': [row(raw)]})
        self.assertIn(compact.TRUNCATED, result['data'][0]['content'])
        source = {'data': [row(diff(f'src/f{i}.py', after='中😀\\"'*1000), str(i), 1/(i+1))
                           for i in range(10)]}
        result = compact.compact_search(source, max_row_bytes=512, max_response_bytes=1200)
        self.assertLessEqual(len(compact.wire(result)), 1200)
        self.assertTrue(all(len(compact.wire(x)) <= 512 for x in result['data']))
        self.assertLess(len(result['data']), 10)
        self.assertEqual([x['id'] for x in result['data']], [str(i) for i in range(len(result['data']))])
        self.assertTrue(all(compact.TRUNCATED in x['content'] for x in result['data']))
        # Large immutable metadata cannot fit: omit rather than corrupt identifiers.
        self.assertEqual(compact.compact_search({'data': [row('x', 'z'*1000)]},
                         max_row_bytes=128, max_response_bytes=128), {'data': []})

    def test_add_normalization_storage_and_ranking_sources_unchanged(self):
        base = subprocess.run(['git', '-C', str(ROOT), 'show', 'c45d23c5fb8ff1e2c44ed3470704d2e9ec51a741:experimental/source_memory/deployment.py'],
                              capture_output=True, text=True, check=True).stdout
        def method(source, name):
            tree = ast.parse(source)
            cls = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == 'DeploymentMemory')
            return ast.dump(next(x for x in cls.body if isinstance(x, ast.FunctionDef) and x.name == name))
        current = (ROOT/'experimental/source_memory/deployment.py').read_text()
        self.assertEqual(method(base, 'add'), method(current, 'add'))
        for name in ('content.py', 'lexical.py', 'streaming.py'):
            original = subprocess.run(['git', '-C', str(ROOT), 'show', 'c45d23c5fb8ff1e2c44ed3470704d2e9ec51a741:experimental/source_memory/'+name],
                                      capture_output=True, check=True).stdout
            self.assertEqual(original, (ROOT/'experimental/source_memory'/name).read_bytes())
        original = subprocess.run(['git', '-C', str(ROOT), 'show', 'c45d23c5fb8ff1e2c44ed3470704d2e9ec51a741:experimental/source_memory/store.py'],
                                  capture_output=True, text=True, check=True).stdout
        self.assertEqual(original.replace('not 1<=top<=100:', 'not 1<=top<=1000:'),
                         (ROOT/'experimental/source_memory/store.py').read_text())
