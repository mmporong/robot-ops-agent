"""저장된 궤적에서 관절별 위치 기록 정체 후보를 찾는 시간 기반 검사기."""

from __future__ import annotations

import math
from typing import Any

from .schema import validate_trajectory_header, validate_trajectory_row

SCHEMA = "robot-ops-recording-check/1"
DETECTOR = "joint_time_v2"


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: 유한한 숫자여야 합니다")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name}: 유한한 숫자여야 합니다")
    return result


def _parameter(value: object, name: str, *, allow_zero: bool) -> float:
    result = _number(value, name)
    if result < 0.0 or (not allow_zero and result == 0.0):
        relation = "0 이상" if allow_zero else "0보다 큰"
        raise ValueError(f"{name}: {relation} 값이어야 합니다")
    return result


def _issue(
    *,
    kind: str,
    joint: str,
    anchor: int,
    end: int,
    times: list[float],
    command_span: float,
    observation_span: float,
    unit: str,
    confirmed_at_frame: int,
    reason_ko: str,
    symptom_only: bool = True,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "joint": joint,
        "frame_start": anchor + 1,
        "frame_end": end,
        "n_frames": end - anchor,
        "t_start_s": times[anchor + 1],
        "t_end_s": times[end],
        "duration_s": times[end] - times[anchor + 1],
        "reference_frame": anchor,
        "reference_t_s": times[anchor],
        "evidence_duration_s": times[end] - times[anchor],
        "command_span": command_span,
        "observation_span": observation_span,
        "unit": unit,
        "confirmed_at_frame": confirmed_at_frame,
        "confirmation_t_s": times[confirmed_at_frame],
        "symptom_only": symptom_only,
        "reason_ko": reason_ko,
    }


def _stagnation_issues(
    *,
    name: str,
    commands: list[float],
    observations: list[float],
    times: list[float],
    observation_limit: float,
    command_limit: float,
    min_hold_s: float,
    max_gap_s: float,
    unit: str,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    anchor = 0
    obs_min = obs_max = observations[0]
    cmd_min = cmd_max = commands[0]
    confirmed: int | None = None

    def close(end: int) -> None:
        if confirmed is None:
            return
        issues.append(
            _issue(
                kind="joint_observation_stagnation",
                joint=name,
                anchor=anchor,
                end=end,
                times=times,
                command_span=cmd_max - cmd_min,
                observation_span=obs_max - obs_min,
                unit=unit,
                confirmed_at_frame=confirmed,
                reason_ko="명령 변화에 비해 관측 위치 변화가 작은 구간이어서 확인이 필요합니다.",
            )
        )

    for index in range(1, len(times)):
        gap = times[index] - times[index - 1]
        next_obs_min = min(obs_min, observations[index])
        next_obs_max = max(obs_max, observations[index])
        if gap > max_gap_s + 1e-9 or next_obs_max - next_obs_min > observation_limit + 1e-9:
            close(index - 1)
            anchor = index
            obs_min = obs_max = observations[index]
            cmd_min = cmd_max = commands[index]
            confirmed = None
            continue

        obs_min, obs_max = next_obs_min, next_obs_max
        cmd_min = min(cmd_min, commands[index])
        cmd_max = max(cmd_max, commands[index])
        if (
            confirmed is None
            and times[index] - times[anchor] >= min_hold_s - 1e-9
            and cmd_max - cmd_min > command_limit + 1e-9
        ):
            confirmed = index

    close(len(times) - 1)
    return issues


def inspect_recording(
    header: dict,
    rows: list,
    *,
    min_hold_s: float = 0.3,
    obs_span_deg: float = 0.1,
    min_command_span_deg: float = 0.5,
    max_gap_s: float = 0.25,
    gripper_observation_span: float = 0.1,
    gripper_command_span: float = 1.0,
) -> dict[str, Any]:
    """검사 가능한 신호에서 위치 기록 정체 *후보*를 찾는다.

    결과는 원인을 단정하지 않는다. 원본 센서 측정 시각과 순서 번호의 갱신 여부는
    이 함수의 검사 범위에 포함하지 않는다.
    """

    min_hold_s = _parameter(min_hold_s, "min_hold_s", allow_zero=False)
    obs_span_deg = _parameter(obs_span_deg, "obs_span_deg", allow_zero=True)
    min_command_span_deg = _parameter(
        min_command_span_deg, "min_command_span_deg", allow_zero=True
    )
    max_gap_s = _parameter(max_gap_s, "max_gap_s", allow_zero=False)
    gripper_observation_span = _parameter(
        gripper_observation_span, "gripper_observation_span", allow_zero=True
    )
    gripper_command_span = _parameter(
        gripper_command_span, "gripper_command_span", allow_zero=True
    )

    if not isinstance(header, dict):
        raise ValueError("header: 객체여야 합니다")
    if not isinstance(rows, list):
        raise ValueError("rows: 목록이어야 합니다")
    validate_trajectory_header(header)
    joint_names = list(header["joint_names"])
    if len(set(joint_names)) != len(joint_names):
        raise ValueError("header.joint_names: 중복 이름을 허용하지 않습니다")
    if len(rows) < 2:
        raise ValueError("rows: 시간 구간 검사를 위해 두 행 이상이어야 합니다")

    times: list[float] = []
    for index, row in enumerate(rows):
        validate_trajectory_row(row, len(joint_names), index)
        timestamp = _number(row["t"], f"rows[{index}].t")
        if index and timestamp <= times[-1]:
            raise ValueError("rows[].t: 유한하며 엄격히 증가해야 합니다")
        times.append(timestamp)

    resolution_doc = header.get("recording_check", {})
    if resolution_doc is None:
        resolution_doc = {}
    if not isinstance(resolution_doc, dict):
        raise ValueError("header.recording_check: 객체여야 합니다")
    resolutions = resolution_doc.get("observation_resolution_deg", {})
    if resolutions is None:
        resolutions = {}
    if not isinstance(resolutions, dict):
        raise ValueError("header.recording_check.observation_resolution_deg: 객체여야 합니다")
    unknown_resolution_names = sorted(set(resolutions) - set(joint_names))
    if unknown_resolution_names:
        raise ValueError(
            "header.recording_check.observation_resolution_deg: 알 수 없는 관절 이름이 있습니다: "
            + ", ".join(unknown_resolution_names)
        )
    clean_resolutions = {
        name: _parameter(value, f"observation_resolution_deg.{name}", allow_zero=True)
        for name, value in resolutions.items()
    }
    effective_command_spans = {
        name: max(min_command_span_deg, 2.0 * clean_resolutions.get(name, 0.0))
        for name in joint_names
    }
    effective_max_gap_s = max(max_gap_s, 2.5 / float(header["fps"]))

    parameters = {
        "min_hold_s": min_hold_s,
        "obs_span_deg": obs_span_deg,
        "min_command_span_deg": min_command_span_deg,
        "max_gap_s": max_gap_s,
        "gripper_observation_span": gripper_observation_span,
        "gripper_command_span": gripper_command_span,
        "basis": "proposed_default",
        "field_validated": False,
        "effective_max_gap_s": effective_max_gap_s,
        "effective_command_span_deg": effective_command_spans,
    }
    issues: list[dict[str, Any]] = []

    for joint_index, joint_name in enumerate(joint_names):
        commands = [math.degrees(float(row["q_cmd"][joint_index])) for row in rows]
        observations = [math.degrees(float(row["q_obs"][joint_index])) for row in rows]
        issues.extend(
            _stagnation_issues(
                name=joint_name,
                commands=commands,
                observations=observations,
                times=times,
                observation_limit=obs_span_deg,
                command_limit=effective_command_spans[joint_name],
                min_hold_s=min_hold_s,
                max_gap_s=effective_max_gap_s,
                unit="deg",
            )
        )

    gripper_units = header.get("gripper_units")
    gripper_checked = False
    gripper_not_checked_reason: str | None = None
    gripper_checked_ranges: list[list[int]] = []
    gripper_missing_frames: list[int] = []
    if gripper_units != "lerobot_raw":
        gripper_not_checked_reason = "header.gripper_units가 lerobot_raw가 아니어서 단위를 해석하지 않았습니다."
    else:
        for index, row in enumerate(rows):
            for key in ("gripper_cmd", "gripper_obs"):
                if row.get(key) is not None:
                    _number(row[key], f"rows[{index}].{key}")
        missing = [
            index
            for index, row in enumerate(rows)
            if row.get("gripper_cmd") is None or row.get("gripper_obs") is None
        ]
        gripper_missing_frames = missing
        missing_set = set(missing)
        complete = [index not in missing_set for index in range(len(rows))]
        segment_start: int | None = None
        segments: list[tuple[int, int]] = []
        for index in range(len(rows) + 1):
            available = index < len(rows) and complete[index]
            if available and segment_start is None:
                segment_start = index
            elif not available and segment_start is not None:
                if index - segment_start >= 2:
                    segments.append((segment_start, index - 1))
                segment_start = None
        for start, end in segments:
            grip_commands = [
                _number(rows[index]["gripper_cmd"], f"rows[{index}].gripper_cmd")
                for index in range(start, end + 1)
            ]
            grip_observations = [
                _number(rows[index]["gripper_obs"], f"rows[{index}].gripper_obs")
                for index in range(start, end + 1)
            ]
            segment_issues = _stagnation_issues(
                name="gripper",
                commands=grip_commands,
                observations=grip_observations,
                times=times[start : end + 1],
                observation_limit=gripper_observation_span,
                command_limit=gripper_command_span,
                min_hold_s=min_hold_s,
                max_gap_s=effective_max_gap_s,
                unit="lerobot_raw",
            )
            for issue in segment_issues:
                for key in ("frame_start", "frame_end", "reference_frame", "confirmed_at_frame"):
                    issue[key] += start
            issues.extend(segment_issues)
            gripper_checked_ranges.append([start, end])
        gripper_checked = bool(segments)
        if missing:
            gripper_not_checked_reason = "값이 없는 프레임을 경계로 나누어 값이 있는 연속 구간만 검사했습니다."
        elif not segments:
            gripper_not_checked_reason = "검사할 수 있는 연속 그리퍼 구간이 없습니다."

    for index in range(1, len(times)):
        gap = times[index] - times[index - 1]
        if gap > effective_max_gap_s + 1e-9:
            issues.append(
                _issue(
                    kind="sampling_gap",
                    joint="timestamp",
                    anchor=index - 1,
                    end=index,
                    times=times,
                    command_span=0.0,
                    observation_span=0.0,
                    unit="s",
                    confirmed_at_frame=index,
                    reason_ko="연속 샘플 사이의 시간 간격이 검사 한도를 넘어 기록 연속성 확인이 필요합니다.",
                )
            )

    issues.sort(key=lambda item: (item["frame_start"], item["kind"], item["joint"]))
    return {
        "schema": SCHEMA,
        "detector": DETECTOR,
        "headline_ko": "위치 기록 정체 의심",
        "status": "review_required" if issues else "no_issue_in_checked_scope",
        "parameters": parameters,
        "issues": issues,
        "coverage": {
            "checked_joints": joint_names,
            "gripper_checked": gripper_checked,
            "gripper_not_checked_reason": gripper_not_checked_reason,
            "gripper_checked_ranges": gripper_checked_ranges,
            "gripper_missing_frames": gripper_missing_frames,
            "sampling": {
                "requested_max_gap_s": max_gap_s,
                "effective_max_gap_s": effective_max_gap_s,
                "policy": "max(requested_max_gap_s, 2.5 / header.fps)",
                "nominal_rate_handling": "최소 2.5개 공칭 샘플 주기를 허용합니다.",
            },
            "observation_resolution": {
                "known_deg": clean_resolutions,
                "unknown_joints": [name for name in joint_names if name not in clean_resolutions],
                "limitation_ko": (
                    "분해능을 모르는 관절은 반복값만으로 정상 양자화와 기록 정체를 구분할 수 없습니다."
                ),
            },
            "observation_freshness": "not_assessable",
            "freshness": {
                "status": "not_assessable",
                "reason_ko": "이 검사는 원본 센서 측정 시각·순서 번호의 갱신 여부를 평가하지 않습니다.",
            },
            "observation_freshness_reason_ko": (
                "이 검사는 원본 센서 측정 시각·순서 번호의 갱신 여부를 평가하지 않습니다."
            ),
        },
        "cause_status": "undetermined",
        "next_checks_ko": [
            "표시된 구간의 카메라 영상과 관절 상태 그래프를 함께 확인합니다.",
            "수집기의 수신 시각, 원본 센서 측정 시각, 메시지 순서 번호를 확인합니다.",
            "통신 지연, 기구 걸림, 제어 한계 여부를 별도 진단으로 구분합니다.",
        ],
    }
