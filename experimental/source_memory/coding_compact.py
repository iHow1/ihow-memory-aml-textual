"""Deterministic, response-only coding evidence projection (no model or I/O).

Inspired by Hy-Memory's diff-chain decorator; independently implemented. Candidate
ranking and stored source are untouched. This is evidence, not proof an edit ran.
"""
import ast
import json
import re

# Organizer approved 8192 B/item and 65536 B/response on 2026-10-29.
# Count serialized UTF-8 JSON bytes, including metadata, escaping and structure.
MAX_RESPONSE_BYTES = 64 * 1024
MAX_ROW_BYTES = 8 * 1024
FALLBACK_BYTES = 2048
TRUNCATED = '\n[compact: truncated]'
HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*$')
PATH = re.compile(r'(?<![\w:/])(?:[\w.@+\-]+/)+[\w.@+\-]+(?:\.[\w+\-]+)?|'
                  r'(?<![\w.])[\w@+\-]+\.(?:py|js|mjs|cjs|ts|tsx|jsx|go|rs|java|rb|'
                  r'c|cc|cpp|h|hpp|cs|php|sh|sql|json|ya?ml|toml|ini|md|txt)(?!\w)')
DECISION = re.compile(r'\b(?:fix(?:ed|es)?|root cause|because|therefore|decision|'
                      r'resolved|solution|final(?: summary)?|conclusion)\b|'
                      r'根因|原因|修复|因此|结论|最终|决定', re.I)
ASSIGN = re.compile(r'\b(path|file_path|old|old_string|new|new_string)\s*=\s*'
                    r'(""".*?"""|\'\'\'.*?\'\'\'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')', re.S)


def wire(value):
    """Exactly the deployment HTTP response serializer, including JSON escapes."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def prefix(text, limit):
    return text.encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def clean_path(value, *, git=False):
    value = value.strip().strip('`"\'').split('\t', 1)[0]
    if git and value.startswith(('a/', 'b/')):
        value = value[2:]
    while value.startswith('./'):
        value = value[2:]
    return value if value and value != '/dev/null' else ''


def diff_hunks(text):
    """Read unified hunk counts; never absorb following dialogue as patch text.

    Both git headers and ordinary ---/+++ unified headers are supported. A
    malformed/incomplete hunk is not presented as a complete unified diff.
    """
    lines = text.replace('\r\n', '\n').splitlines()
    path = ''
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('diff --git '):
            match = re.match(r'diff --git (?:"a/.*?"|a/.*?) ("?b/.*?)$', line)
            path = clean_path(match[1], git=True) if match else ''
        elif line.startswith('--- '):
            path = clean_path(line[4:], git=True)
        elif line.startswith('+++ '):
            path = clean_path(line[4:], git=True) or path
        match = HUNK.fullmatch(line)
        if match and path:
            old, new = int(match[2] or 1), int(match[4] or 1)
            body = [line]
            j = i + 1
            while j < len(lines) and (old or new):
                part = lines[j]
                if part.startswith('\\ No newline at end of file'):
                    body.append(part)
                    j += 1
                    continue
                if not part or part[0] not in ' +-':
                    break
                a, b = int(part[0] in ' -'), int(part[0] in ' +')
                if a > old or b > new:
                    break
                old, new = old - a, new - b
                body.append(part)
                j += 1
            if old == new == 0:
                if j < len(lines) and lines[j].startswith('\\ No newline at end of file'):
                    body.append(lines[j])
                    j += 1
                result.append((path, '\n'.join(body)))
                i = j
                continue
        i += 1
    return result


def views_and_edits(text):
    """Decode explicit JSON objects/arguments and literal assignments only.

    No eval, execution, filesystem lookups or guessed edits. Bound nested JSON
    expansion; do not interpret arbitrary backslash sequences in plain text.
    """
    views, edits, seen = [text], [], {text}

    def edit(value):
        path = value.get('path', value.get('file_path'))
        old = value.get('old', value.get('old_string'))
        new = value.get('new', value.get('new_string'))
        if all(isinstance(x, str) for x in (path, old, new)) and old != new:
            path = clean_path(path)
            if path:
                edits.append((path, 'Edit action (execution unverified):\n' +
                              json.dumps({'path': path, 'old': old, 'new': new}, ensure_ascii=False)))

    def walk(value, depth=0):
        if depth > 8:
            return
        if isinstance(value, dict):
            edit(value)
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)
        elif isinstance(value, str) and value not in seen:
            seen.add(value)
            views.append(value)
            if value.lstrip().startswith(('{', '[')):
                try:
                    walk(json.loads(value), depth + 1)
                except (ValueError, RecursionError):
                    pass

    # A stored candidate may contain several rendered messages, each with JSON.
    decoder = json.JSONDecoder()
    end = 0
    for match in re.finditer(r'[\{\[]', text):
        if match.start() < end:
            continue
        try:
            value, end = decoder.raw_decode(text, match.start())
        except (ValueError, RecursionError):
            continue
        walk(value)
    # Group each literal edit at its path assignment; never pair across actions.
    for view in list(views):
        pending = {}
        for match in ASSIGN.finditer(view):
            key = match[1]
            if key in ('path', 'file_path'):
                if pending:
                    edit(pending)
                pending = {}
            try:
                pending[key] = ast.literal_eval(match[2])
            except (ValueError, SyntaxError, RecursionError):
                continue
        edit(pending)
    return views, edits


def extract(text):
    views, edits = views_and_edits(text)
    changes, paths, decisions = {}, [], []
    for view in views:
        for path, hunk in diff_hunks(view):
            changes.setdefault(path, [])
            if hunk not in changes[path]:
                changes[path].append(hunk)
    for path, action in edits:
        changes.setdefault(path, [])
        if action not in changes[path]:
            changes[path].append(action)
    paths.extend(changes)
    # Paths in prose are useful when a candidate has no recognizable edit.
    for view in views:
        for line in view.splitlines():
            git_header = line.startswith(('diff --git ', '--- ', '+++ '))
            if line.startswith(('--- ', '+++ ')) and line[4:].strip() == '/dev/null':
                continue
            for match in PATH.finditer(line):
                path = clean_path(match[0], git=git_header)
                if path and path not in paths and '://' not in line[max(0, match.start()-8):match.start()]:
                    paths.append(path)
        for line in view.splitlines():
            line = line.strip()
            if line and not line.startswith(('+', '-', '@@', '{', '[', '"', "'")) and DECISION.search(line):
                item = excerpt(line, 512)
                if item not in decisions:
                    decisions.append(item)
    # Last matching lines preferentially preserve a final explanation.
    return paths, changes, decisions[-4:]


def fit_content(row, content, fits):
    trial = {**row, 'content': content}
    if fits(trial):
        return trial
    low, high = 0, len(content)
    if not fits({**row, 'content': TRUNCATED}):
        return None
    while low < high:
        mid = (low + high + 1) // 2
        if fits({**row, 'content': content[:mid] + TRUNCATED}):
            low = mid
        else:
            high = mid - 1
    return {**row, 'content': content[:low] + TRUNCATED}


def validate_budgets(row_bytes, response_bytes):
    # Enough room for the empty response and a useful truncation marker.
    for value in (row_bytes, response_bytes):
        if type(value) is not int or value < 128:
            raise ValueError('compact byte budgets must be integers >= 128')


def excerpt(text, limit):
    if len(text.encode('utf-8')) <= limit:
        return text
    return prefix(text, limit - len(TRUNCATED.encode('utf-8'))) + TRUNCATED


def compact_search(result, *, max_row_bytes=MAX_ROW_BYTES,
                   max_response_bytes=MAX_RESPONSE_BYTES):
    """Keep memory-api data/id/content/score shape and original relevance order.

    First candidate owns a path. Within it, distinct hunks/actions survive in
    source order. Unstructured candidates retain a bounded raw prefix.
    """
    validate_budgets(max_row_bytes, max_response_bytes)
    output = {**result, 'data': []}
    seen_files = set()
    for original in result['data']:
        paths, changes, decisions = extract(original['content'])
        fresh = [path for path in paths if path not in seen_files]
        if paths and not fresh:
            continue
        blocks = ['File: ' + path for path in fresh]
        for path in fresh:
            if path in changes:
                blocks.append('Changes in ' + path + ':\n' + '\n'.join(changes[path]))
        if decisions:
            blocks.append('Decision evidence (verbatim lines):\n' + '\n'.join(decisions))
        if not changes:
            blocks.append('Raw excerpt (no recognized change):\n' + excerpt(original['content'], FALLBACK_BYTES))
        content = '\n\n'.join(blocks)

        def fits(row):
            return (len(wire(row)) <= max_row_bytes and
                    len(wire({**output, 'data': output['data'] + [row]})) <= max_response_bytes)

        row = fit_content(original, content, fits)
        if row is None:
            break
        output['data'].append(row)
        # A clipped path list must not hide lower-ranked evidence never emitted.
        retained = (content if row['content'] == content else
                    content[:len(row['content']) - len(TRUNCATED)])
        complete_lines = retained.split('\n')
        if len(retained) < len(content) and content[len(retained)] != '\n':
            complete_lines.pop()  # The marker's newline cannot complete a cut line.
        emitted = set(complete_lines)
        seen_files.update(path for path in fresh if 'File: ' + path in emitted)
    return output
