# iHow Memory AML Textual

A model-free memory service for the Agent Memory Leaderboard (AML) Textual
track, implementing the `/add` and `/search` endpoints of the memory API.

- **Add** stores every message verbatim in a per-user SQLite database. No
  model, no embedding, no summarisation.
- **Search** ranks the user's stored messages with user-local BM25, expands
  each hit with its neighbouring messages in the same session, and returns the
  original text with role and timestamp.
- **Zero model calls** in both Add and Search. The only third-party runtime
  package is `tiktoken`.

`SOURCE_MANIFEST.json` lists the SHA-256 of every source file and the private
development commit they were copied from. The deployed service runs exactly
these files. Method details are in [TECHNICAL_REPORT.md](TECHNICAL_REPORT.md);
origins and references are in [PROVENANCE.md](PROVENANCE.md).

## Requirements

- Linux or macOS (the service uses `fcntl` file locks)
- Python 3.11 or newer
- `pip install -r requirements.txt` (pins `tiktoken==0.12.0`)

On first use `tiktoken` downloads the `o200k_base` vocabulary from its
official host and caches it. Set `TIKTOKEN_CACHE_DIR` to pre-populate the cache
for offline hosts.

## Run the tests

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The tests use temporary directories and a fixed fixture key. They make no
network calls apart from the one-time `tiktoken` vocabulary download.

## Run the service

1. Create a data directory and an API key file. The key must be 20–512
   printable ASCII characters and the file must be private (mode 0600 or 0400).

   ```sh
   mkdir -p /srv/aml-memory/data
   python -c "import secrets; print(secrets.token_urlsafe(32))" > /srv/aml-memory/api.key
   chmod 600 /srv/aml-memory/api.key
   ```

2. Copy `config.example.json` and adjust the paths. The lexical configuration
   has exactly five fields, and all paths must be absolute:

   | Field | Meaning |
   |---|---|
   | `retrieval` | must be `"lexical"` |
   | `data_dir` | directory for per-user databases and the process lock |
   | `api_key_file` | file holding the API key |
   | `host` | `127.0.0.1` (default; put a TLS reverse proxy in front) or `0.0.0.0` |
   | `port` | 1024–65535 |

3. Check and start:

   ```sh
   python -m experimental.source_memory.deployment --config /srv/aml-memory/config.json --check-config
   python -m experimental.source_memory.deployment --config /srv/aml-memory/config.json
   ```

   Stop the service with SIGINT. Only one process may own a `data_dir`.

## API

Authenticate with `Authorization: Bearer <key>`, `Authorization: Token <key>`
or `X-Api-Key: <key>`. Requests and responses are JSON.

| Endpoint | Behaviour |
|---|---|
| `GET /health` | No auth. Returns `{"status":"ok","mode":"source-memory-lexical-v1"}` |
| `POST /add` | `{user_id, session_id, request_id, messages:[{role, content, timestamp?}]}`. `role` is `user` or `assistant`; `content` is a string or an array of `{"type":"text","text":...}` parts; `timestamp` is Unix milliseconds. Returns `{user_id, session_id, request_id, success:true}` |
| `POST /search` | `{user_id, query, top_k, options?}`, with `top_k` 1–100. Returns `{"data":[{id, content, score, created_at?}]}` |

Behaviour worth knowing:

- An Add is idempotent by `(user_id, request_id)`. Repeating it with the same
  body returns the same result; the same ID with a different body returns 409.
- Up to 10,000 messages or 2 MiB per Add. Large Adds are processed in batches
  of at most 20 messages / 2,000 words in a private staging copy and published
  atomically, so a failed Add exposes no partial data.
- Each `user_id` has its own database, and ranking statistics never cross
  users.
- Add and Search run one at a time. Other requests wait in arrival order
  (first in, first out) after their body is read and parsed as JSON. A request
  still waiting after 900 seconds gets `429 {"error":"capacity_busy"}` with
  `Retry-After: 60`. Up to 64 connections are held; further connections get a
  bare 429 with `Retry-After: 60`. On shutdown, waiting requests get the same
  429 and only the running one finishes.
- A reverse proxy in front must allow a long upstream wait, longer than the
  900-second queue (for example nginx `proxy_read_timeout 1500s`), and must not
  retry requests itself.
- Other errors: 401 (auth), 413 (body size), 422 (invalid input), 503
  (backend unavailable).
- The service writes no request or payload logs.

## Scope of this repository

This repository holds the deployed lexical mode only. `deployment.py` also
contains an older model-assisted mode (`--allow-model-calls`) whose transport
and embedding modules are not included; that mode is not part of this entry
and fails at import. `model.py`, `span_model.py` and `unique_span_model.py` are
included because the lexical code imports them (the annotation prompt text is
part of the database identity hash), but they make no calls in lexical mode.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
