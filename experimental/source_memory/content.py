"""Plain-text ContentPart[] input for memory-api-v1.1 Add and Search.

The public Coding contract lists string_or_content_parts with content type
text only. A part is exactly {"type": "text", "text": <non-empty string>}, as in
the official API guide. Parts are joined in order with one newline; each part's
own text, spaces and newlines are kept verbatim. The raw request envelope, not
the joined text, remains the request identity (see streaming.atomic_add).
"""

import json
from .validation import InvalidInput, log_tolerance

SEPARATOR = '\n'


def text(value, field='content'):
    """Return a string unchanged or join text parts; reject everything else before any model call."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list) or not value:
        raise InvalidInput('content must be a string or non-empty text part array', field, 'string_or_nonempty_parts_required', value)
    texts = []
    for index, part in enumerate(value):
        path = f'{field}[{index}]'
        if not isinstance(part, dict):
            raise InvalidInput('invalid content part', path, 'object_required', part)
        if part.get('type') != 'text':
            # Never drop an image or unknown part and store the remaining text.
            raise InvalidInput('unsupported content part type', path + '.type', 'text_part_required', part.get('type'))
        if set(part) != {'type', 'text'}:
            raise InvalidInput('invalid text content part', path, 'invalid_fields', part)
        if not isinstance(part['text'], str) or not part['text']:
            raise InvalidInput('invalid text content part', path + '.text', 'nonempty_string_required', part['text'])
        texts.append(part['text'])
    return SEPARATOR.join(texts)


def normalize_add(payload):
    """Return the payload itself when every content is a string, so string requests are unchanged."""
    if not isinstance(payload, dict) or not isinstance(payload.get('messages'), list):
        return payload
    messages = payload['messages']
    if not any(isinstance(m, dict) and isinstance(m.get('content'), list) for m in messages):
        return payload
    return {**payload, 'messages': [
        {**m, 'content': text(m['content'], f'messages[{index}].content')} if isinstance(m, dict) and isinstance(m.get('content'), list) else m
        for index, m in enumerate(messages)]}


def normalize_search(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('query'), list):
        return payload
    return {**payload, 'query': text(payload['query'], 'query')}


def tolerant_search(payload):
    """Search-only coercion; Add and strict normalization stay lossless.

    Long text: strip outer whitespace, then first/last 10,000 Unicode
    code points (20,000 total, no marker). Short strings stay verbatim.
    Missing/non-integer top_k defaults to 100; integers clamp to 1..100.
    Invalid or oversized options are ignored. Validate the owner before any
    coercion; an empty query signals an empty result without owner I/O.
    """
    if not isinstance(payload, dict):
        raise InvalidInput('invalid Search fields', 'payload', 'object_required', payload)
    owner = payload.get('user_id')
    if not isinstance(owner, str) or not 0 < len(owner) <= 512:
        raise InvalidInput('invalid Search', 'user_id', 'string_length_1_512', owner)
    unknown = sum(key not in {'user_id', 'query', 'top_k', 'options'} for key in payload)
    value = payload.get('query')
    if isinstance(value, list):
        unknown += sum(key not in {'type', 'text'} for part in value
                       if isinstance(part, dict) for key in part)
        query = SEPARATOR.join(part['text'] for part in value
            if isinstance(part, dict) and part.get('type') == 'text'
            and isinstance(part.get('text'), str) and part['text'].strip())
        if query:
            log_tolerance('query', 'tolerated_content_parts', value)
    else:
        query = value
    if not isinstance(query, str) or not query.strip():
        log_tolerance('query', 'tolerated_empty_query', value)
        query = ''
    if len(query) > 20000:
        log_tolerance('query', 'tolerated_query_truncation', query)
        query = query.strip()
        if len(query) > 20000:
            query = query[:10000] + query[-10000:]
    top = payload.get('top_k')
    if type(top) is not int or not 1 <= top <= 100:
        log_tolerance('top_k', 'tolerated_top_k', top)
    top = min(100, max(1, top)) if type(top) is int else 100
    options = payload.get('options', [])
    if (not isinstance(options, list) or any(not isinstance(x, str) for x in options)
            or len(json.dumps(options)) > 20000):
        log_tolerance('options', 'tolerated_options_discarded', options)
        options = []
    if unknown:
        log_tolerance('payload', 'tolerated_unknown_fields', payload, count=unknown)
    return {'user_id': owner, 'query': query, 'top_k': top, 'options': options}
