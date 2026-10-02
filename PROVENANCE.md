# Provenance and attribution

## Authorship

The code was written for this entry in the iHow Memory project's private
development repository. It was produced by iHow with AI coding agents (Claude
and Codex) working under iHow's direction and review. iHow is the copyright
holder and releases it under the Apache License 2.0.

`SOURCE_MANIFEST.json` records the private commit the files were copied from
and the SHA-256 of each file, so the public files can be matched to the
deployed service.

## References and reuse

| Item | How it is used | Code copied? |
|---|---|---|
| ActiveMemoryIndex (AMI), https://github.com/linxuhao/ActiveMemoryIndex, viewed at commit `2e6d76cae4454c2f9c722d0c927aaf0825f1cd69` | Conceptual reference only: the idea of keeping raw messages alongside derived "fact" units and retrieving over both (noted in the `store.py` docstring). The deployed lexical mode keeps only the raw messages. | No. Independent implementation. |
| ReFind, https://github.com/imlrz/ReFind | Design reference cited by our earlier adapter for the same benchmark: retrieving original text with lexical scoring and organising evidence around source messages. | No. No code imported or adapted. |
| Okapi BM25 (Robertson and Zaragoza, "The Probabilistic Relevance Framework: BM25 and Beyond", 2009) | Standard ranking formula with k1 = 1.5, b = 0.75 and the `log(1 + (N − n + 0.5)/(n + 0.5))` IDF form. | No. Written from the published formula. |
| Reciprocal Rank Fusion (Cormack, Clarke and Büttcher, SIGIR 2009) | Rank-to-score conversion of the form `w/(60 + rank)`, kept from the hybrid design. | No. Written from the published formula. |
| `tiktoken` 0.12.0 (MIT) | Runtime dependency; installed from PyPI, not vendored. | No. See THIRD_PARTY_NOTICES.md. |
| Python standard library (`sqlite3`, `http.server`, …) | Storage and HTTP serving. | n/a |

No other repository, dataset or model was copied into this code. Listing a
reference does not imply that its authors endorse this entry.

## Earlier iHow work

An earlier private Node.js adapter built by the same team for this benchmark
(on top of iHow Memory Core) explored raw-text retrieval and message windows.
Those ideas carried over; no code from it is in this repository, which is a
separate Python implementation.

## Benchmark data

No benchmark data, gold answers, model outputs or evaluation logs are in this
repository. The tests use short synthetic sentences written for the tests.

## Relation to other iHow Memory repositories

This service does not depend on, and does not include, the iHow Memory Core
package (`ihow-memory` on npm, https://github.com/iHow1/ihow-memory-core).
It is a separate, purpose-built implementation for this benchmark.
