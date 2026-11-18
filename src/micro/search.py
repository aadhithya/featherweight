"""Keyword search over a few short texts: BM25, in memory, with no dependency. Tool discovery uses it, and a lookup of
skills or agents could use it too: it knows nothing about what the texts are."""

import math
import re
from collections import Counter
from collections.abc import Sequence

_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokens(text: str) -> list[str]:
    """Lowercase words and numbers. Underscores, punctuation and a camelCase boundary split words. No stemming."""
    return [word.lower() for word in _WORD.findall(_CAMEL.sub(" ", text))]


class Bm25:
    """BM25 over a fixed list of texts. `search` returns the indices of the texts that match, best first."""

    def __init__(self, documents: Sequence[str], *, k1: float = 1.5, b: float = 0.75):
        self._k1, self._b = k1, b
        self._counts = [Counter(tokens(document)) for document in documents]
        self._lengths = [sum(counts.values()) for counts in self._counts]
        total = len(documents)
        self._average = (sum(self._lengths) / total) if total else 0.0
        frequency: Counter[str] = Counter()
        for counts in self._counts:
            frequency.update(counts.keys())
        # `1 + ...` keeps the idf above zero also for a word that is in most of the texts.
        self._idf = {word: math.log(1 + (total - n + 0.5) / (n + 0.5)) for word, n in frequency.items()}

    def search(self, query: str, k: int) -> list[tuple[int, float]]:
        """Up to `k` pairs of (index, score), highest score first, equal scores in the order of the texts. A text that
        has none of the words of the query is not a match."""
        words = set(tokens(query))
        scored: list[tuple[int, float]] = []
        for index, counts in enumerate(self._counts):
            score = 0.0
            for word in words:
                count = counts.get(word, 0)
                if count:
                    norm = 1 - self._b + self._b * self._lengths[index] / (self._average or 1.0)
                    score += self._idf[word] * count * (self._k1 + 1) / (count + self._k1 * norm)
            if score > 0:
                scored.append((index, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:k]
