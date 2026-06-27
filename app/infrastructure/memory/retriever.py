from __future__ import annotations

import re
from collections import Counter

_ENGLISH_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "of", "to", "in", "on", "and", "or", "for",
})

_ENGLISH_WORD_RE = re.compile(r"[A-Za-z0-9]+")


class MemoryRetriever:
    """Lightweight retrieval for Markdown-file memory providers.

    Tokenizes text into Chinese character bigrams + English lowercase words,
    scores entries by Jaccard similarity x term-frequency boost, returns top-K.

    Only serves Markdown-file providers (builtin / multi-project). Does not
    leak into the ExternalMemoryProvider SPI and does not constrain external
    query providers (mem0 / holographic / honcho).
    """

    def __init__(self, max_results: int = 3, min_score: float = 0.3):
        self.max_results = max_results
        self.min_score = min_score

    def retrieve(self, query: str, entries: list[str]) -> list[tuple[str, float]]:
        """Return (entry, score) pairs sorted by score desc.

        Filters out entries with score < min_score. At most max_results.
        Empty query or empty entries -> [].
        """
        if not query or not entries:
            return []
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []
        scored: list[tuple[str, float]] = []
        for entry in entries:
            if not entry or not entry.strip():
                continue
            entry_tokens = self._tokenize(entry)
            if not entry_tokens:
                continue
            score = self._score(query_tokens, entry_tokens, entry)
            if score >= self.min_score:
                scored.append((entry, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[: self.max_results]

    @staticmethod
    def jaccard(a: str, b: str) -> float:
        """Token-level Jaccard similarity between two strings."""
        retriever = MemoryRetriever()
        ta = retriever._tokenize(a)
        tb = retriever._tokenize(b)
        if not ta or not tb:
            return 0.0
        union = ta | tb
        if not union:
            return 0.0
        return len(ta & tb) / len(union)

    def _score(
        self,
        query_tokens: set[str],
        entry_tokens: set[str],
        entry_text: str,
    ) -> float:
        """Jaccard similarity x term-frequency boost.

        tf_boost rewards entries where query terms appear repeatedly in the
        original entry text (not the deduplicated token set).
        """
        intersection = query_tokens & entry_tokens
        union = query_tokens | entry_tokens
        if not union:
            return 0.0
        jaccard = len(intersection) / len(union)
        if jaccard == 0.0:
            return 0.0
        entry_tf = self._term_frequency(entry_text)
        tf_hits = sum(entry_tf.get(w, 0) for w in query_tokens)
        tf_boost = 1.0 + (tf_hits / len(entry_tokens)) if entry_tokens else 1.0
        return jaccard * tf_boost

    def _tokenize(self, text: str) -> set[str]:
        """Tokenize into Chinese char bigrams + English lowercase words.

        English stopwords are filtered. Chinese bigrams are kept as-is.
        """
        if not text:
            return set()
        tokens: set[str] = set()
        english_spans = [m.span() for m in _ENGLISH_WORD_RE.finditer(text)]
        cursor = 0
        cjk_chunks: list[str] = []
        for start, end in english_spans:
            if start > cursor:
                cjk_chunks.append(text[cursor:start])
            word = text[start:end].lower()
            if word not in _ENGLISH_STOPWORDS:
                tokens.add(word)
            cursor = end
        if cursor < len(text):
            cjk_chunks.append(text[cursor:])
        for chunk in cjk_chunks:
            chars = [c for c in chunk if not c.isspace() and not c.isascii()]
            for i in range(len(chars) - 1):
                tokens.add(chars[i] + chars[i + 1])
        return tokens

    def _term_frequency(self, text: str) -> dict[str, int]:
        """Return term-frequency dict computed from the original text.

        Counts both English word occurrences and Chinese bigram occurrences,
        so repeated query terms in the entry genuinely boost the score.
        """
        if not text:
            return {}
        counts: Counter[str] = Counter()
        cursor = 0
        for m in _ENGLISH_WORD_RE.finditer(text):
            if m.start() > cursor:
                self._count_cjk_bigrams(text[cursor:m.start()], counts)
            word = m.group(0).lower()
            if word not in _ENGLISH_STOPWORDS:
                counts[word] += 1
            cursor = m.end()
        if cursor < len(text):
            self._count_cjk_bigrams(text[cursor:], counts)
        return dict(counts)

    @staticmethod
    def _count_cjk_bigrams(chunk: str, counts: Counter[str]) -> None:
        chars = [c for c in chunk if not c.isspace() and not c.isascii()]
        for i in range(len(chars) - 1):
            counts[chars[i] + chars[i + 1]] += 1
