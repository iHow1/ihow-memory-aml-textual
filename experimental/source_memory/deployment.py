"""Single-process Textual deployment adapter; no evaluation datasets are loaded."""
import argparse
from collections import deque
from contextlib import contextmanager
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import stat
import sys
import threading
import time

from .store import Memory, Conflict, Incomplete, digest
from .lexical import NoAnnotator
from .unique_span_model import UniqueSpanAnnotator
from .streaming import batches, atomic_add
from .content import normalize_add, normalize_search

ROOT = Path(__file__).resolve().parents[2]


def private_key(path):
    path = Path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('key file must be private (0600 or 0400)')
    key = path.read_text().strip()
    if not 20 <= len(key) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('invalid key file')
    return key


LEXICAL_FIELDS = {'retrieval', 'data_dir', 'api_key_file', 'host', 'port'}
MODEL_FIELDS = {'data_dir', 'model_path', 'python_path', 'api_key_file',
                'model_key_file', 'host', 'port', 'embedding_threads', 'stages'}


def read_config(path):
    value = json.loads(Path(path).read_text())
    # Two exact shapes: model-free lexical (no model/embedding fields at all) or
    # the original model candidate. A partial mix is rejected, not defaulted.
    if not isinstance(value, dict) or set(value) not in (LEXICAL_FIELDS, MODEL_FIELDS):
        raise ValueError('invalid configuration fields')
    if 'retrieval' in value and value['retrieval'] != 'lexical':
        raise ValueError('invalid retrieval mode')
    for k in sorted(set(value) & {'data_dir', 'model_path', 'python_path', 'api_key_file', 'model_key_file'}):
        if not isinstance(value[k], str) or not Path(value[k]).is_absolute():
            raise ValueError('configuration paths must be absolute')
    if value['host'] not in ('127.0.0.1', '0.0.0.0'):
        raise ValueError('invalid listen host')
    if type(value['port']) is not int or not 1024 <= value['port'] <= 65535:
        raise ValueError('invalid port')
    if 'retrieval' in value:
        return value
    if type(value['embedding_threads']) is not int or not 1 <= value['embedding_threads'] <= 32:
        raise ValueError('invalid embedding thread count')
    stages = value['stages']
    if not isinstance(stages, dict) or set(stages) != {'add', 'search'}:
        raise ValueError('explicit Add and Search budgets required')
    for pair in stages.values():
        if (not isinstance(pair, list) or len(pair) != 2 or type(pair[0]) is not int
                or pair[0] < 1 or type(pair[1]) not in (int, float)
                or not math.isfinite(pair[1]) or pair[1] <= 0):
            raise ValueError('positive call and cost ceilings required')
    return value


@contextmanager
def deployment_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'deployment.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another deployment process owns this data directory') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class DeploymentMemory:
    """Serialize requests and bind model replays to owner plus memory revision.

    The evaluated engine, prompts, source selection and ranking are unchanged.
    Each owner gets its own database, as in the completed 44-question run.
    transport=None with embedder=None is the model-free lexical mode: raw
    messages only, owner-local BM25, no model or embedding call anywhere.
    """
    def __init__(self, directory, transport, embedder, encoding):
        if transport is None and embedder is not None:
            raise ValueError('lexical mode takes no embedder')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.transport, self.embedder, self.encoding = transport, embedder, encoding
        self.lock = threading.RLock()

    def annotator(self, complete):
        return NoAnnotator() if self.transport is None else UniqueSpanAnnotator(complete)

    def memory(self, owner, complete):
        return Memory(self.directory / (digest(owner) + '.sqlite'),
                      self.annotator(complete), self.embedder, self.encoding)

    def add(self, payload):
        # Content parts are validated and joined before batching, so every
        # rejection happens before a model call and both the single-batch and
        # cross-batch paths receive text. The raw body stays the identity.
        source, payload = payload, normalize_add(payload)
        chunks = batches(payload, identity=digest(source))
        with self.lock:
            def execute(path, chunk):
                ident = 'add:' + digest([chunk['user_id'], chunk['request_id']])
                annotator = self.annotator(lambda messages: self.transport.complete(
                    'add', ident, messages, strict=True, max_tokens=1024))
                return Memory(path, annotator, self.embedder, self.encoding).add(chunk)
            memory = self.memory(payload['user_id'], None)
            return atomic_add(memory, payload, chunks, execute, source=source)

    def search(self, payload):
        source, payload = payload, normalize_search(payload)
        if not isinstance(payload, dict) or not {'user_id', 'query', 'top_k'} <= set(payload):
            raise ValueError('invalid Search fields')
        if set(payload) - {'user_id', 'query', 'top_k', 'options'}:
            raise ValueError('invalid Search fields')
        owner, query, top = payload['user_id'], payload['query'], payload['top_k']
        options = payload.get('options', [])
        if (not isinstance(owner, str) or not 0 < len(owner) <= 512
                or not isinstance(query, str) or not query.strip() or len(query) > 20000
                or type(top) is not int or not 1 <= top <= 100
                or not isinstance(options, list) or any(not isinstance(x, str) for x in options)
                or len(json.dumps(options)) > 20000):
            raise ValueError('invalid Search')
        with self.lock:
            # Search has no official request_id. Include the committed memory
            # revision so the same question after an incremental Add is new work.
            def complete(messages):
                return self.transport.complete('search', ident, messages, max_tokens=1024)
            memory = self.memory(owner, complete)
            if self.transport is not None:
                with memory.connect() as db:
                    revision = [tuple(r) for r in db.execute(
                        "SELECT request,hash FROM requests WHERE state='complete' ORDER BY rowid")]
                ident = 'search:' + digest([owner, revision, source])
            result = memory.search(payload)
            for row in result['data']:
                if row.get('created_at') is None:
                    row.pop('created_at', None)
            return result

    def health(self):
        # No counts, owner identifiers, prompt bodies or billing data in public health.
        if self.transport is None:
            return {'status': 'ok', 'mode': 'source-memory-lexical-v1'}
        with self.transport.db() as db:
            stopped = db.execute("SELECT v FROM meta WHERE k='stopped'").fetchone()[0]
        return {'status': 'unavailable' if stopped else 'ok',
                'mode': 'source-memory-textual-deployment-v1'}


class FifoSlot:
    """One operation at a time; waiting requests are served in arrival order.

    Same acquire/release shape as a semaphore. A newcomer never overtakes a
    waiter, and a waiter that times out leaves the queue without blocking others.
    """
    def __init__(self):
        self.cond = threading.Condition()
        self.waiters = deque()
        self.busy = False
        self.closed = False

    def acquire(self, blocking=True, timeout=None):
        with self.cond:
            if self.closed:
                return False
            if not self.busy and not self.waiters:
                self.busy = True
                return True
            if not blocking:
                return False
            ticket = object()
            self.waiters.append(ticket)
            deadline = None if timeout is None else time.monotonic() + timeout
            try:
                while True:
                    if self.closed:
                        return False
                    left = None if deadline is None else deadline - time.monotonic()
                    if left is not None and left <= 0:
                        return False
                    if not self.busy and self.waiters[0] is ticket:
                        self.busy = True
                        return True
                    self.cond.wait(left)
            finally:
                self.waiters.remove(ticket)
                self.cond.notify_all()

    def close(self):
        """Cancel waiting operations; let the already active operation finish."""
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def release(self):
        with self.cond:
            if not self.busy:
                raise ValueError('slot released while free')
            self.busy = False
            self.cond.notify_all()


# The platform declares 16-64 Add and 16-256 Search workers, retries 429 after
# 60 s (at most 32 Add attempts) and lets one request run up to 30 minutes.
# Waiting in a queue costs one service time; a 429 costs 60 s and an attempt.
CONNECTIONS = 64
QUEUE_TIMEOUT = 900
SOCKET_TIMEOUT = 30


def server(memory, *, key, host='127.0.0.1', port=0,
           connections=CONNECTIONS, queue_timeout=QUEUE_TIMEOUT):
    if not isinstance(key, str) or len(key) < 20 or not key.isascii():
        raise ValueError('a dedicated nonempty API key is required')
    if host not in ('127.0.0.1', '0.0.0.0'):
        raise ValueError('invalid listen host')
    if type(connections) is not int or not 16 <= connections <= 256:
        raise ValueError('connection capacity must cover the declared concurrency (16-256)')
    if type(queue_timeout) not in (int, float) or not 0 < queue_timeout <= 1500:
        raise ValueError('queue timeout must be positive and well under 30 minutes')

    class Handler(BaseHTTPRequestHandler):
        server_version = 'iHowMemory'
        sys_version = ''

        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(SOCKET_TIMEOUT)

        def send(self, status, value):
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            if status == 429:
                self.send_header('Retry-After', '60')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path != '/health':
                return self.send(404, {'error': 'not_found'})
            try:
                health = memory.health()
                self.send(200 if health['status'] == 'ok' else 503, health)
            except Exception:
                self.send(503, {'error': 'backend_unavailable'})

        def do_POST(self):
            if self.path not in ('/add', '/search'):
                return self.send(404, {'error': 'not_found'})
            header = self.headers.get('Authorization', '').split(' ', 1)
            supplied = self.headers.get('X-Api-Key', '')
            if len(header) == 2 and header[0].lower() in ('bearer', 'token'):
                supplied = header[1]
            if not hmac.compare_digest(supplied.encode(), key.encode()):
                return self.send(401, {'error': 'unauthorized'})
            # Read the body before queueing: a slow upload holds a connection
            # (socket timeout applies), never the operation slot.
            body = self.read_body()
            if isinstance(body, tuple):
                return self.send(*body)
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                return self.send(422, {'error': 'invalid_input_or_model_output'})
            if not self.server.operation.acquire(timeout=self.server.queue_timeout):
                return self.send(429, {'error': 'capacity_busy'})
            try:
                status, value = self.operate(payload)
            finally:
                # Free the slot before replying: a client that sends its next request
                # as soon as it reads this response must not find the slot still taken.
                self.server.operation.release()
            self.send(status, value)

        def read_body(self):
            """The body bytes, or an error (status, value) pair."""
            try:
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                    raise ValueError('invalid HTTP framing')
                length = int(self.headers['Content-Length'])
                if not 0 < length <= 2 * 1024 * 1024:
                    return 413, {'error': 'body_too_large_or_empty'}
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError('incomplete body')
                return body
            except (ValueError, TypeError, KeyError, UnicodeError):
                return 422, {'error': 'invalid_input_or_model_output'}
            except Exception:
                return 503, {'error': 'backend_unavailable'}

        def operate(self, payload):
            try:
                return 200, memory.add(payload) if self.path == '/add' else memory.search(payload)
            except Conflict:
                return 409, {'error': 'request_conflict'}
            except Incomplete:
                return 503, {'error': 'incomplete_no_automatic_model_retry'}
            except (ValueError, TypeError, KeyError, UnicodeError):
                return 422, {'error': 'invalid_input_or_model_output'}
            except Exception:
                return 503, {'error': 'backend_unavailable'}

    class BoundedServer(ThreadingHTTPServer):
        daemon_threads = False

        def __init__(self, address):
            # Listen backlog must be set before super().__init__ calls listen().
            self.capacity = self.request_queue_size = connections
            self.queue_timeout = queue_timeout
            self.connections = threading.BoundedSemaphore(connections)
            self.operation = FifoSlot()
            super().__init__(address, Handler)

        def process_request(self, request, address):
            if not self.connections.acquire(blocking=False):
                try:
                    request.sendall(b'HTTP/1.1 429 Too Many Requests\r\nRetry-After: 60\r\n'
                                    b'Content-Length: 0\r\nConnection: close\r\n\r\n')
                finally:
                    self.shutdown_request(request)
                return
            try:
                super().process_request(request, address)
            except BaseException:
                self.connections.release()
                raise

        def process_request_thread(self, request, address):
            try:
                super().process_request_thread(request, address)
            finally:
                self.connections.release()

        def server_close(self):
            # Do not drain a 900-second queue during a 40-second service stop.
            # Pending requests have not touched persistence or a model and can
            # safely retry; only the currently active operation is allowed to finish.
            self.operation.close()
            super().server_close()

    return BoundedServer((host, port))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--check-config', action='store_true')
    parser.add_argument('--allow-model-calls', action='store_true')
    args = parser.parse_args()
    config = read_config(args.config)
    lexical = 'retrieval' in config
    if args.check_config:
        print(json.dumps({'config_valid': True, 'model_calls': 0,
                          'retrieval': 'lexical' if lexical else 'model',
                          'note': 'paths, dependencies, credentials and capacity not checked'}))
        return
    if lexical:
        if args.allow_model_calls:
            parser.error('lexical mode makes no model calls; drop --allow-model-calls')
        return serve_lexical(config)
    if not args.allow_model_calls:
        parser.error('serving requires explicit --allow-model-calls and an approved budget')
    os.umask(0o077)
    api_key = private_key(config['api_key_file'])
    model_key = private_key(config['model_key_file'])
    if hmac.compare_digest(api_key, model_key):
        raise ValueError('API auth and model keys must differ')
    # Reuse the proven receipt/budget implementation. Full source checkout is
    # currently required; a standalone Linux bundle remains a separate gate.
    sys.path.insert(0, str(ROOT / 'scripts'))
    from source_eval_transport_v4 import RunTransport
    from .portable_embedding import PortableBGE
    import tiktoken
    with deployment_lock(config['data_dir']):
        # One upstream timeout or 5xx fails that request only. Safety breaches and
        # pending calls left by a crashed process still stop all model calls.
        transport = RunTransport(Path(config['data_dir']) / 'billing.sqlite', model_key,
                                 stages=config['stages'], failure_scope='request')
        embedder = PortableBGE(ROOT, model_path=config['model_path'],
                               python_path=config['python_path'], threads=config['embedding_threads'])
        try:
            memory = DeploymentMemory(Path(config['data_dir']) / 'owners', transport,
                                      embedder, tiktoken.get_encoding('o200k_base'))
            httpd = server(memory, key=api_key, host=config['host'], port=config['port'])
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                httpd.server_close()
        finally:
            embedder.close()


def serve_lexical(config):
    # No model key, transport, billing ledger or embedding worker is loaded.
    os.umask(0o077)
    api_key = private_key(config['api_key_file'])
    import tiktoken
    with deployment_lock(config['data_dir']):
        memory = DeploymentMemory(Path(config['data_dir']) / 'owners', None, None,
                                  tiktoken.get_encoding('o200k_base'))
        httpd = server(memory, key=api_key, host=config['host'], port=config['port'])
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()


if __name__ == '__main__':
    main()
