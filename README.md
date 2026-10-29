# iHow Memory AML Textual

A model-free memory service derived from the Agent Memory Leaderboard (AML)
Textual entry, implementing the `/add` and `/search` endpoints of the memory
API. This branch packages the separate Coding new version described below.

- **Add** stores every message verbatim in a per-user SQLite database. No
  model, no embedding, no summarisation.
- **Search** ranks the user's stored messages with user-local BM25, expands
  each hit with its neighbouring messages in the same session, and returns the
  original text with role and timestamp when compact projection is disabled.
  The Coding configuration applies the lossy projection described below.
- **Zero model calls** in both Add and Search. The only third-party runtime
  package is `tiktoken`.

`SOURCE_MANIFEST.json` lists the SHA-256 of every source file and the private
development commit they were copied from, with test-only adjustments disclosed.
This Coding package has only been prepared locally; deployment, registration,
Coding Smoke and Full have not been performed for this version. Method details
are in [TECHNICAL_REPORT.md](TECHNICAL_REPORT.md);
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

The tests use temporary directories, synthetic fixtures and fixed fixture
keys. HTTP tests use loopback only. With the vocabulary cache pre-populated,
the suite runs offline. Baseline comparisons use `git show` on the public
parent commit, so retain Git history when running the full suite.

## Run the service

1. Create a data directory and an API key file. The key must be 20–512
   printable ASCII characters and the file must be private (mode 0600 or 0400).

   ```sh
   mkdir -p /srv/aml-memory/data
   python -c "import secrets; print(secrets.token_urlsafe(32))" > /srv/aml-memory/api.key
   chmod 600 /srv/aml-memory/api.key
   ```

2. Copy `config.example.json` and adjust the paths. The lexical configuration
   has five required fields plus the optional Coding fields below, and all
   paths must be absolute:

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
| `POST /search` | `{user_id, query, top_k, options?}`, with strict integer `top_k` 1–1000 on this branch. Returns `{"data":[{id, content, score, created_at?}]}` |

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

This repository packages the lexical mode only. `deployment.py` also
contains an older model-assisted mode (`--allow-model-calls`) whose transport
and embedding modules are not included; that mode is not part of this entry
and fails at import. `model.py`, `span_model.py` and `unique_span_model.py` are
included because the lexical code imports them (the annotation prompt text is
part of the database identity hash), but they make no calls in lexical mode.

## Coding new version

Branch `coding-new-2b1e3b3` packages version `lexical-coding-2b1e3b3`, derived
directly from Textual `lexical-107ac55`, at private source commit
`2b1e3b3aeafd17928b46ff22378d7f095cc57978`. It is not based on the tolerant
Coding branch. Its single public commit is based on
`c45d23c5fb8ff1e2c44ed3470704d2e9ec51a741`, which publishes private `107ac55`.
All runtime files are byte-identical to the fixed source commit. The manifest
records all runtime/test hashes and the test-only public baseline revision
and config path adjustments. Internal development notes and the measurement
helper are summarized here rather than copied into this tree.
The internal offline helper measured serialized response bytes, row counts
and source-file-path proxy retention on read-only source-store copies. Path
presence is not complete diff preservation, gold recall or problem-solving
quality; no measurement outputs or source histories are included here.

Use `config.coding-new.example.json` for a separate Coding instance with its
own data directory and API key file. It retains the five lexical fields above
and explicitly sets:

| Field | Coding value | Validation / default |
|---|---|---|
| `coding_compact_search` | `true` | strict boolean; defaults to `false` |
| `coding_compact_max_row_bytes` | `8192` | strict integer, at least 128; default 8192 |
| `coding_compact_max_response_bytes` | `65536` | strict integer, at least 128; default 65536 |

In this lexical configuration **Add and Search make zero model calls and
zero embedding calls**. Do not enable `--allow-model-calls`. Add, source-text
storage, normalization and user isolation are unchanged. Search accepts only
strict integer `top_k` values from 1 through 1000, rejecting booleans, floats,
strings, null, missing and out-of-range values. It does not supply defaults,
convert or clamp values. Required/unknown-field and options validation is
preserved. The input query is not truncated; queries above the existing
20,000-character limit are rejected.

Candidate retrieval, BM25, fusion, neighbor grouping and ranking are unchanged.
A deterministic projection then processes candidates in their original order.
It extracts file paths, complete unified diff hunks (checking old/new line
counts), explicit JSON editing parameters (including up to eight nested
levels), and Python string-literal editing actions. Actions are labeled
`execution unverified`: no edit is executed and no success is inferred. It
keeps the last four source lines matching fixed repair/reason keywords, each
limited to 512 UTF-8 bytes. When no change is recognized, it keeps a source
prefix of at most 2048 UTF-8 bytes. These line/prefix limits include the
newline and `[compact: truncated]` marker when truncation occurs. The rules
have no task-ID, dataset or gold-specific cases and do not read gold data.
The diff-chain idea was inspired by Hy-Memory and implemented independently;
no code was copied (see [PROVENANCE.md](PROVENANCE.md)).

The projection uses **first-claim-per-file**: only a `File: <path>` line fully
retained at its original line boundary in the returned content claims a file.
A cut path line cannot claim a shorter path merely because the truncation
marker supplies a newline. Later candidates omit already-claimed file/change
blocks; a candidate with paths but no new file is omitted. Distinct changes
within one candidate may remain, subject to the byte budgets.

The **8192-byte row budget** counts the complete memory JSON object, including
`id`, `score`, `content` and optional metadata. The **65536-byte response
budget** counts the entire successful Search JSON body, including its outer
structure. Both use the same serializer as HTTP transmission:
`json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')`.
JSON escaping, spaces, punctuation and truncation markers count; HTTP headers
do not. These are byte budgets, not character or token budgets.

If content exceeds a row budget or the remaining response budget, a fitting
Unicode content prefix is retained and a newline plus `[compact: truncated]`
is appended before complete JSON serialization. The result remains valid
UTF-8/JSON; `id`, `score` and optional metadata are unchanged. If those fixed
fields plus the marker cannot fit, processing stops and the current and
remaining candidates are omitted. No placeholder candidates or extra API
fields are added. The requested `top_k` is unchanged: the result may contain
fewer than `top_k` items, and never more.

This is a **lossy** Search projection. Later edits or versions of the same
file may be omitted; explanation lines, source prefixes and diff text may be
truncated; whole candidates may be omitted. The marker means byte-budget
truncation of that fragment, not edit execution, success, complete file
preservation or complete candidate coverage. Rule-based extraction, deduping
and omission do not guarantee a marker for every information loss. Stored
source text is unchanged, but complete file/change preservation is not
guaranteed. Local offline checks and measurements are not an Answer score or
Coding Agent acceptance test; its receiving limits, deployment capacity and
runtime latency have not been verified. No benchmark score is claimed.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
