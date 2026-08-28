from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .search import SearchHit


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    robot: str
    category: str
    question: str
    expected_paths: tuple[str, ...]


def dataset_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_dataset(path: Path) -> tuple[dict[str, object], list[EvaluationCase]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("지원하지 않는 평가셋 schema_version입니다")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("평가셋 cases가 비어 있습니다")

    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    for raw_case in raw_cases:
        case_id = str(raw_case["id"])
        if case_id in seen_ids:
            raise ValueError(f"중복 평가 case id: {case_id}")
        seen_ids.add(case_id)
        expected_paths = tuple(str(value) for value in raw_case["expected_paths"])
        if not expected_paths:
            raise ValueError(f"expected_paths가 비어 있습니다: {case_id}")
        cases.append(
            EvaluationCase(
                case_id=case_id,
                robot=str(raw_case["robot"]),
                category=str(raw_case["category"]),
                question=str(raw_case["question"]),
                expected_paths=expected_paths,
            )
        )
    return payload, cases


def evaluate_retrieval(
    dataset_path: Path,
    search: Callable[[str, int], Sequence[SearchHit]],
    *,
    k: int = 5,
    method: str,
) -> dict[str, object]:
    if k <= 0:
        raise ValueError("k는 1 이상이어야 합니다")
    payload, cases = load_dataset(dataset_path)

    reciprocal_ranks: list[float] = []
    hit_count = 0
    path_recalls: list[float] = []
    source_precisions: list[float] = []
    chunk_precisions: list[float] = []
    case_results: list[dict[str, object]] = []
    for case in cases:
        hits = list(search(case.question, k))
        expected = set(case.expected_paths)
        relevant_ranks = [rank for rank, hit in enumerate(hits, 1) if hit.path in expected]
        first_rank = min(relevant_ranks) if relevant_ranks else None
        if first_rank is not None:
            hit_count += 1
            reciprocal_ranks.append(1.0 / first_rank)
        else:
            reciprocal_ranks.append(0.0)
        retrieved_expected = {hit.path for hit in hits} & expected
        unique_retrieved_paths = {hit.path for hit in hits}
        path_recall = len(retrieved_expected) / len(expected)
        path_recalls.append(path_recall)
        chunk_precisions.append(sum(hit.path in expected for hit in hits) / k)
        source_precisions.append(
            len(retrieved_expected) / len(unique_retrieved_paths)
            if unique_retrieved_paths
            else 0.0
        )
        case_results.append(
            {
                "id": case.case_id,
                "robot": case.robot,
                "category": case.category,
                "relevant_rank": first_rank,
                "retrieved_expected_count": len(retrieved_expected),
                "path_recall": path_recall,
                "expected_paths": list(case.expected_paths),
                "top_paths": [hit.path for hit in hits],
                "top_hits": [
                    {"rank": rank, **hit.to_dict(include_text=False)}
                    for rank, hit in enumerate(hits, 1)
                ],
            }
        )

    case_count = len(cases)
    return {
        "schema_version": 1,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": payload.get("dataset_id"),
        "dataset_split": payload.get("split"),
        "dataset_sha256": dataset_sha256(dataset_path),
        "method": method,
        "k": k,
        "case_count": case_count,
        "metrics": {
            f"hit_rate_at_{k}": hit_count / case_count,
            f"recall_at_{k}": sum(path_recalls) / case_count,
            "mrr": sum(reciprocal_ranks) / case_count,
            f"source_precision_at_{k}": sum(source_precisions) / case_count,
            f"chunk_precision_at_{k}": sum(chunk_precisions) / case_count,
        },
        "unmeasured": [
            "answer_citation_accuracy",
            "diagnosis_accuracy",
            "generation_latency",
        ],
        "cases": case_results,
    }


def write_evaluation_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
