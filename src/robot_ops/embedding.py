from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, Sequence


TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_./:-]+")


class Embedder(Protocol):
    @property
    def identifier(self) -> str: ...

    def embed(self, text: str) -> Sequence[float]: ...


class HashEmbedder:
    """Deterministic dependency-free embedder for tests and offline smoke checks.

    It is intentionally not a semantic model and must not be used as retrieval
    quality evidence.
    """

    def __init__(self, dimension: int = 128) -> None:
        if dimension <= 0:
            raise ValueError("dimension은 1 이상이어야 합니다")
        self.dimension = dimension

    @property
    def identifier(self) -> str:
        return f"hash-v1:{self.dimension}"

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            return [value / norm for value in vector]
        return vector
