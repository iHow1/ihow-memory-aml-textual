# Technical report: iHow Memory AML Textual (lexical, zero-model)

## 1. Summary

The entry is a memory service that stores conversation messages verbatim and
retrieves them with user-local BM25. Neither Add nor Search calls a language
model or an embedding model. Search returns the original messages, each with
its neighbouring messages in the same session, so the downstream Answer model
sees source text rather than summaries.

| Item | Value |
|---|---|
| Health mode string | `source-memory-lexical-v1` |
| Model calls in Add / Search | 0 / 0 |
| Storage | one SQLite database per `user_id` |
| Ranking | BM25 (k1 = 1.5, b = 0.75), statistics computed per user |
| Context | ±1 neighbouring message in the same session |
| Local evidence cap | none; results stop at `top_k` |

## 2. Design rationale

- **Keep evidence verbatim.** Memory questions often hinge on exact numbers,
  names, dates and who said what. Summarising or extracting at Add time can
  drop or distort these; returning the original messages avoids that, and the
  role and timestamp travel with every message.
- **Deterministic and cheap.** No model calls means no per-request model cost,
  no rate limits from a model provider and identical results on replay.
- **Isolation by construction.** Each user has a separate database file, and
  BM25 document frequencies are computed over that user's messages only, so
  one benchmark user's data cannot influence another user's ranking.

## 3. Add

1. **Input normalisation.** `content` may be a string or an array of text
   parts; parts are joined with a single newline. Any non-text part rejects the
   request before anything is stored. Only `user` and `assistant` roles are
   accepted.
2. **Batching.** An Add of up to 10,000 messages (2 MiB) is split into
   batches of at most 20 messages and 2,000 words. A single message longer
   than 2,000 words is split into segments at word boundaries, keeping its
   whitespace, role and timestamp.
3. **Atomic publish.** For a multi-batch Add, all batches are written to a
   private staging copy of the user's database, which is then published with
   SQLite's backup API in one transaction. A failure leaves the published
   database unchanged; repeating the same request resumes from the committed
   batches.
4. **Storage.** Each message becomes one `raw` unit holding its full text.
   No annotation, vector or derived text is created in this mode.
5. **Idempotency.** `(user_id, request_id)` identifies a request by the hash of
   its full body. A repeat with the same body returns the original result; a
   different body returns 409.

## 4. Search

1. **Tokenisation.** Query and messages are lower-cased (`str.casefold`) and
   split with the regular expression `\w+`. The first 32 distinct query terms
   are used.
2. **BM25.** For every raw unit of the user,
   `score = Σ idf(t) · tf · (k1 + 1) / (tf + k1 · (1 − b + b · len / avglen))`
   with k1 = 1.5, b = 0.75 and `idf(t) = ln(1 + (N − n_t + 0.5) / (n_t + 0.5))`,
   where N and n_t count the user's units only.
3. **Rank score.** The 100 highest BM25 units receive `0.5 / (60 + rank)`
   (rank starting at 1). This rank-based form is kept from the hybrid design
   (section 6) so that the two modes differ only in the removed components.
   The returned `score` field is this value.
4. **Grouping.** Each hit is expanded with the previous and next message of the
   same session. Groups are emitted in score order (ties by storage order). A
   message that already appeared inside an earlier group does not start a new
   group; a neighbouring message can still appear in more than one group.
5. **Rendering.** Each group is the messages in storage order, joined by a
   blank line, each as

   ```
   [<message id> | <role> | said_at=<ISO-8601 UTC or "unknown">]
   <original text>
   ```

   `created_at` is the timestamp of the hit message when one was supplied.
6. **Stopping.** Emission stops after `top_k` groups. There is no local token
   budget; if the returned text exceeds the Answer model's window, the platform
   truncates it in the returned order. When the 100 best BM25 hits yield fewer
   than `top_k` groups, the remaining slots are filled with groups started from
   the user's other messages (including BM25 matches ranked below 100) in
   storage order, with score 0.

## 5. Service behaviour

- Python standard-library HTTP server; authentication by a single API key
  (Bearer, Token or `X-Api-Key`), compared in constant time.
- One processing slot with a first-in, first-out queue. A request's body is
  read and parsed as JSON before it joins the queue, so a slow upload or a
  syntax error never holds the slot; request fields are validated once the
  request reaches the slot. A request still waiting after 900 seconds receives
  `429 capacity_busy` with `Retry-After: 60`.
- Up to 64 connections are held (the listen backlog is also 64); further
  connections receive a bare 429 with `Retry-After: 60`. The minimum declared
  concurrency for the benchmark is 16.
- On shutdown, waiting requests receive 429 with `Retry-After: 60` without
  touching storage; only the running request finishes.
- 30-second socket timeout for reading a request.
- No request, payload or access logging in the service.
- Data lives only under the configured `data_dir`. Deleting that directory
  removes all stored evaluation data; operators should do so after the
  evaluation as required by the benchmark's data rules.

## 6. Method history

The lexical mode is the last of several configurations developed for this
benchmark. Earlier configurations, kept in the code but not used by this
entry:

| Date | Change |
|---|---|
| September 2026 | Hybrid candidate: an LLM marks verbatim spans of each message as index annotations at Add time; spans and raw messages are embedded with a local BGE model; Search fuses dense and BM25 ranks with reciprocal rank fusion and follows annotation links. A local 6,000-token evidence cap matched our local reader. |
| 2026-09-30 | Lexical mode added: annotations, embeddings and query planning removed; only raw-message BM25, neighbour expansion and rendering remain. Chosen after the organiser confirmed a zero-model entry is acceptable. |
| 2026-10-01 | Local 6,000-token evidence cap removed; results now stop only at `top_k`. The cap is part of the database identity, so databases created with it are refused. |
| 2026-10-02 | Add/Search requests queue first-in, first-out (900-second wait, 64 connections, `Retry-After: 60` on 429) instead of receiving 429 immediately when busy. Retrieval unchanged. |

The model-assisted code paths remain in `deployment.py` and `model.py` but are
not reachable with the lexical configuration and make no calls.

## 7. Performance (local measurement)

Synthetic data, 2-vCPU host, in-process calls without HTTP. Production hosts
may be slower.

| Messages for one user | Add, median (20 messages) | Search, median (`top_k` = 100) |
|---|---|---|
| 1,000 | 14 ms | 54 ms |
| 5,000 | 17 ms | 277 ms |
| 20,000 | 24 ms | 1,266 ms |

Search scores every message of the user on each request, so its latency grows
roughly linearly with the user's history.

## 8. Limitations

- **No semantic matching.** A message that shares no word with the question
  (a paraphrase or synonym) gets no relevance score. It is returned only as a
  neighbour of a hit or in the score-0 fill (section 4, step 6), so its
  position does not reflect its relevance. When the scored hits already fill
  `top_k` there is no fill, and such a message can appear only as a
  neighbour of a hit.
- **Scripts without spaces.** `\w+` treats a run of Chinese or Japanese
  characters as one token, so lexical matching in those languages is weak.
- **No temporal or update reasoning** beyond returning timestamps; resolving
  "latest" facts is left to the Answer model.
- **Linear scan.** There is no inverted index; very long histories make Search
  slower (section 7).
- **Single processing slot.** Requests are queued and served one at a time,
  so under concurrent load each request waits for the ones ahead of it.
  Throughput does not grow with the number of client workers.

## 9. Results

Official evaluation results will be added here once available. No benchmark
score is claimed in this report.
