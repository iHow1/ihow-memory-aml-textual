"""Offline top_k contract regression; only synthetic lexical fixture messages."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tiktoken
from experimental.source_memory.deployment import DeploymentMemory, server


OWNER = 'u' * 73
QUERY = 'How many blue marbles are in the jar?'
ACCEPTED = (1, 100, 101, 500, 1000)
REJECTED = (0, 1001, -1, True, False, 100.0, '200', None, [], {})


def populate(memory, count=1100):
    # Same synthetic marble sentence as tests/test_source_lexical.py; one
    # independent session per message avoids neighbor grouping hiding a clamp.
    for n in range(count):
        memory.add({'user_id': OWNER, 'session_id': f's{n}', 'request_id': f'r{n}',
                    'messages': [{'role': 'user',
                                  'content': ('I keep 24 blue marbles in a jar on my desk. ' * 3).strip()}]})


def query(top):
    return {'user_id': OWNER, 'query': QUERY, 'top_k': top}


def invalid_payloads():
    return [query(top) for top in REJECTED] + [{'user_id': OWNER, 'query': QUERY}]


class TopKTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.encoding = tiktoken.get_encoding('o200k_base')
        cls.deployment = DeploymentMemory(cls.tmp.name, None, None, cls.encoding)
        cls.memory = cls.deployment.memory(OWNER, None)
        populate(cls.memory)

    def test_store_accepts_full_range_without_clamp(self):
        self.assertIsNone(self.memory.reader_tokens)
        for top in ACCEPTED:
            with self.subTest(top=top):
                self.assertEqual(len(self.memory.search(query(top))['data']), top)

    def test_store_rejects_invalid_or_missing_top_k(self):
        for payload in invalid_payloads():
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.memory.search(payload)

    def test_deployment_accepts_full_range_without_clamp(self):
        for top in ACCEPTED:
            with self.subTest(top=top):
                self.assertEqual(len(self.deployment.search(query(top))['data']), top)

    def test_deployment_rejects_invalid_or_missing_top_k(self):
        for payload in invalid_payloads():
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.deployment.search(payload)

    def test_compact_never_exceeds_requested_top_k_on_1100_candidates(self):
        from experimental.source_memory.coding_compact import compact_search, wire
        for top in ACCEPTED:
            original = self.deployment.search(query(top))
            with patch.object(self.deployment, 'coding_compact_search', True):
                result = self.deployment.search(query(top))
            self.assertEqual(result, compact_search(original))
            self.assertGreater(len(result['data']), 0)
            self.assertLessEqual(len(result['data']), top)
            self.assertLessEqual(len(wire(result)), 65536)

    def test_http_status_and_return_counts(self):
        # Public literal fixture only; no configuration or credential files read.
        key = 'offline-topk-fixture-only'
        api = server(self.deployment, key=key)
        worker = threading.Thread(target=api.serve_forever, daemon=True)
        worker.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def post(payload):
            req = urllib.request.Request(f'http://127.0.0.1:{api.server_port}/search',
                data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json', 'X-Api-Key': key})
            try:
                with opener.open(req, timeout=6) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.load(error)

        try:
            for top in ACCEPTED:
                with self.subTest(top=top):
                    status, result = post(query(top))
                    self.assertEqual(status, 200)
                    self.assertEqual(len(result['data']), top)
            for payload in invalid_payloads():
                with self.subTest(payload=payload):
                    self.assertEqual(post(payload)[0], 422)
        finally:
            api.shutdown()
            api.server_close()
            worker.join(3)

    def test_reader_budget_is_independent_of_requested_top_k(self):
        # The deployed default is None. Exercise the existing optional 6000-token
        # path on identical data without changing the database configuration.
        with patch.object(self.memory, 'reader_tokens', 6000):
            small = self.memory.search(query(100))['data']
            large = self.memory.search(query(1000))['data']
        self.assertEqual(large, small)
        self.assertGreater(len(large), 0)
        self.assertLess(len(large), 100)
        self.assertLessEqual(len(large), 1000)
        used = len(self.encoding.encode_ordinary('\n\n'.join(r['content'] for r in large)))
        self.assertLessEqual(used, 6000)
        print(f'\nreader_tokens=6000: top_k=100 -> {len(small)} rows; '
              f'top_k=1000 -> {len(large)} rows; content_tokens={used}')


if __name__ == '__main__':
    unittest.main()
