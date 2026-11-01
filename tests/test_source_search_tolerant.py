"""Offline Search compatibility, coercion and privacy regression tests."""
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tiktoken
from experimental.source_memory import deployment
from experimental.source_memory.content import tolerant_search
from experimental.source_memory.validation import LOGGER

BASE = 'c45d23c5fb8ff1e2c44ed3470704d2e9ec51a741'
KEY = 'offline-search-tolerant-fixture-key'
ENCODING = tiktoken.get_encoding('o200k_base')


def baseline():
    """Load the actual committed adapter, not a rewritten expected algorithm."""
    modules = {}
    for name in ('content', 'deployment'):
        path = f'experimental/source_memory/{name}.py'
        source = subprocess.run(['git', '-C', str(ROOT), 'show', f'{BASE}:{path}'],
                                check=True, capture_output=True, text=True).stdout
        module = types.ModuleType(f'experimental.source_memory._baseline_{name}')
        module.__package__ = 'experimental.source_memory'
        module.__file__ = str(ROOT / path)
        exec(compile(source, module.__file__, 'exec'), module.__dict__)
        modules[name] = module
    # Relative imports must use the baseline ContentPart normalization as well.
    modules['deployment'].normalize_add = modules['content'].normalize_add
    modules['deployment'].normalize_search = modules['content'].normalize_search
    return modules['deployment']


@contextmanager
def serving(module=deployment, **kwargs):
    with tempfile.TemporaryDirectory() as directory:
        memory = module.DeploymentMemory(Path(directory) / 'owners', None, None, ENCODING, **kwargs)
        httpd = module.server(memory, key=KEY)
        worker = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=.01), daemon=True)
        worker.start()
        try:
            yield httpd.server_port, memory
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join(3)


def request(port, route, payload, *, raw=False):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    body = payload if raw else json.dumps(payload, ensure_ascii=False).encode()
    connection.request('POST', route, body, {'Authorization': 'Bearer ' + KEY})
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


def query(value='marbles', **extra):
    return {'user_id': 'alice', 'query': value, 'top_k': 10, **extra}


class CompatibilityTests(unittest.TestCase):
    def test_rejections_match_107ac55_response_bytes(self):
        old = baseline()
        cases = [None, [], 1, {}, query(None), query(''), query(' \t\n'),
                 query([]), query([{'type': 'text', 'text': ' '}]),
                 query(7), query({}), query(top_k=0), query(top_k=True),
                 query(options={}), query(options=[7]), query(extra='unused'),
                 query(user_id=''), query('x' * 20001),
                 {'user_id': 'alice', 'top_k': 10}]
        with serving(old) as (old_port, _), serving() as (default_port, _), \
                serving(coding_search_tolerant=False) as (false_port, _):
            for index, body in enumerate(cases):
                with self.subTest(case=index):
                    expected = request(old_port, '/search', body)
                    self.assertEqual(expected[0], 422)
                    for port in (default_port, false_port):
                        with self.assertLogs(LOGGER, level='WARNING'):
                            self.assertEqual(request(port, '/search', body), expected)

    def test_default_and_explicit_false_match_107ac55_response_bytes(self):
        old = baseline()
        operations = [('/add', {'user_id': owner, 'session_id': 's1', 'request_id': str(index),
                       'messages': [{'role': 'user', 'content': content, 'timestamp': 1704067200000}]})
                      for index, (owner, content) in enumerate([
                          ('alice', 'I have 24 blue marbles. 中文记忆。'),
                          ('alice', [{'type': 'text', 'text': '  green tea\t'},
                                     {'type': 'text', 'text': '\nwith coffee  '}]),
                          ('bob', 'Bob has seven marbles.')])]
        operations += [('/search', body) for body in [
            query(), query('  中文\t记忆\n  ', top_k=1),
            query([{'type': 'text', 'text': '  green tea'}, {'type': 'text', 'text': '\ncoffee '}]),
            query('marbles', top_k=100, options=['original', '中文']),
            query('marbles', user_id='bob'), query('marbles', user_id='nobody'),
            query('x' * 20000), query('\tmarbles\n', options=[]),
        ]]
        # Also compare successful replay bytes, not just parsed JSON equality.
        operations.append(operations[0])
        with serving(old) as (old_port, _), serving() as (new_port, _), \
                serving(coding_search_tolerant=False) as (false_port, _):
            for route, body in operations:
                with self.subTest(route=route, index=operations.index((route, body))):
                    expected = request(old_port, route, body)
                    self.assertEqual(expected[0], 200)
                    self.assertEqual(request(new_port, route, body), expected)
                    self.assertEqual(request(false_port, route, body), expected)

    def test_model_replay_identity_and_model_inputs_match_baseline(self):
        old = baseline()
        class Transport:
            def __init__(self): self.calls = []
            def complete(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return '{}'
        body = query([{'type': 'text', 'text': 'one'}, {'type': 'text', 'text': 'two'}])
        inputs, calls = [], []
        with tempfile.TemporaryDirectory() as directory:
            for module in (old, deployment):
                transport = Transport()
                memory = module.DeploymentMemory(directory, transport, None, ENCODING)
                class Database:
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def execute(self, sql): return [('request-one', 'fixture-hash')]
                class Backend:
                    def __init__(self, owner, complete): self.complete = complete
                    def connect(self): return Database()
                    def search(self, payload):
                        inputs.append(payload)
                        self.complete([{'role': 'user', 'content': payload['query']}])
                        return {'data': []}
                with patch.object(memory, 'memory', side_effect=Backend):
                    self.assertEqual(memory.search(body), {'data': []})
                calls.append(transport.calls)
        self.assertEqual(inputs[0], inputs[1])
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len(calls[0]), 1)


class TolerantTests(unittest.TestCase):
    def test_all_seven_rejection_categories(self):
        cases = [query('开' * 10000 + 'middle' + '尾' * 10000),
                 query(extra='private-unused'), query(top_k=0), query(top_k=101),
                 query(options={'private': 'unused'}), query(options=['okay', 7]),
                 query([{'type': 'text', 'text': '', 'extra': 'unused'},
                        {'type': 'text', 'text': 'marbles', 'extra': 'unused'}]),
                 query([{'type': 'image', 'url': 'private-unused'},
                        {'type': 'text', 'text': 'marbles'}])]
        with serving() as (strict, _), serving(coding_search_tolerant=True) as (loose, _):
            for body in cases:
                with self.subTest(case=cases.index(body)):
                    with self.assertLogs(LOGGER, level='WARNING'):
                        self.assertEqual(request(strict, '/search', body)[0], 422)
                    self.assertEqual(request(loose, '/search', body), (200, b'{"data": []}'))

    def test_empty_queries_return_empty_without_touching_owners(self):
        cases = [{'user_id': 'alice', 'top_k': 10}] + [query(value) for value in (
            None, '', ' \t\n\u3000', 7, 1.5, False, {}, {'text': 'not a part'}, [],
            [{'type': 'text', 'text': ''}], [{'type': 'text', 'text': ' \t\n'}],
            [{'type': 'image', 'text': 'SENSITIVE'}], [None, 1, 'SENSITIVE'],
            [{'type': 'text', 'text': None}], [{'text': 'SENSITIVE'}],
            [{'type': 'text', 'text': 7}, {'type': 'text', 'text': []}])]
        with serving(coding_search_tolerant=True) as (port, memory):
            # Include an existing database, so the snapshot is not just empty.
            request(port, '/add', {'user_id': 'existing', 'session_id': 's1',
                'request_id': 'fixture', 'messages': [{'role': 'user', 'content': 'synthetic fixture'}]})
            before = sorted(str(path.relative_to(memory.directory))
                            for path in memory.directory.rglob('*'))
            self.assertTrue(before)
            with patch.object(memory, 'memory', side_effect=AssertionError('owner backend touched')), \
                    patch.object(deployment.Memory, 'connect', side_effect=AssertionError('database opened')):
                for index, body in enumerate(cases):
                    for owner in ('existing', 'new-owner'):
                        with self.subTest(case=index, owner=owner):
                            with self.assertLogs(LOGGER, level='WARNING') as logs:
                                self.assertEqual(request(port, '/search', {**body, 'user_id': owner}),
                                                 (200, b'{"data": []}'))
                            self.assertEqual([json.loads(r.getMessage())['error_type'] for r in logs.records],
                                             ['tolerated_empty_query'])
                            self.assertEqual(sorted(str(path.relative_to(memory.directory))
                                                    for path in memory.directory.rglob('*')), before)

    def test_empty_queries_still_validate_object_and_owner_first(self):
        cases = [None, [], 7, '', {}, {'query': ''}] + [
            query('', user_id=owner) for owner in ('', 'x' * 513, None, 7, [], {})]
        with serving(coding_search_tolerant=True) as (port, memory):
            with patch.object(memory, 'memory', side_effect=AssertionError('owner backend touched')):
                for index, body in enumerate(cases):
                    with self.subTest(case=index):
                        with self.assertLogs(LOGGER, level='WARNING') as logs:
                            self.assertEqual(request(port, '/search', body)[0], 422)
                        self.assertEqual(len(logs.records), 1)
                        diagnostic = json.loads(logs.records[0].getMessage())
                        self.assertEqual(diagnostic['field'], 'user_id' if isinstance(body, dict) else 'payload')
                        self.assertFalse(diagnostic['error_type'].startswith('tolerated_'))
                self.assertEqual(list(memory.directory.iterdir()), [])

    def test_exact_unicode_truncation_and_no_midpoint_leak(self):
        value = '  ' + '开' * 10000 + 'discard-this-middle' + '尾' * 10000 + '\n'
        result = tolerant_search(query(value, top_k=-50, options=['valid'], extra='unused'))
        self.assertEqual(result, query('开' * 10000 + '尾' * 10000, top_k=1, options=['valid']))
        self.assertEqual(len(result['query']), 20000)
        self.assertEqual(tolerant_search(query(' ' * 20001 + 'word'))['query'], 'word')
        self.assertEqual(tolerant_search(query('  short\n'))['query'], '  short\n')

    def test_optional_coercion_owner_boundary_and_add_stays_strict(self):
        for top in (None, True, '7', 1.5):
            self.assertEqual(tolerant_search(query(top_k=top))['top_k'], 100)
        self.assertEqual(tolerant_search({'user_id': 'alice', 'query': 'word'})['top_k'], 100)
        self.assertEqual(tolerant_search(query(options=['x' * 20001]))['options'], [])
        with serving(coding_search_tolerant=True) as (port, _):
            for owner in ('', 'x' * 513, None):
                with self.assertLogs(LOGGER, level='WARNING'):
                    self.assertEqual(request(port, '/search', query(user_id=owner))[0], 422)
            add = {'user_id': 'alice', 'session_id': 's1', 'request_id': 'one',
                   'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'word', 'extra': 1}]}]}
            with self.assertLogs(LOGGER, level='WARNING'):
                self.assertEqual(request(port, '/add', add)[0], 422)

    def test_coerced_payload_reaches_backend(self):
        with serving(coding_search_tolerant=True) as (port, _):
            with patch.object(deployment.Memory, 'search', return_value={'data': []}) as search:
                request(port, '/search', query([{'type': 'text', 'text': 'first'},
                    {'type': 'text', 'text': ''}, {'type': 'image'},
                    {'type': 'text', 'text': 'last', 'private-field': 'secret'}],
                    top_k=1000, options=[7], private='secret'))
                search.assert_called_once_with(query('first\nlast', top_k=100, options=[]))


class DiagnosticTests(unittest.TestCase):
    def test_422_logs_are_safe_precise_and_response_bytes_unchanged(self):
        cases = [(query('SENSITIVE' * 2501), 'query', 'max_length_20000', 'string', 22509),
                 (query(**{'SENSITIVE-key': 'SENSITIVE-body'}), 'payload', 'unknown_fields', 'object', 4),
                 (query(top_k=False), 'top_k', 'integer_range_1_100', 'boolean', None),
                 (query(options={'SENSITIVE-key': 'SENSITIVE-body'}), 'options', 'string_array_required', 'object', 1),
                 (query(options=['valid', 7]), 'options[1]', 'string_required', 'integer', None),
                 (query(''), 'query', 'nonempty_string_required', 'string', 0),
                 (query([{'type': 'text', 'text': ''}]), 'query[0].text', 'nonempty_string_required', 'string', 0),
                 (query([{'type': 'SENSITIVE-type', 'text': 'SENSITIVE-body'}]), 'query[0].type', 'text_part_required', 'string', 14),
                 (query([{'type': 'text', 'text': 'SENSITIVE-body', 'SENSITIVE-key': 1}]), 'query[0]', 'invalid_fields', 'object', 3)]
        with serving() as (port, _):
            for body, field, error_type, kind, length in cases:
                with self.subTest(field=field, error=error_type):
                    with self.assertLogs(LOGGER, level='WARNING') as logs:
                        self.assertEqual(request(port, '/search', body),
                            (422, b'{"error": "invalid_input_or_model_output"}'))
                    self.assertEqual(len(logs.records), 1)
                    message = logs.records[0].getMessage()
                    self.assertNotIn('SENSITIVE', message)
                    self.assertNotIn(KEY, message)
                    self.assertEqual(json.loads(message), {'field': field, 'error_type': error_type,
                                                         'type': kind, 'length': length})

    def test_json_framing_and_backend_errors_log_without_exception_messages(self):
        with serving() as (port, memory):
            with self.assertLogs(LOGGER, level='WARNING') as logs:
                self.assertEqual(request(port, '/search', b'{SENSITIVE', raw=True)[0], 422)
            self.assertEqual(json.loads(logs.records[0].getMessage())['error_type'], 'invalid_json')
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            connection.putrequest('POST', '/search')
            connection.putheader('Authorization', 'Bearer ' + KEY)
            connection.putheader('Content-Length', 'SENSITIVE')
            with self.assertLogs(LOGGER, level='WARNING') as logs:
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 422)
                response.read()
            connection.close()
            self.assertEqual(json.loads(logs.records[0].getMessage())['error_type'], 'invalid_framing')
            with patch.object(memory, 'search', side_effect=ValueError('SENSITIVE exception / address')):
                with self.assertLogs(LOGGER, level='WARNING') as logs:
                    self.assertEqual(request(port, '/search', query())[0], 422)
            self.assertNotIn('SENSITIVE', logs.records[0].getMessage())

    def test_enabled_mode_logs_empty_query_tolerance(self):
        with serving(coding_search_tolerant=True) as (port, _):
            with self.assertLogs(LOGGER, level='WARNING') as logs:
                self.assertEqual(request(port, '/search', query([{'type': 'image', 'text': 'SENSITIVE'}])),
                                 (200, b'{"data": []}'))
            self.assertEqual(len(logs.records), 1)
            self.assertEqual(json.loads(logs.records[0].getMessage()),
                {'field': 'query', 'error_type': 'tolerated_empty_query', 'type': 'array', 'length': 1})

    def test_each_tolerance_logs_one_redacted_warning(self):
        marker = 'SENSITIVE-MARKER'
        missing_top = query(marker)
        del missing_top['top_k']
        cases = [
            (query([{'type': 'image', 'text': marker}]), 'query', 'tolerated_empty_query', 'array', 1),
            (query(marker * 2000), 'query', 'tolerated_query_truncation', 'string', len(marker) * 2000),
            (query(' ' * 20001 + marker), 'query', 'tolerated_query_truncation', 'string', 20001 + len(marker)),
            (missing_top, 'top_k', 'tolerated_top_k', 'null', None),
            (query(marker, top_k=0), 'top_k', 'tolerated_top_k', 'integer', None),
            (query(marker, top_k=101), 'top_k', 'tolerated_top_k', 'integer', None),
            (query(marker, top_k=True), 'top_k', 'tolerated_top_k', 'boolean', None),
            (query(marker, top_k='bad'), 'top_k', 'tolerated_top_k', 'string', 3),
            (query(marker, options={marker: marker}), 'options', 'tolerated_options_discarded', 'object', 1),
            (query(marker, options=[7]), 'options', 'tolerated_options_discarded', 'array', 1),
            (query(marker, options=['x' * 20001]), 'options', 'tolerated_options_discarded', 'array', 1),
            (query(marker, **{marker: marker, 'CLIENT-FIELD': None}),
             'payload', 'tolerated_unknown_fields', 'object', 2),
            (query([{'type': 'text', 'text': marker}, {'type': 'text', 'text': 'two'}]),
             'query', 'tolerated_content_parts', 'array', 2)]
        with serving(coding_search_tolerant=True) as (port, _):
            for body, field, error_type, kind, length in cases:
                with self.subTest(error=error_type, kind=kind, length=length):
                    with self.assertLogs(LOGGER, level='WARNING') as logs:
                        self.assertEqual(request(port, '/search', body)[0], 200)
                    self.assertEqual(len(logs.records), 1)
                    record = logs.records[0]
                    self.assertEqual(record.levelname, 'WARNING')
                    message = record.getMessage()
                    for secret in (marker, 'CLIENT-FIELD', KEY):
                        self.assertNotIn(secret, message)
                    self.assertEqual(json.loads(message), {'field': field, 'error_type': error_type,
                                                         'type': kind, 'length': length})

    def test_combined_tolerances_and_nested_unknown_fields(self):
        marker = 'SENSITIVE-MARKER'
        body = query([{'type': 'text', 'text': marker * 2000, marker: marker}],
                     top_k=0, options=[7], **{marker: marker})
        with serving(coding_search_tolerant=True) as (port, _):
            with self.assertLogs(LOGGER, level='WARNING') as logs:
                self.assertEqual(request(port, '/search', body)[0], 200)
        diagnostics = [json.loads(record.getMessage()) for record in logs.records]
        self.assertEqual([d['error_type'] for d in diagnostics], [
            'tolerated_content_parts', 'tolerated_query_truncation', 'tolerated_top_k',
            'tolerated_options_discarded', 'tolerated_unknown_fields'])
        self.assertEqual(diagnostics[-1], {'field': 'payload', 'error_type': 'tolerated_unknown_fields',
                                         'type': 'object', 'length': 2})
        self.assertNotIn(marker, '\n'.join(logs.output))

    def test_valid_string_requests_emit_no_logs_and_match_strict_bytes(self):
        with serving() as (strict, _), serving(coding_search_tolerant=True) as (loose, _):
            for body in (query(), query('  中文\tquery\n', top_k=1, options=['one', 'two']),
                         query('x' * 20000, top_k=100, options=[])):
                with self.assertNoLogs(LOGGER, level='WARNING'):
                    self.assertEqual(request(loose, '/search', body), request(strict, '/search', body))


class ConfigTests(unittest.TestCase):
    def test_optional_flag_is_boolean_and_default_false(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            original = {'retrieval': 'lexical', 'data_dir': directory,
                        'api_key_file': directory + '/synthetic-key', 'host': '127.0.0.1', 'port': 8080}
            for flag in ('absent', False, True, 0, 1, 'false', None):
                config = original if flag == 'absent' else {**original, 'coding_search_tolerant': flag}
                path.write_text(json.dumps(config))
                if flag == 'absent' or type(flag) is bool:
                    self.assertEqual(deployment.read_config(path), config)
                else:
                    with self.assertRaises(ValueError): deployment.read_config(path)
            memory = deployment.DeploymentMemory(directory, None, None, ENCODING)
            self.assertIs(memory.coding_search_tolerant, False)

    def test_lexical_startup_passes_enabled_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'retrieval': 'lexical', 'data_dir': directory,
                      'api_key_file': directory + '/unused', 'host': '127.0.0.1', 'port': 8080,
                      'coding_search_tolerant': True}
            with patch.object(deployment, 'private_key', return_value=KEY), \
                    patch.object(deployment, 'server') as server:
                deployment.serve_lexical(config)
            self.assertIs(server.call_args.args[0].coding_search_tolerant, True)


if __name__ == '__main__':
    unittest.main()
