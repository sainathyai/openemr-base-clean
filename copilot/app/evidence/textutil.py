"""Shared tokenization for lexical retrieval (Week 2).

Stopword removal helps BM25 and the fallback lexical reranker focus on content
words (drug names, analytes, conditions) rather than filler, which is exactly
where clinical queries carry their signal. The dense embedder does its own
tokenization and does not use this.
"""
from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Compact English stopword list. Clinically loaded negations ("no", "not") are
# deliberately kept.
_STOPWORDS = frozenset(
    """a an and are as at be been by can could did do does for from had has have
    in into is it its may might of on or should so than that the their them then
    there these they this to was were what when where which who will with would
    you your patient please list all""".split()
)


def content_tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]
