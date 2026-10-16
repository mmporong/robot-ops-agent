from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence


PRE_CLOSE_PHASES = frozenset(
    {"RESET", "REORIENT_ABOVE", "PREGRASP_ABOVE", "ALIGN_MIDDLE", "APPROACH"}
)
_TIME_TOLERANCE_S = 1e-6


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}은 유한한 숫자여야 합니다")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name}은 유한한 숫자여야 합니다")
    return number


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}은 0 이상의 정수여야 합니다")
    return value


def _vector(value: object, length: int, name: str) -> list[float]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != length
    ):
        raise ValueError(f"{name}은 유한한 숫자 {length}개여야 합니다")
    return [_finite(item, f"{name}[{index}]") for index, item in enumerate(value)]


def _forces(value: object, name: str) -> list[float]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 2
    ):
        raise ValueError(f"{name}은 양측 접촉력인 유한한 숫자 2개여야 합니다")
    return [_finite(item, f"{name}[{index}]") for index, item in enumerate(value)]


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name}은 JSON 객체여야 합니다")
    return value


def _check(check_id: str, passed: bool, value: object, threshold: object, unit: str) -> dict[str, object]:
    return {
        "id": check_id,
        "pass": passed,
        "value": value,
        "threshold": threshold,
        "unit": unit,
    }


def _validate_time_axis(
    samples: Sequence[object],
    *,
    expected_dt_s: float | None,
) -> tuple[list[Mapping[str, object]], float]:
    if len(samples) < 2:
        raise ValueError("samples는 두 개 이상의 샘플을 포함해야 합니다")

    checked: list[Mapping[str, object]] = []
    times: list[float] = []
    physics_times: list[float] = []
    previous_attempt: int | None = None
    for index, value in enumerate(samples):
        sample = _mapping(value, f"samples[{index}]")
        time_s = _finite(sample.get("time_s"), f"samples[{index}].time_s")
        physics_time_s = _finite(
            sample.get("physics_time_s"), f"samples[{index}].physics_time_s"
        )
        attempt = _nonnegative_int(sample.get("attempt", 0), f"samples[{index}].attempt")
        if previous_attempt is not None and attempt < previous_attempt:
            raise ValueError("samples.attempt는 감소하면 안 됩니다")
        previous_attempt = attempt
        times.append(time_s)
        physics_times.append(physics_time_s)
        checked.append(sample)

    deltas = [times[index] - times[index - 1] for index in range(1, len(times))]
    physics_deltas = [
        physics_times[index] - physics_times[index - 1]
        for index in range(1, len(physics_times))
    ]
    if any(delta <= 0 for delta in deltas + physics_deltas):
        raise ValueError("sample 시간축은 엄격히 증가해야 합니다")
    dt_s = expected_dt_s if expected_dt_s is not None else deltas[0]
    if dt_s <= 0:
        raise ValueError("physics_dt_s는 0보다 커야 합니다")
    for delta, physics_delta in zip(deltas, physics_deltas):
        if (
            abs(delta - dt_s) > _TIME_TOLERANCE_S
            or abs(physics_delta - dt_s) > _TIME_TOLERANCE_S
            or abs(delta - physics_delta) > _TIME_TOLERANCE_S
        ):
            raise ValueError("sample 시간 간격이 일정한 물리 시간 간격과 다릅니다")
    offsets = [physics - time for time, physics in zip(times, physics_times)]
    if any(abs(offset - offsets[0]) > _TIME_TOLERANCE_S for offset in offsets[1:]):
        raise ValueError("physics_time_s와 time_s의 오프셋이 일정하지 않습니다")
    return checked, dt_s


def derive_plan_correction(
    planned_center_m: object,
    reset_samples: Sequence[object],
    *,
    window_s: float,
    max_motion_m: float,
    contact_threshold_n: float,
) -> dict[str, object]:
    """초기 RESET 관측과 명시된 계획 중심의 상대 오차만 계산한다."""

    planned = _vector(planned_center_m, 3, "planned_center_m")
    duration_s = _finite(window_s, "window_s")
    motion_limit_m = _finite(max_motion_m, "max_motion_m")
    force_limit_n = _finite(contact_threshold_n, "contact_threshold_n")
    if duration_s <= 0:
        raise ValueError("window_s는 0보다 커야 합니다")
    if motion_limit_m < 0 or force_limit_n < 0:
        raise ValueError("max_motion_m과 contact_threshold_n은 0 이상이어야 합니다")

    samples, dt_s = _validate_time_axis(reset_samples, expected_dt_s=None)
    positions: list[list[float]] = []
    peak_force_n = 0.0
    for index, sample in enumerate(samples):
        if sample.get("phase") != "RESET":
            raise ValueError("보정 관측 창에는 RESET 샘플만 있어야 합니다")
        positions.append(
            _vector(sample.get("cup_position_m"), 3, f"samples[{index}].cup_position_m")
        )
        peak_force_n = max(
            peak_force_n,
            max(_forces(sample.get("contact_force_n"), f"samples[{index}].contact_force_n")),
        )

    elapsed_s = _finite(samples[-1].get("time_s"), "samples[-1].time_s") - _finite(
        samples[0].get("time_s"), "samples[0].time_s"
    )
    if elapsed_s < duration_s - dt_s - _TIME_TOLERANCE_S:
        raise ValueError("RESET 관측 샘플이 요청한 관측 창 끝까지 충분하지 않습니다")
    if elapsed_s > duration_s + dt_s + _TIME_TOLERANCE_S:
        raise ValueError("RESET 관측 샘플이 요청한 관측 창을 벗어납니다")

    first = positions[0]
    maximum_motion_m = max(math.dist(first, position) for position in positions)
    if maximum_motion_m > motion_limit_m:
        raise ValueError("RESET 관측 창에서 컵이 허용 범위보다 많이 움직였습니다")
    if peak_force_n >= force_limit_n:
        raise ValueError("RESET 관측 창에서 접촉력이 임계값 이상입니다")

    observed = [statistics.median(position[axis] for position in positions) for axis in range(3)]
    error_mm = [(observed[axis] - planned[axis]) * 1000.0 for axis in range(2)]
    material = math.hypot(observed[0] - planned[0], observed[1] - planned[1]) > motion_limit_m
    correction_mm = error_mm if material else [0.0, 0.0]
    return {
        "schema": "robot-ops-cup-plan-correction/1",
        "status": "candidate_available" if material else "no_material_pose_mismatch",
        "correction_mm": correction_mm,
        "planned_center_m": planned,
        "observed_center_m": observed,
        "observed_minus_planned_xy_mm": error_mm,
        "observation": {
            "sample_count": len(samples),
            "start_time_s": _finite(samples[0].get("time_s"), "samples[0].time_s"),
            "end_time_s": _finite(samples[-1].get("time_s"), "samples[-1].time_s"),
            "elapsed_s": elapsed_s,
            "sample_interval_s": dt_s,
            "maximum_motion_m": maximum_motion_m,
            "maximum_contact_force_n": peak_force_n,
        },
        "thresholds": {
            "window_s": duration_s,
            "maximum_observation_motion_m": motion_limit_m,
            "minimum_contact_force_n": force_limit_n,
        },
        "interpretation": {
            "cause_status": "undetermined",
            "coordinates": "simulator_ground_truth",
            "limitations_ko": [
                "보정값은 계획 중심과 RESET 관측 중심의 상대 오차입니다.",
                "이 오차가 생긴 물리 원인은 이 결과만으로 확정할 수 없습니다.",
                "시뮬레이터 정답 좌표를 사용하는 실험에만 적용됩니다.",
            ],
        },
    }


def evaluate_common_reference(
    result: Mapping[str, object],
    config: Mapping[str, object],
    reference_m: object,
) -> dict[str, object]:
    """저장된 성공값과 계획 중심을 배제하고 공통 초기 위치로 상승을 판정한다."""

    if not isinstance(result, Mapping) or not isinstance(config, Mapping):
        raise ValueError("result와 config는 JSON 객체여야 합니다")
    reference = _vector(reference_m, 3, "reference_m")
    required_thresholds = {
        "physics_dt_s": "physics_dt_s",
        "minimum_lift_m": "minimum_lift_m",
        "minimum_contact_force_n": "minimum_contact_force_n",
        "maximum_tracking_error_rad": "maximum_tracking_error_rad",
        "maximum_midbody_height_error_m": "maximum_midbody_height_error_m",
        "maximum_contact_center_error_m": "maximum_contact_center_error_m",
        "maximum_cup_lateral_drift_m": "maximum_cup_lateral_drift_m",
        "maximum_cup_tilt_deg": "maximum_cup_tilt_deg",
    }
    thresholds = {
        key: _finite(config.get(source), f"config.{source}")
        for key, source in required_thresholds.items()
    }
    if thresholds["physics_dt_s"] <= 0:
        raise ValueError("config.physics_dt_s는 0보다 커야 합니다")
    if any(value < 0 for key, value in thresholds.items() if key != "physics_dt_s"):
        raise ValueError("상승 판정 임계값은 0 이상이어야 합니다")

    raw_samples = result.get("samples")
    if not isinstance(raw_samples, Sequence) or isinstance(raw_samples, (str, bytes)):
        raise ValueError("result.samples는 배열이어야 합니다")
    samples, _ = _validate_time_axis(
        raw_samples, expected_dt_s=thresholds["physics_dt_s"]
    )

    parsed: list[dict[str, object]] = []
    early_contact_count = 0
    maximum_preclose_force_n = 0.0
    for index, sample in enumerate(samples):
        phase = sample.get("phase")
        if not isinstance(phase, str) or not phase:
            raise ValueError(f"samples[{index}].phase는 문자열이어야 합니다")
        attempt = _nonnegative_int(sample.get("attempt", 0), f"samples[{index}].attempt")
        position = _vector(
            sample.get("cup_position_m"), 3, f"samples[{index}].cup_position_m"
        )
        forces = _forces(sample.get("contact_force_n"), f"samples[{index}].contact_force_n")
        if phase in PRE_CLOSE_PHASES:
            force = max(forces)
            maximum_preclose_force_n = max(maximum_preclose_force_n, force)
            if force >= thresholds["minimum_contact_force_n"]:
                early_contact_count += 1
        parsed.append(
            {
                "sample": sample,
                "phase": phase,
                "attempt": attempt,
                "position": position,
                "forces": forces,
            }
        )

    final_attempt = max(item["attempt"] for item in parsed)
    hold = [
        item
        for item in parsed
        if item["attempt"] == final_attempt and item["phase"] == "LIFT_HOLD"
    ]
    needed = max(2, math.ceil(1.0 / thresholds["physics_dt_s"]))
    terminal = hold[-needed:]
    enough = len(terminal) == needed

    if terminal:
        lift_mm = min(
            (item["position"][2] - reference[2]) * 1000.0 for item in terminal
        )
        force_n = min(min(item["forces"]) for item in terminal)
        arm_rad = max(
            _finite(item["sample"].get("arm_error_rad"), "terminal.arm_error_rad")
            for item in terminal
        )
        mid_mm = max(
            abs(_finite(item["sample"].get("midbody_height_error_m"), "terminal.midbody_height_error_m"))
            * 1000.0
            for item in terminal
        )
        center_mm = max(
            _finite(item["sample"].get("contact_center_error_m"), "terminal.contact_center_error_m")
            * 1000.0
            for item in terminal
        )
        drift_mm = max(
            math.dist(item["position"][:2], reference[:2]) * 1000.0
            for item in terminal
        )
        tilt_deg = max(
            _finite(item["sample"].get("cup_tilt_deg"), "terminal.cup_tilt_deg")
            for item in terminal
        )
        finite = True
    else:
        lift_mm = force_n = arm_rad = mid_mm = center_mm = drift_mm = tilt_deg = None
        finite = False

    completed = result.get("completed") is True
    normal_stop = result.get("stop_reason") == "sequence_finished"
    recovery_disabled = result.get("recovery_enabled") is False
    retry_count = result.get("retry_count", result.get("recovery_attempts"))
    retry_zero = retry_count == 0 and not isinstance(retry_count, bool)
    checks = [
        _check("completed", completed, result.get("completed"), True, "bool"),
        _check("normal_stop_reason", normal_stop, result.get("stop_reason"), "sequence_finished", "text"),
        _check("recovery_disabled", recovery_disabled, result.get("recovery_enabled"), False, "bool"),
        _check("retry_count_zero", retry_zero, retry_count, 0, "count"),
        _check("single_attempt", final_attempt == 0, final_attempt, 0, "attempt"),
        _check(
            "no_preclose_contact",
            early_contact_count == 0,
            maximum_preclose_force_n,
            thresholds["minimum_contact_force_n"],
            "N",
        ),
        _check("hold_window_samples", enough, len(terminal), needed, "samples"),
        _check("samples_finite", finite, finite, True, "bool"),
        _check(
            "proxy_lift_height",
            lift_mm is not None and lift_mm >= thresholds["minimum_lift_m"] * 1000.0,
            lift_mm,
            thresholds["minimum_lift_m"] * 1000.0,
            "mm",
        ),
        _check(
            "finger_contact_force_min",
            force_n is not None and force_n >= thresholds["minimum_contact_force_n"],
            force_n,
            thresholds["minimum_contact_force_n"],
            "N",
        ),
        _check(
            "arm_tracking_error_max",
            arm_rad is not None and arm_rad <= thresholds["maximum_tracking_error_rad"],
            arm_rad,
            thresholds["maximum_tracking_error_rad"],
            "rad",
        ),
        _check(
            "midbody_height_error_max",
            mid_mm is not None and mid_mm <= thresholds["maximum_midbody_height_error_m"] * 1000.0,
            mid_mm,
            thresholds["maximum_midbody_height_error_m"] * 1000.0,
            "mm",
        ),
        _check(
            "contact_center_error_max",
            center_mm is not None and center_mm <= thresholds["maximum_contact_center_error_m"] * 1000.0,
            center_mm,
            thresholds["maximum_contact_center_error_m"] * 1000.0,
            "mm",
        ),
        _check(
            "cup_lateral_drift_max",
            drift_mm is not None and drift_mm <= thresholds["maximum_cup_lateral_drift_m"] * 1000.0,
            drift_mm,
            thresholds["maximum_cup_lateral_drift_m"] * 1000.0,
            "mm",
        ),
        _check(
            "cup_tilt_max",
            tilt_deg is not None and tilt_deg <= thresholds["maximum_cup_tilt_deg"],
            tilt_deg,
            thresholds["maximum_cup_tilt_deg"],
            "deg",
        ),
    ]
    passed = all(check["pass"] is True for check in checks)
    first_failure = next((str(check["id"]) for check in checks if check["pass"] is not True), None)
    return {
        "schema": "robot-ops-common-reference-evaluation/1",
        "verdict": "pass" if passed else "fail",
        "passed": passed,
        "failure_code": None if passed else f"{first_failure}_failed",
        "reference_m": reference,
        "reference_basis": "frozen_observed_initial_position",
        "attempt": final_attempt,
        "checks": checks,
        "stored_outcome_ignored": True,
        "planned_center_ignored": True,
    }
