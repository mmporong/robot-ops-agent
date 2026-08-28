from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence, TypedDict

from .evaluation import dataset_sha256, load_dataset
from .search import SearchHit, keyword_search
from .secrets import redact_text


STRICT_CITATION_RE = re.compile(r"\[출처:\s*([^\]]+?)\s*\]")
LOOSE_CITATION_RE = re.compile(r"[\[(]출처:\s*([^\]\)]+?)[\])]")


@dataclass(frozen=True, slots=True)
class StreamingResponse:
    answer: str
    wall_latency_ms: float
    ttft_ms: float | None
    usage: dict[str, object]
    timings: dict[str, object]


class AnswerScore(TypedDict):
    required_concept_count: int
    matched_concept_count: int
    concept_recall: float | None
    rubric_pass: bool
    citations: list[str]
    citation_precision: float
    citation_format_compliance: float
    expected_source_citation_recall: float


class BenchmarkCaseResult(TypedDict):
    round: int
    id: str
    robot: str
    question: str
    retrieved_paths: list[str]
    answer: str
    wall_latency_ms: float
    ttft_ms: float | None
    usage: dict[str, object]
    timings: dict[str, object]
    quality: AnswerScore
    max_rss_kib: int | None


class RssMonitor:
    def __init__(self, pid: int | None, interval_seconds: float = 0.02) -> None:
        self.pid = pid
        self.interval_seconds = interval_seconds
        self.max_rss_kib: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "RssMonitor":
        if self.pid is not None:
            self._sample()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._sample()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def _sample(self) -> None:
        if self.pid is None:
            return
        try:
            status = Path(f"/proc/{self.pid}/status").read_text(encoding="utf-8")
        except OSError:
            return
        match = re.search(r"(?m)^VmRSS:\s+(\d+)\s+kB$", status)
        if match:
            value = int(match.group(1))
            self.max_rss_kib = max(self.max_rss_kib or 0, value)


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_chat(
    endpoint: str,
    model: str,
    messages: Sequence[Mapping[str, str]],
    *,
    timeout_seconds: float,
    max_tokens: int,
) -> StreamingResponse:
    body = json.dumps(
        {
            "model": model,
            "messages": list(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    first_token_at: float | None = None
    parts: list[str] = []
    usage: dict[str, object] = {}
    timings: dict[str, object] = {}
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            event = json.loads(payload)
            if isinstance(event.get("usage"), dict):
                usage = dict(event["usage"])
            if isinstance(event.get("timings"), dict):
                timings = dict(event["timings"])
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            delta = choices[0].get("delta", {})
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str) and content:
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                parts.append(content)
    finished = time.perf_counter()
    return StreamingResponse(
        answer=redact_text("".join(parts).strip()),
        wall_latency_ms=round((finished - started) * 1000.0, 3),
        ttft_ms=(
            round((first_token_at - started) * 1000.0, 3)
            if first_token_at is not None
            else None
        ),
        usage=usage,
        timings=timings,
    )


def _build_messages(question: str, hits: Sequence[SearchHit]) -> list[dict[str, str]]:
    evidence = "\n\n".join(
        f"[근거 {index}] 경로: {hit.path}\n{hit.text}"
        for index, hit in enumerate(hits, 1)
    )
    return [
        {
            "role": "system",
            "content": (
                "당신은 로봇 운용 기록을 읽는 조회 전용 진단 보조자다. "
                "제공된 근거에 있는 사실만 사용하고, 로봇 구동 명령은 제안하지 않는다. "
                "근거가 부족하면 부족하다고 답한다. 답은 한국어 세 문장 이내로 쓰고, "
                "사용한 근거마다 정확한 경로를 [출처: 경로] 형식으로 붙인다."
            ),
        },
        {
            "role": "user",
            "content": f"질문: {question}\n\n{evidence}",
        },
    ]


def score_answer(
    answer: str,
    rubric: Mapping[str, object],
    *,
    retrieved_paths: Sequence[str],
    expected_paths: Sequence[str],
) -> AnswerScore:
    raw_groups = rubric.get("required_concepts")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("answer_rubric.required_concepts는 비어 있지 않은 목록이어야 합니다")
    groups: list[list[str]] = []
    for group in raw_groups:
        if (
            not isinstance(group, list)
            or not group
            or not all(isinstance(alternative, str) and alternative for alternative in group)
        ):
            raise ValueError(
                "answer_rubric.required_concepts의 각 항목은 비어 있지 않은 문자열 목록이어야 합니다"
            )
        groups.append(group)
    matched = [any(alternative in answer for alternative in group) for group in groups]
    concept_recall = sum(matched) / len(groups)
    raw_citations = [value.strip() for value in LOOSE_CITATION_RE.findall(answer)]
    citations = [
        re.sub(r"^경로\s*:\s*", "", value).strip() for value in raw_citations
    ]
    strict_citations = [value.strip() for value in STRICT_CITATION_RE.findall(answer)]
    retrieved = set(retrieved_paths)
    expected = set(expected_paths)
    valid_citations = [citation for citation in citations if citation in retrieved]
    expected_citations = set(citations) & expected
    return {
        "required_concept_count": len(groups),
        "matched_concept_count": sum(matched),
        "concept_recall": concept_recall,
        "rubric_pass": all(matched),
        "citations": citations,
        "citation_precision": (
            len(valid_citations) / len(citations) if citations else 0.0
        ),
        "citation_format_compliance": (
            sum(not value.startswith("경로:") for value in strict_citations)
            / len(raw_citations)
            if raw_citations
            else 0.0
        ),
        "expected_source_citation_recall": len(expected_citations) / len(expected),
    }


def run_benchmark(
    *,
    endpoint: str,
    model: str,
    db_path: Path,
    dataset_path: Path,
    rounds: int,
    k: int,
    timeout_seconds: float,
    max_tokens: int,
    server_pid: int | None,
    model_file: Path | None,
    runtime_version: str | None,
) -> dict[str, object]:
    if rounds <= 0:
        raise ValueError("rounds는 1 이상이어야 합니다")
    payload, cases = load_dataset(dataset_path)
    raw_case_values = payload.get("cases")
    if not isinstance(raw_case_values, list):
        raise ValueError("평가셋 cases 형식이 올바르지 않습니다")
    raw_cases: dict[str, Mapping[str, object]] = {}
    for raw_case in raw_case_values:
        if not isinstance(raw_case, Mapping) or "id" not in raw_case:
            raise ValueError("평가셋 case 형식이 올바르지 않습니다")
        raw_cases[str(raw_case["id"])] = raw_case

    results: list[BenchmarkCaseResult] = []
    max_rss_kib: int | None = None
    for case in cases:
        hits = keyword_search(db_path, case.question, k=k)
        for round_index in range(1, rounds + 1):
            with RssMonitor(server_pid) as memory:
                response = _stream_chat(
                    endpoint,
                    model,
                    _build_messages(case.question, hits),
                    timeout_seconds=timeout_seconds,
                    max_tokens=max_tokens,
                )
            if memory.max_rss_kib is not None:
                max_rss_kib = max(max_rss_kib or 0, memory.max_rss_kib)
            rubric = raw_cases[case.case_id].get("answer_rubric", {})
            score = score_answer(
                response.answer,
                rubric if isinstance(rubric, Mapping) else {},
                retrieved_paths=[hit.path for hit in hits],
                expected_paths=case.expected_paths,
            )
            results.append(
                {
                    "round": round_index,
                    "id": case.case_id,
                    "robot": case.robot,
                    "question": case.question,
                    "retrieved_paths": [hit.path for hit in hits],
                    "answer": response.answer,
                    "wall_latency_ms": response.wall_latency_ms,
                    "ttft_ms": response.ttft_ms,
                    "usage": response.usage,
                    "timings": response.timings,
                    "quality": score,
                    "max_rss_kib": memory.max_rss_kib,
                }
            )

    latencies = [result["wall_latency_ms"] for result in results]
    ttfts = [
        value
        for result in results
        if (value := result["ttft_ms"]) is not None
    ]
    cold_results = [result for result in results if result["round"] == 1]
    repeated_results = [result for result in results if result["round"] > 1]
    cold_latencies = [result["wall_latency_ms"] for result in cold_results]
    cold_ttfts = [
        value
        for result in cold_results
        if (value := result["ttft_ms"]) is not None
    ]
    repeated_latencies = [
        result["wall_latency_ms"] for result in repeated_results
    ]
    repeated_ttfts = [
        value
        for result in repeated_results
        if (value := result["ttft_ms"]) is not None
    ]
    generation_rates = [
        float(value)
        for result in results
        if isinstance(
            (value := result["timings"].get("predicted_per_second")),
            (int, float),
        )
    ]
    prompt_rates = [
        float(value)
        for result in results
        if isinstance(
            (value := result["timings"].get("prompt_per_second")),
            (int, float),
        )
    ]
    first_run_prompt_rates = [
        float(value)
        for result in cold_results
        if isinstance(
            (value := result["timings"].get("prompt_per_second")),
            (int, float),
        )
    ]
    concept_recalls = [
        value
        for result in results
        if (value := result["quality"]["concept_recall"]) is not None
    ]
    citation_precisions = [
        result["quality"]["citation_precision"] for result in results
    ]
    citation_format_compliance = [
        result["quality"]["citation_format_compliance"]
        for result in results
    ]
    citation_recalls = [
        result["quality"]["expected_source_citation_recall"]
        for result in results
    ]
    rubric_passes = [result["quality"]["rubric_pass"] for result in results]

    model_artifact: dict[str, object] | None = None
    if model_file is not None:
        model_artifact = {
            "path": str(model_file),
            "size_bytes": model_file.stat().st_size,
            "sha256": _sha256(model_file),
        }
    return {
        "schema_version": 1,
        "benchmark_id": "local-rag-generation-dev-v1",
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": payload.get("dataset_id"),
        "dataset_split": payload.get("split"),
        "dataset_sha256": dataset_sha256(dataset_path),
        "model": model,
        "model_artifact": model_artifact,
        "runtime_version": runtime_version,
        "endpoint": endpoint,
        "execution": {
            "accelerator": "cpu",
            "rounds": rounds,
            "case_count": len(cases),
            "request_count": len(results),
            "retrieval_k": k,
            "max_tokens": max_tokens,
            "server_pid": server_pid,
            "host": platform.platform(),
            "logical_cpu_count": os.cpu_count(),
        },
        "metrics": {
            "wall_latency_ms_p50": _percentile(latencies, 0.50),
            "wall_latency_ms_p95": _percentile(latencies, 0.95),
            "ttft_ms_p50": _percentile(ttfts, 0.50),
            "ttft_ms_p95": _percentile(ttfts, 0.95),
            "first_run_wall_latency_ms_p50": _percentile(cold_latencies, 0.50),
            "first_run_wall_latency_ms_p95": _percentile(cold_latencies, 0.95),
            "first_run_ttft_ms_p50": _percentile(cold_ttfts, 0.50),
            "first_run_ttft_ms_p95": _percentile(cold_ttfts, 0.95),
            "repeated_wall_latency_ms_p50": _percentile(repeated_latencies, 0.50),
            "repeated_wall_latency_ms_p95": _percentile(repeated_latencies, 0.95),
            "repeated_ttft_ms_p50": _percentile(repeated_ttfts, 0.50),
            "repeated_ttft_ms_p95": _percentile(repeated_ttfts, 0.95),
            "generation_tokens_per_second_p50": _percentile(generation_rates, 0.50),
            "generation_tokens_per_second_p05": _percentile(generation_rates, 0.05),
            "generation_tokens_per_second_p95": _percentile(generation_rates, 0.95),
            "prompt_tokens_per_second_p50": _percentile(prompt_rates, 0.50),
            "first_run_prompt_tokens_per_second_p50": _percentile(
                first_run_prompt_rates, 0.50
            ),
            "max_server_rss_mib": (
                max_rss_kib / 1024.0 if max_rss_kib is not None else None
            ),
            "required_concept_recall": (
                sum(concept_recalls) / len(concept_recalls) if concept_recalls else None
            ),
            "rubric_case_accuracy": sum(rubric_passes) / len(rubric_passes),
            "citation_precision": sum(citation_precisions) / len(citation_precisions),
            "citation_format_compliance": (
                sum(citation_format_compliance) / len(citation_format_compliance)
            ),
            "expected_source_citation_recall": (
                sum(citation_recalls) / len(citation_recalls)
            ),
        },
        "claim_boundary": (
            "개발셋·현재 x86_64 CPU 호스트의 기준선이다. 홀드아웃 정확도나 "
            "Raspberry Pi·Jetson 엣지 성능으로 일반화하지 않는다."
        ),
        "cases": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robot-ops-benchmark")
    parser.add_argument("--endpoint", default="http://127.0.0.1:18080")
    parser.add_argument("--model", required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("-k", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-tokens", type=int, default=160)
    parser.add_argument("--server-pid", type=int)
    parser.add_argument("--model-file", type=Path)
    parser.add_argument("--runtime-version")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_benchmark(
            endpoint=args.endpoint,
            model=args.model,
            db_path=args.db.expanduser().resolve(),
            dataset_path=args.dataset.expanduser().resolve(),
            rounds=args.rounds,
            k=args.k,
            timeout_seconds=args.timeout,
            max_tokens=args.max_tokens,
            server_pid=args.server_pid,
            model_file=(args.model_file.expanduser().resolve() if args.model_file else None),
            runtime_version=args.runtime_version,
        )
        args.output.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        args.output.expanduser().resolve().write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report["metrics"], ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
