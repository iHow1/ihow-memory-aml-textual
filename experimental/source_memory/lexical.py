"""Model-free annotator: Add stores raw messages only, Search uses the query as given.

With Memory(embedder=None) the ranking is exactly the owner-local BM25 part of
the hybrid candidate, so the two modes differ only by the removed components.
"""


class NoAnnotator:
    identity = 'none/deterministic-raw-bm25-v1'

    def extract(self, messages, prior):
        return {'atoms': []}

    def plan(self, query, options):
        return []
