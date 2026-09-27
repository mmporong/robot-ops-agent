"""결정적 섭동 격자, 조건별 통과 지도 요약, 반복 판정 일치율.

- 격자 순서: y 오름차순 바깥, x 오름차순 안쪽(행 우선). 입력 목록 순서를 그대로 쓴다.
- 반복 점: 격자 목록에서 균등 간격 인덱스 `round(i*(N-1)/(n-1))`를 고른다(25점·5개 → 대각선).
- `valid` = status가 ok인 셀 수. ok가 아닌 셀(infra·timeout)이 `max_invalid`(기본 3)를
  넘으면 `invalid=true`.
- 판정 일치율(verdict_agreement): 반복 점마다 유효 반복 판정 중 최빈 판정의 비율을 구하고
  그 평균을 낸다. 유효 반복이 없는 점은 제외한다. 3회 중 1회가 뒤집히면 점마다 2/3.
- max_metric_spread: 반복 점마다 검사 id별 숫자 값의 (최대 − 최소)를 구하고 점 전체 최대.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

MAX_INVALID_CELLS = 3


def grid_points(xs: Sequence[float], ys: Sequence[float]) -> list[tuple[float, float]]:
    return [(x, y) for y in ys for x in xs]


def select_repeat_points(points: Sequence[tuple[float, float]], n: int) -> list[tuple[float, float]]:
    if n <= 0 or not points:
        return []
    if n >= len(points):
        return list(points)
    if n == 1:
        return [points[(len(points) - 1) // 2]]
    indices = sorted({round(i * (len(points) - 1) / (n - 1)) for i in range(n)})
    return [points[i] for i in indices]


def summarize_condition(cells: Sequence[dict[str, Any]], *, max_invalid: int = MAX_INVALID_CELLS) -> dict[str, Any]:
    valid = [c for c in cells if c.get("status") == "ok"]
    passed = sum(1 for c in valid if c.get("verdict") == "pass")
    not_ok = len(cells) - len(valid)
    return {
        "pass": passed,
        "valid": len(valid),
        "total": len(cells),
        "infra": sum(1 for c in cells if c.get("status") == "infra"),
        "timeout": sum(1 for c in cells if c.get("status") == "timeout"),
        "invalid": not_ok > max_invalid,
    }


def verdict_agreement(repeats: Mapping[Any, Sequence[dict[str, Any]]]) -> dict[str, Any]:
    """repeats: 점 → 그 점의 반복 셀 목록."""
    shares = []
    unanimous = 0
    spread: dict[str, float] = {}
    for cells in repeats.values():
        valid = [c for c in cells if c.get("status") == "ok"]
        if not valid:
            continue
        counts = Counter(c.get("verdict") for c in valid)
        top = counts.most_common(1)[0][1]
        shares.append(top / len(valid))
        if top == len(valid):
            unanimous += 1
        per_check: dict[str, list[float]] = {}
        for cell in valid:
            for check in cell.get("checks") or []:
                value = check.get("value")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    per_check.setdefault(str(check.get("id")), []).append(float(value))
        for check_id, values in per_check.items():
            spread[check_id] = max(spread.get(check_id, 0.0), max(values) - min(values))
    return {
        "verdict_agreement": (sum(shares) / len(shares)) if shares else None,
        "unanimous_points": unanimous,
        "measured_points": len(shares),
        "max_metric_spread": dict(sorted(spread.items())),
    }
