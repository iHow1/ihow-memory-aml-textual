# Textual track deployed version and Search changes

## Version and deployment

The registered version name remains `lexical-107ac55`. The public `main`
branch retains the registration-time source. This separate branch,
`text-tolerant-d57b845`, publishes the runtime from private source commit
`d57b845ef9a34e9e983e4b98ee50f064e6849c10`.

Since `2026-10-23T15:06Z`, the online Textual service has run the `107ac55`
baseline plus the tolerant Search changes in `d57b845`, with
`coding_search_tolerant: true`. The service URL, key and previously stored
data were unchanged.

During the official Full run, the service returned HTTP 422 for 52 of 5,892
Search requests. The platform did not retry HTTP 422, and the task failed at
the retrieval stage. This prompted the tolerant Search change.

## Changes from 107ac55 to d57b845 and their effects

These are behavior changes relative to the registered baseline, based on the
diff of `content.py`, `deployment.py` and the added `validation.py`.
Tolerance applies only when `coding_search_tolerant` is enabled; its default
is `false`.

| Input or diagnostic | Registered baseline | Deployed tolerant behavior and effect |
|---|---|---|
| Long query | A query longer than 20,000 Unicode code points is rejected with 422. | When the original string exceeds 20,000 code points, strip outer whitespace; if it still exceeds the limit, concatenate its first and last 10,000 code points without a marker. This discards the middle and can change the query terms used for retrieval. Short strings are unchanged. |
| Query text parts | Require a non-empty array containing only valid text parts, with no extra fields. | Join usable text parts with a newline, skipping non-text, malformed, empty and whitespace-only parts. Extra part fields are ignored. The resulting query can differ from the original input rather than causing 422. |
| Query with no usable text | Missing, non-text or empty/whitespace-only text is rejected with 422. | Return HTTP 200 with `{"data": []}` after validating the payload object and `user_id`, without opening or creating an owner database. Retrieval therefore supplies no evidence for this request instead of failing it with 422. |
| `top_k` | Require an integer from 1 to 100. | Clamp integers to 1–100; missing or non-integer values, including booleans, default to 100. This can change the number of returned groups. |
| `options` | Reject a non-list, a list with any non-string element, or a JSON-serialized list longer than 20,000 characters. | Replace such values with an empty list and continue Search. Invalid options no longer reject the request. |
| Unknown fields | Reject extra Search fields or extra text-part fields. | Ignore those fields and continue Search, using only the accepted fields and usable text parts. |
| Validation and tolerance logs | No structured validation metadata for these cases. | Emit warnings with fixed field paths, error types, value types and lengths; unknown fields expose only their count. Rejection logging also covers malformed JSON, invalid HTTP framing and validation/backend errors, without retaining request values, client field names or exception messages. This adds diagnostics rather than retrieved text. |

These changes do change retrieval behavior. Subsequent Textual retrieval
was completed on the changed code, rather than on the unmodified registered
baseline. A missing or invalid `user_id` still rejects the request; tolerance
does not relax Add validation, authentication, HTTP body limits or user
isolation. Neither Add nor Search makes model calls in lexical mode.

The diff implements general input rules. It contains no branches based on
question identifiers, dataset identity or gold answers. It does not change
BM25 ranking, neighbour expansion or stored message content, but its query
normalization, result limit coercion and empty-result behavior affect the
retrieval requests and responses.

This change was disclosed to the organizers by email in October 2026.

## Source correspondence

`SOURCE_MANIFEST.json` records the private source commit and the unchanged
public baseline commit. All ten public runtime files match both the private
commit and the archived Textual deployment source byte for byte. The Search
regression test uses the public baseline revision in place of the equivalent
private revision; the manifest records that adjustment and hashes of the
original and adjusted files. Internal development documentation is represented
by the public README section rather than copied into the runtime tree.
