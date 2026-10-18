"""Small, dependency-free 64-bit SimHash for near-duplicate clustering."""

from __future__ import annotations

import hashlib
import re


SPACE_RE = re.compile(r"\s+")


def _tokens(text: str) -> list[str]:
    compact = SPACE_RE.sub("", text or "")
    if not compact:
        return []
    if len(compact) <= 3:
        return [compact]
    return [compact[i : i + 3] for i in range(len(compact) - 2)]


def simhash64(text: str) -> int:
    vector = [0] * 64
    for token in _tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(64):
            vector[bit] += 1 if (value >> bit) & 1 else -1
    result = 0
    for bit, weight in enumerate(vector):
        if weight >= 0:
            result |= 1 << bit
    return result


def bands(value: int) -> tuple[int, int, int, int]:
    return (
        value & 0xFFFF,
        (value >> 16) & 0xFFFF,
        (value >> 32) & 0xFFFF,
        (value >> 48) & 0xFFFF,
    )


def hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()
