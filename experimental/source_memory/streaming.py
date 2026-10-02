"""Stage large logical writes, then publish with SQLite's atomic backup transaction."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from .store import Conflict, Incomplete, Memory, digest


def batches(payload, identity=None):
    if not isinstance(payload, dict) or set(payload) != {'user_id', 'session_id', 'request_id', 'messages'}:
        raise ValueError('invalid Add fields')
    for field in ('user_id', 'session_id', 'request_id'):
        if not isinstance(payload[field], str) or not 0 < len(payload[field]) <= 512:
            raise ValueError('invalid Add identity')
    messages = payload['messages']
    if not isinstance(messages, list) or not 1 <= len(messages) <= 10000:
        raise ValueError('invalid messages')
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
        raise ValueError('Add exceeds deployment body capacity')
    parts = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get('content'), str):
            raise ValueError('invalid message')
        if message.get('role') not in ('user', 'assistant'):
            raise ValueError('unsupported Textual role')
        body = message['content']
        words = list(re.finditer(r'\S+', body))
        boundaries = [0] + [words[i].start() for i in range(2000, len(words), 2000)] + [len(body)]
        for start, end in zip(boundaries, boundaries[1:]):
            part = {**message, 'content': body[start:end]}
            Memory.validate({**payload, 'messages': [part]})
            parts.append(part)
    groups, group, count = [], [], 0
    for part in parts:
        n = len(part['content'].split())
        if group and (len(group) == 20 or count + n > 2000):
            groups.append(group)
            group, count = [], 0
        group.append(part)
        count += n
    if group:
        groups.append(group)
    if len(groups) == 1 and groups[0] == messages:
        return [payload]
    # identity is the raw envelope digest; for string requests it equals digest(payload).
    identity = digest(payload) if identity is None else identity
    return [{**payload, 'request_id': 'stream-chunk:' + digest([payload['request_id'], identity, i]),
             'messages': group} for i, group in enumerate(groups)]


def backup(source, target):
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dest:
        # The destination update is one SQLite transaction, including when it uses WAL.
        # Callers hold the deployment lock; no other writer can race this publication.
        src.backup(dest)


def atomic_add(memory, payload, chunks, execute, source=None):
    """Caller serializes owner writes; failed model requests are never reset/reissued.

    payload is the normalized text request that is stored and annotated. source
    is the raw request envelope (default: payload); its digest is the request
    identity, so two raw bodies that normalize to the same text still conflict.
    """
    source = payload if source is None else source
    owner, request = payload['user_id'], payload['request_id']
    fingerprint = digest(source)
    result = {k: payload[k] for k in ('request_id', 'user_id', 'session_id')}
    result['success'] = True
    with memory.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS streaming_requests(
            request TEXT PRIMARY KEY, hash TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL, chunks INTEGER NOT NULL)''')
        # Raw identity of requests whose stored text was normalized from content parts.
        db.execute('''CREATE TABLE IF NOT EXISTS request_sources(
            owner TEXT NOT NULL, request TEXT NOT NULL, hash TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(owner, request))''')
        bound = db.execute('SELECT hash FROM request_sources WHERE owner=? AND request=?', (owner, request)).fetchone()
        old = db.execute('SELECT hash,state FROM requests WHERE owner=? AND request=?', (owner, request)).fetchone()
        known = bound['hash'] if bound else old['hash'] if old else None
        if known is not None and known != fingerprint:
            raise Conflict('request payload changed')
        if old:
            if old['state'] != 'complete':
                raise Incomplete('request requires explicit recovery')
            return result
        pending = db.execute("SELECT request,hash FROM streaming_requests WHERE state!='complete'").fetchone()
        if pending:
            if pending['request'] != request:
                raise Incomplete('finish prior owner write before accepting another')
            if pending['hash'] != fingerprint:
                raise Conflict('request payload changed')
        if source is not payload:
            # Bound before any model call, so a failed attempt still rejects a different raw body.
            db.execute('INSERT OR IGNORE INTO request_sources VALUES (?,?,?,?)',
                       (owner, request, fingerprint, json.dumps(source, ensure_ascii=False)))
        if len(chunks) > 1:
            db.execute('INSERT OR IGNORE INTO streaming_requests VALUES (?,?,?,?,?)',
                       (request, fingerprint, json.dumps(source, ensure_ascii=False), 'pending', len(chunks)))
    if len(chunks) == 1:
        return execute(memory.path, payload)

    stage_dir = memory.path.parent / '.staging' / digest(owner)
    stage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = stage_dir / (digest(request) + '.sqlite')
    # Crash-safe initialization: an interrupted backup never appears as a ready stage.
    if not stage.exists():
        initial = stage.with_suffix('.initial.sqlite')
        backup(memory.path, initial)
        with closing(sqlite3.connect(initial)) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise Incomplete('staging database failed integrity check')
        initial.replace(stage)
    for chunk in chunks:
        # Already-complete chunks replay from the stage without a model call.
        # A failed/uncertain chunk remains failed until explicit reconciliation.
        execute(stage, chunk)
    with closing(sqlite3.connect(stage)) as db, db:
        db.execute('PRAGMA synchronous=FULL')
        old = db.execute('SELECT hash,state FROM requests WHERE owner=? AND request=?', (owner, request)).fetchone()
        if old is None:
            db.execute('INSERT INTO requests VALUES (?,?,?,?,?)',
                       (owner, request, fingerprint, json.dumps(source, ensure_ascii=False), 'complete'))
        elif old != (fingerprint, 'complete'):
            raise Conflict('staging parent conflict')
        db.execute("UPDATE streaming_requests SET state='complete' WHERE request=?", (request,))
    # Search reads the original database until this single atomic publication.
    backup(stage, memory.path)
    # The live DB retains the original envelope/chunk journals and the billing DB
    # retains all receipts. Completed full-owner staging copies need not accumulate.
    for suffix in ('', '-wal', '-shm'):
        try:
            Path(str(stage) + suffix).unlink(missing_ok=True)
        except OSError:
            pass  # Publication succeeded; an orphan cleanup must not report Add failure.
    return result
