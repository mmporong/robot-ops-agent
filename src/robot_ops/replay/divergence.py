"""`trajectory/1` 궤적의 명령·관측 어긋남 계산.

1. 정적 구간(명령·관측 모두 거의 움직이지 않는 구간)의 관절별 평균 `q_cmd − q_obs`를
   교정 차로 보고 빼서 텔레옵 리더·팔로워 교정 차를 제거한다.
2. 관절별 추종 오차(도)와, TCP가 있으면 명령·관측 TCP 거리(mm)를 프레임마다 계산한다.
3. 임계를 처음 넘은 시각을 관절·TCP 각각 기록한다.
4. 관측 동결: 모든 관절(그리퍼 포함) |Δq_obs| < eps 이고 어느 관절이든(그리퍼 포함)
   |Δq_cmd| > delta 인 프레임이 k개 이상 이어지면 동결 구간으로 본다. 구간 시작은 조건이 처음
   성립한 프레임(Δ가 i−1→i)이다. M0 기준(`inventory_scan.freeze_stats`)과 같게 그리퍼는
   LeRobot 원값(`gripper_units=lerobot_raw`) 차분에 같은 eps·delta 수치를 쓴다. 행에
   `gripper_cmd`·`gripper_obs`가 없거나 None이면 그 프레임은 관절만 본다.
5. `calibration_suspect`는 교정 차가 비정상적으로 크다는 표시로, TCP 지표에만 붙인다
   (관절 오차는 이미 교정 차를 뺐다).
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .schema import validate_trajectory_header, validate_trajectory_row

DEFAULT_FREEZE_EPS_DEG = 0.1
DEFAULT_FREEZE_DELTA_DEG = 0.5
DEFAULT_FREEZE_MIN_FRAMES = 3
DEFAULT_CALIBRATION_SUSPECT_DEG = 5.0


def read_trajectory(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = [line for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"빈 궤적 파일입니다: {path}")
    header = json.loads(lines[0])
    validate_trajectory_header(header)
    n = len(header["joint_names"])
    rows = []
    for i, line in enumerate(lines[1:]):
        row = json.loads(line)
        validate_trajectory_row(row, n, i)
        rows.append(row)
    return header, rows


def _deg(rad: float) -> float:
    return math.degrees(rad)


def find_static_window(
    rows: Sequence[dict[str, Any]],
    *,
    eps_deg: float = DEFAULT_FREEZE_EPS_DEG,
    min_frames: int = 5,
) -> tuple[int, int] | None:
    """명령·관측이 모두 eps 미만으로 움직이는 첫 연속 구간 [start, end] (프레임 번호)."""
    start = 0
    for i in range(1, len(rows) + 1):
        still = i < len(rows) and all(
            abs(_deg(a - b)) < eps_deg
            for key in ("q_cmd", "q_obs")
            for a, b in zip(rows[i][key], rows[i - 1][key])
        )
        if not still:
            if i - start >= min_frames:
                return (start, i - 1)
            start = i
    return None


def calibration_offsets(
    rows: Sequence[dict[str, Any]], window: tuple[int, int] | None
) -> list[float]:
    """정적 구간의 관절별 평균 `q_cmd − q_obs`(rad). 구간이 없으면 0."""
    n = len(rows[0]["q_cmd"]) if rows else 0
    if window is None:
        return [0.0] * n
    start, end = window
    segment = rows[start : end + 1]
    return [
        sum(r["q_cmd"][j] - r["q_obs"][j] for r in segment) / len(segment) for j in range(n)
    ]


def detect_freezes(
    rows: Sequence[dict[str, Any]],
    *,
    eps_deg: float = DEFAULT_FREEZE_EPS_DEG,
    delta_deg: float = DEFAULT_FREEZE_DELTA_DEG,
    min_frames: int = DEFAULT_FREEZE_MIN_FRAMES,
) -> list[dict[str, Any]]:
    freezes = []
    run_start: int | None = None
    for i in range(1, len(rows) + 1):
        frozen = False
        if i < len(rows):
            d_obs = [abs(_deg(a - b)) for a, b in zip(rows[i]["q_obs"], rows[i - 1]["q_obs"])]
            d_cmd = [abs(_deg(a - b)) for a, b in zip(rows[i]["q_cmd"], rows[i - 1]["q_cmd"])]
            for key, target in (("gripper_obs", d_obs), ("gripper_cmd", d_cmd)):
                now, prev = rows[i].get(key), rows[i - 1].get(key)
                if now is not None and prev is not None:
                    target.append(abs(float(now) - float(prev)))
            frozen = all(d < eps_deg for d in d_obs) and any(d > delta_deg for d in d_cmd)
        if frozen and run_start is None:
            run_start = i
        elif not frozen and run_start is not None:
            length = i - run_start
            if length >= min_frames:
                freezes.append(
                    {
                        "frame_start": run_start,
                        "frame_end": i - 1,
                        "n_frames": length,
                        "t_start_s": float(rows[run_start]["t"]),
                        "t_end_s": float(rows[i - 1]["t"]),
                    }
                )
            run_start = None
    return freezes


def _first_exceed(ts: Sequence[float], values: Sequence[float | None], threshold: float) -> float | None:
    for t, v in zip(ts, values):
        if v is not None and v > threshold:
            return float(t)
    return None


def compute_divergence(
    header: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    *,
    joint_threshold_deg: float = 3.0,
    tcp_threshold_mm: float = 20.0,
    static_window: tuple[int, int] | None = None,
    auto_static: bool = True,
    freeze_eps_deg: float = DEFAULT_FREEZE_EPS_DEG,
    freeze_delta_deg: float = DEFAULT_FREEZE_DELTA_DEG,
    freeze_min_frames: int = DEFAULT_FREEZE_MIN_FRAMES,
    calibration_suspect_deg: float = DEFAULT_CALIBRATION_SUSPECT_DEG,
) -> dict[str, Any]:
    names = list(header["joint_names"])
    if not rows:
        raise ValueError("궤적 행이 없습니다")
    window = static_window
    if window is None and auto_static:
        window = find_static_window(rows, eps_deg=freeze_eps_deg)
    offsets = calibration_offsets(rows, window)

    ts = [float(r["t"]) for r in rows]
    joint_err = [
        [abs(_deg((r["q_cmd"][j] - r["q_obs"][j]) - offsets[j])) for j in range(len(names))]
        for r in rows
    ]
    max_joint = [max((f[j] for f in joint_err), default=0.0) for j in range(len(names))]
    frame_max = [max(f) if f else 0.0 for f in joint_err]

    tcp_mm: list[float | None] = []
    for r in rows:
        a, b = r.get("tcp_cmd_m"), r.get("tcp_obs_m")
        tcp_mm.append(math.dist(a, b) * 1000.0 if a is not None and b is not None else None)
    has_tcp = any(v is not None for v in tcp_mm)

    offsets_deg = [_deg(o) for o in offsets]
    calibration_suspect = has_tcp and any(abs(o) > calibration_suspect_deg for o in offsets_deg)

    freezes = detect_freezes(
        rows, eps_deg=freeze_eps_deg, delta_deg=freeze_delta_deg, min_frames=freeze_min_frames
    )
    joint_first = _first_exceed(ts, frame_max, joint_threshold_deg)
    tcp_first = _first_exceed(ts, tcp_mm, tcp_threshold_mm) if has_tcp else None

    incidents = []
    for freeze in freezes:
        incidents.append({"kind": "freeze", **freeze})
    if joint_first is not None:
        end = max((t for t, v in zip(ts, frame_max) if v > joint_threshold_deg), default=joint_first)
        incidents.append({"kind": "joint_tracking", "t_start_s": joint_first, "t_end_s": end})
    if tcp_first is not None:
        end = max((t for t, v in zip(ts, tcp_mm) if v is not None and v > tcp_threshold_mm), default=tcp_first)
        incidents.append(
            {
                "kind": "tcp_distance",
                "t_start_s": tcp_first,
                "t_end_s": end,
                "calibration_suspect": calibration_suspect,
            }
        )
    incidents.sort(key=lambda item: (item["t_start_s"], item["kind"]))

    tcp_values = [v for v in tcp_mm if v is not None]
    return {
        "joint_names": names,
        "static_window": list(window) if window is not None else None,
        "calibration_offset_deg": offsets_deg,
        "thresholds": {"joint_deg": joint_threshold_deg, "tcp_mm": tcp_threshold_mm},
        "freeze_rule": {
            "eps_deg": freeze_eps_deg,
            "delta_deg": freeze_delta_deg,
            "min_frames": freeze_min_frames,
        },
        "max_joint_err_deg": dict(zip(names, max_joint)),
        "first_exceed_joint_s": joint_first,
        "tcp": (
            {
                "max_mm": max(tcp_values),
                "first_exceed_s": tcp_first,
                "calibration_suspect": calibration_suspect,
            }
            if has_tcp
            else None
        ),
        "freezes": freezes,
        "incidents": incidents,
        "series": {"t": ts, "joint_err_deg": joint_err, "tcp_mm": tcp_mm if has_tcp else None},
    }
