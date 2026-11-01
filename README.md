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

`SOURCE_MANIFEST.json` lists the SHA-256 of the runtime and test files and the
private development commit they were copied from. On this branch, runtime
files match the archived Textual deployment source at `d57b845` byte for byte.
Method details are in [TECHNICAL_REPORT.md](TECHNICAL_REPORT.md);
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
   has five required fields and an optional boolean `coding_search_tolerant`, and all paths must be absolute:

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
- The service writes no request or payload values to logs. HTTP 422 rejection
  logs contain only fixed field paths, error types, JSON types and lengths.
  Tolerated Search inputs also emit warnings with this metadata; unknown
  fields are logged only as a count, without their names or values.

## Scope of this repository

This repository holds the deployed lexical mode only. `deployment.py` also
contains an older model-assisted mode (`--allow-model-calls`) whose transport
and embedding modules are not included; that mode is not part of this entry
and fails at import. `model.py`, `span_model.py` and `unique_span_model.py` are
included because the lexical code imports them (the annotation prompt text is
part of the database identity hash), but they make no calls in lexical mode.

## Coding track version

Branch `coding-tolerant-f4fb4d2` publishes the coding track deployment from
private source commit `f4fb4d297262880b324ad2b9fc220b172ec95589`, matching the
runtime source in deployed image `ihow-aml-coding:f4fb4d2`. It is based on the
Textual `lexical-107ac55` service. Add and Search make zero model calls.

That historical branch rejects empty text with 422. The Textual deployment
published on this branch has the later behavior described below.

## Textual track deployed version

Branch `text-tolerant-d57b845` publishes the Textual runtime from private
source commit `d57b845ef9a34e9e983e4b98ee50f064e6849c10`. The registered
version name remains `lexical-107ac55`; `main` retains the registration-time
source. Since `2026-10-23T15:06Z`, the Textual service has used the `107ac55`
baseline plus the `d57b845` tolerant Search changes, with
`"coding_search_tolerant": true`. Its URL, key and previously stored data
were unchanged.

With tolerance enabled, queries longer than 20,000 Unicode code points are
trimmed at their outer whitespace and, if still too long, reduced to their
first and last 10,000 code points. Short strings stay verbatim. Usable text
parts are joined with newlines; missing, non-text or whitespace-only queries
return HTTP 200 with `{"data": []}` after object and user validation, without
opening or creating an owner database. Integer `top_k` is clamped to 1–100;
missing or non-integer values default to 100. Invalid or oversized options
and unknown fields are ignored. These rules change retrieval behavior; later
Textual retrieval ran on this changed code. The default and explicit `false`
retain strict Search validation. Add validation, authentication, request size
limits and user isolation retain their existing behavior.

See [TEXT_TRACK_CHANGE_NOTE.md](TEXT_TRACK_CHANGE_NOTE.md) for the deployment
history, reason for the change and its effects.

The added regression test compares strict responses with the actual code
from public parent commit `c45d23c5fb8ff1e2c44ed3470704d2e9ec51a741`, which
published private baseline `107ac55`. `SOURCE_MANIFEST.json` discloses this
test-only revision adjustment and hashes both its original and public forms.
Internal development documentation is represented by this section rather
than copied into the public runtime tree.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
