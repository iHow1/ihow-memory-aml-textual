"""Model-free lexical mode: no annotator call, no embedding, BM25 over raw messages."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
import tiktoken
from experimental.source_memory import deployment
from experimental.source_memory.deployment import DeploymentMemory, read_config, server
from experimental.source_memory.lexical import NoAnnotator
from experimental.source_memory.store import Memory

KEY = 'offline-lexical-fixture-key-only'
ENCODING = tiktoken.get_encoding('o200k_base')


class Encoder:
    identity = 'offline-lexical-fixture-vectors'

    def encode(self, texts, query=False):
        return [[(x + 1) / 256 for x in hashlib.sha256(t.encode()).digest()] for t in texts]


def add(request, content, owner='alice', session='s1'):
    return {'request_id': request, 'session_id': session, 'user_id': owner,
            'messages': [{'role': 'user', 'content': content}]}


class LexicalStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'owner.sqlite'

    def test_raw_units_without_vectors_and_bm25_order(self):
        memory = Memory(self.path, NoAnnotator(), None, ENCODING)
        memory.add(add('a', 'The weather in Lisbon was sunny all week.', session='s1'))
        memory.add(add('b', 'I keep 24 blue marbles in a jar on my desk.', session='s2'))
        memory.add(add('c', 'My sister prefers green tea over coffee.', session='s3'))
        with memory.connect() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT kind, vector FROM units')],
                             [('raw', None)] * 3)
            self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='dimension'").fetchone())
        data = memory.search({'user_id': 'alice', 'query': 'How many marbles are in the jar?',
                              'top_k': 3})['data']
        self.assertIn('24 blue marbles', data[0]['content'])
        self.assertGreater(data[0]['score'], data[-1]['score'])

    def test_lexical_and_vector_databases_do_not_mix(self):
        Memory(self.path, NoAnnotator(), Encoder(), ENCODING)
        with self.assertRaisesRegex(ValueError, 'configuration mismatch'):
            Memory(self.path, NoAnnotator(), None, ENCODING)

    def test_owner_isolation_in_one_database(self):
        memory = Memory(self.path, NoAnnotator(), None, ENCODING)
        memory.add(add('a', 'Alice owns 24 marbles.'))
        memory.add(add('b', 'Bob owns 7 marbles.', owner='bob'))
        data = memory.search({'user_id': 'bob', 'query': 'marbles', 'top_k': 10})['data']
        self.assertEqual(len(data), 1)
        self.assertNotIn('Alice', data[0]['content'])


class LexicalDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.start()
        self.addCleanup(self.stop)

    def start(self):
        self.memory = DeploymentMemory(self.path / 'owners', None, None, ENCODING)
        self.httpd = server(self.memory, key=KEY)
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()
        self.url = 'http://127.0.0.1:' + str(self.httpd.server_port)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.worker.join(3)

    def request(self, route, body=None):
        req = urllib.request.Request(self.url + route,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + KEY})
        try:
            with urllib.request.urlopen(req, timeout=6) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def search(self, query='How many marbles?', owner='alice'):
        return self.request('/search', {'user_id': owner, 'query': query, 'top_k': 10})

    def test_health_reports_lexical_mode(self):
        self.assertEqual(self.request('/health'),
                         (200, {'status': 'ok', 'mode': 'source-memory-lexical-v1'}))

    def test_add_search_replay_conflict_and_restart(self):
        original = add('one', 'I have 24 marbles.')
        status, result = self.request('/add', original)
        self.assertEqual((status, result['success']), (200, True))
        first = self.search()
        self.assertEqual(first[0], 200)
        self.assertIn('24 marbles', first[1]['data'][0]['content'])
        self.assertEqual(self.request('/add', original), (status, result))
        self.assertEqual(self.request('/add', add('one', 'I have 25 marbles.'))[0], 409)
        self.stop()
        self.start()
        self.assertEqual(self.search(), first)

    def test_large_add_is_staged_and_searchable(self):
        messages = [{'role': 'user' if n % 2 == 0 else 'assistant',
                     'content': f'Note {n}: routine update number {n}.'} for n in range(45)]
        messages[33]['content'] = 'The spare house key is under the blue flowerpot.'
        body = {'request_id': 'bulk', 'session_id': 's1', 'user_id': 'alice', 'messages': messages}
        self.assertEqual(self.request('/add', body)[0], 200)
        status, found = self.search('Where is the spare key?')
        self.assertEqual(status, 200)
        self.assertIn('blue flowerpot', found['data'][0]['content'])
        owner = next((self.path / 'owners').glob('*.sqlite'))
        with sqlite3.connect(owner) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM streaming_requests WHERE state='complete'")
                             .fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM units WHERE vector IS NOT NULL')
                             .fetchone()[0], 0)

    def test_empty_owner_and_invalid_search(self):
        self.assertEqual(self.search(owner='nobody'), (200, {'data': []}))
        self.assertEqual(self.request('/search', {'user_id': 'alice', 'query': ' ', 'top_k': 1})[0], 422)

    def test_lexical_mode_rejects_an_embedder(self):
        with self.assertRaises(ValueError):
            DeploymentMemory(self.path / 'other', None, Encoder(), ENCODING)


class LexicalConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.config = {'retrieval': 'lexical', 'data_dir': str(self.dir / 'data'),
                       'api_key_file': str(self.dir / 'key'), 'host': '127.0.0.1', 'port': 8080}
        self.file = self.dir / 'config.json'

    def write(self, value):
        self.file.write_text(json.dumps(value))
        return self.file

    def test_lexical_shape_is_exact(self):
        self.assertEqual(read_config(self.write(self.config)), self.config)
        for bad in [{**self.config, 'retrieval': 'bm25'}, {**self.config, 'data_dir': 'rel'},
                    {**self.config, 'stages': {'add': [1, .1], 'search': [1, .1]}},
                    {**self.config, 'model_key_file': '/example/model'},
                    {k: v for k, v in self.config.items() if k != 'retrieval'}]:
            with self.assertRaises(ValueError):
                read_config(self.write(bad))

    def run_main(self, *extra):
        argv = ['deployment', '--config', str(self.write(self.config)), *extra]
        with patch.object(sys, 'argv', argv):
            return deployment.main()

    def test_check_config_and_model_flag(self):
        with patch('builtins.print') as shown:
            self.run_main('--check-config')
        self.assertEqual(json.loads(shown.call_args[0][0])['retrieval'], 'lexical')
        with patch('sys.stderr'), self.assertRaises(SystemExit):
            self.run_main('--allow-model-calls')

    def test_serve_loads_no_model_key_transport_or_embedder(self):
        key = self.dir / 'key'
        key.write_text(KEY)
        key.chmod(0o600)
        built = []

        class Stop:
            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        def fake_server(memory, **kwargs):
            built.append((memory, kwargs))
            return Stop()

        with patch.object(deployment, 'server', fake_server), \
                patch.dict(sys.modules, {'source_eval_transport_v4': None,
                                         'experimental.source_memory.portable_embedding': None}):
            self.run_main()
        memory, kwargs = built[0]
        self.assertIsNone(memory.transport)
        self.assertIsNone(memory.embedder)
        self.assertEqual(kwargs, {'key': KEY, 'host': '127.0.0.1', 'port': 8080})


if __name__ == '__main__':
    unittest.main()
