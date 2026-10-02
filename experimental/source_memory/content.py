"""Plain-text ContentPart[] input for memory-api-v1.1 Add and Search.

The public Coding contract lists string_or_content_parts with content type
text only. A part is exactly {"type": "text", "text": <non-empty string>}, as in
the official API guide. Parts are joined in order with one newline; each part's
own text, spaces and newlines are kept verbatim. The raw request envelope, not
the joined text, remains the request identity (see streaming.atomic_add).
"""

SEPARATOR = '\n'


def text(value):
    """Return a string unchanged or join text parts; reject everything else before any model call."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list) or not value:
        raise ValueError('content must be a string or non-empty text part array')
    texts = []
    for part in value:
        if not isinstance(part, dict):
            raise ValueError('invalid content part')
        if part.get('type') != 'text':
            # Never drop an image or unknown part and store the remaining text.
            raise ValueError('unsupported content part type')
        if set(part) != {'type', 'text'} or not isinstance(part['text'], str) or not part['text']:
            raise ValueError('invalid text content part')
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
        {**m, 'content': text(m['content'])} if isinstance(m, dict) and isinstance(m.get('content'), list) else m
        for m in messages]}


def normalize_search(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('query'), list):
        return payload
    return {**payload, 'query': text(payload['query'])}
