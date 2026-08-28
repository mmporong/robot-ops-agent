from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .embedding import Embedder
from .indexer import _connect_readonly, blob_to_vector


WORD_RE = re.compile(r"[0-9A-Za-z가-힣_./:-]+")
MAX_QUERY_CHARS = 2_000


@dataclass(frozen=True, slots=True)
class SearchHit:
    path: str
    chunk_index: int
    score: float
    text: str
    method: str

    def to_dict(self, *, include_text: bool = True) -> dict[str, object]:
        data = asdict(self)
        if not include_text:
            data.pop("text")
        return data


@dataclass(frozen=True, slots=True)
class _ChunkRow:
    path: str
    chunk_index: int
    text: str
    embedding: bytes
    embedding_dim: int
    pipeline_id: str


def tokenize(text: str) -> list[str]:
    """Tokenize Korean/English text and add character trigrams for inflections."""

    tokens: list[str] = []
    for word in WORD_RE.findall(text.lower()):
        tokens.append(word)
        compact = word.replace("_", "")
        if len(compact) >= 3:
            tokens.extend(f"#{compact[index:index + 3]}" for index in range(len(compact) - 2))
    return tokens


def _load_chunks(db_path: Path) -> list[_ChunkRow]:
    con = _connect_readonly(db_path)
    try:
        rows = con.execute(
            """
            SELECT
                documents.path,
                chunks.idx,
                chunks.text,
                chunks.embedding,
                chunks.embedding_dim,
                documents.pipeline_id
            FROM chunks
            JOIN documents ON documents.id = chunks.document_id
            ORDER BY documents.path, chunks.idx
            """
        ).fetchall()
        return [_ChunkRow(*row) for row in rows]
    finally:
        con.close()


def keyword_search(db_path: Path, query: str, *, k: int = 5) -> list[SearchHit]:
    if k <= 0:
        raise ValueError("k는 1 이상이어야 합니다")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query는 {MAX_QUERY_CHARS}자 이하여야 합니다")
    query_terms = tuple(dict.fromkeys(tokenize(query)))
    if not query_terms:
        return []

    rows = _load_chunks(db_path)
    if not rows:
        return []

    term_frequencies: list[Counter[str]] = []
    document_lengths: list[int] = []
    document_frequency = Counter({term: 0 for term in query_terms})
    for row in rows:
        counts = Counter(tokenize(row.text))
        term_frequencies.append(counts)
        document_lengths.append(sum(counts.values()))
        for term in query_terms:
            if counts[term]:
                document_frequency[term] += 1

    average_length = sum(document_lengths) / max(len(document_lengths), 1)
    corpus_size = len(rows)
    k1 = 1.5
    b = 0.75
    scored: list[tuple[float, _ChunkRow]] = []
    for row, counts, document_length in zip(rows, term_frequencies, document_lengths, strict=True):
        score = 0.0
        length_norm = 1.0 - b + b * (document_length / max(average_length, 1.0))
        for term in query_terms:
            frequency = counts[term]
            if not frequency:
                continue
            df = document_frequency[term]
            inverse_document_frequency = math.log(
                1.0 + (corpus_size - df + 0.5) / (df + 0.5)
            )
            score += inverse_document_frequency * (
                frequency * (k1 + 1.0) / (frequency + k1 * length_norm)
            )
        if score > 0.0:
            scored.append((score, row))

    scored.sort(key=lambda item: (-item[0], item[1].path, item[1].chunk_index))
    return [
        SearchHit(
            path=row.path,
            chunk_index=row.chunk_index,
            score=score,
            text=row.text,
            method="keyword-bm25-char3-v1",
        )
        for score, row in scored[:k]
    ]


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return float("-inf")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def vector_search(
    db_path: Path,
    query: str,
    embedder: Embedder,
    *,
    k: int = 5,
) -> list[SearchHit]:
    if k <= 0:
        raise ValueError("k는 1 이상이어야 합니다")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query는 {MAX_QUERY_CHARS}자 이하여야 합니다")
    rows = _load_chunks(db_path)
    if not rows:
        return []

    incompatible = sorted(
        {
            row.pipeline_id.split("|", 1)[0]
            for row in rows
            if row.pipeline_id.split("|", 1)[0] != embedder.identifier
        }
    )
    if incompatible:
        raise ValueError(
            "질의 임베더와 인덱스 임베더가 다릅니다: "
            f"query={embedder.identifier}, index={','.join(incompatible)}"
        )

    query_vector = list(embedder.embed(query))
    scored = [
        (_cosine_similarity(query_vector, blob_to_vector(row.embedding)), row)
        for row in rows
        if row.embedding_dim == len(query_vector)
    ]
    scored.sort(key=lambda item: (-item[0], item[1].path, item[1].chunk_index))
    return [
        SearchHit(
            path=row.path,
            chunk_index=row.chunk_index,
            score=score,
            text=row.text,
            method=f"vector:{embedder.identifier}",
        )
        for score, row in scored[:k]
    ]


def reciprocal_rank_fusion(
    result_sets: Iterable[Sequence[SearchHit]],
    *,
    k: int = 5,
    rank_constant: int = 60,
) -> list[SearchHit]:
    fused_scores: dict[tuple[str, int], float] = {}
    representatives: dict[tuple[str, int], SearchHit] = {}
    for results in result_sets:
        for rank, hit in enumerate(results, 1):
            key = (hit.path, hit.chunk_index)
            fused_scores[key] = fused_scores.get(key, 0.0) + 1.0 / (rank_constant + rank)
            representatives[key] = hit

    ordered = sorted(fused_scores, key=lambda key: (-fused_scores[key], key[0], key[1]))
    return [
        SearchHit(
            path=representatives[key].path,
            chunk_index=representatives[key].chunk_index,
            score=fused_scores[key],
            text=representatives[key].text,
            method="hybrid-rrf-v1",
        )
        for key in ordered[:k]
    ]
