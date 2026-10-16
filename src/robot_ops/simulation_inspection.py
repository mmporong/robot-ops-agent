from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


EARLY_CONTACT_PHASES = frozenset(
    {
        "RESET",
        "REORIENT_ABOVE",
        "PREGRASP_ABOVE",
        "ALIGN_MIDDLE",
        "APPROACH",
    }
)
MAX_ISSUES = 8


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name}은 JSON 객체여야 합니다")
    return value


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name}은 bool이어야 합니다")
    return value


def _require_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}은 유한한 숫자여야 합니다")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name}은 유한한 숫자여야 합니다")
    return result


def _require_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}은 0 이상의 정수여야 합니다")
    return value


def _sample_force(sample: Mapping[str, object], index: int) -> float:
    forces = sample.get("contact_force_n")
    if not isinstance(forces, Sequence) or isinstance(forces, (str, bytes)) or not forces:
        raise ValueError(f"samples[{index}].contact_force_n은 숫자 배열이어야 합니다")
    return max(
        _require_finite(value, f"samples[{index}].contact_force_n")
        for value in forces
    )


def _stored_premature_events(result: Mapping[str, object]) -> list[dict[str, object]]:
    events = result.get("events", [])
    if not isinstance(events, list):
        raise ValueError("events는 배열이어야 합니다")
    selected: list[dict[str, object]] = []
    for index, event_value in enumerate(events):
        event = _require_mapping(event_value, f"events[{index}]")
        if event.get("failure_code") != "premature_cup_contact":
            continue
        time_value = event.get("time_s")
        attempt_value = event.get("attempt")
        sample_index_value = event.get("sample_index")
        selected.append(
            {
                "event_index": index,
                "time_s": (
                    _require_finite(time_value, f"events[{index}].time_s")
                    if time_value is not None
                    else None
                ),
                "attempt": (
                    _require_nonnegative_int(attempt_value, f"events[{index}].attempt")
                    if attempt_value is not None
                    else None
                ),
                "sample_index": (
                    _require_nonnegative_int(
                        sample_index_value, f"events[{index}].sample_index"
                    )
                    if sample_index_value is not None
                    else None
                ),
            }
        )
    return selected


def _match_stored_events(
    stored_events: list[dict[str, object]],
    raw_hits: list[dict[str, object]],
    incidents: list[dict[str, object]],
    *,
    physics_dt_s: float,
    sample_count: int,
) -> dict[str, object]:
    tolerance_s = physics_dt_s / 2.0
    available_hits = set(range(len(raw_hits)))
    matched_incidents: set[int] = set()
    details: list[dict[str, object]] = []
    unassessable_count = 0

    for stored in stored_events:
        detail = dict(stored)
        time_s = stored["time_s"]
        attempt = stored["attempt"]
        sample_index = stored["sample_index"]
        missing_fields = [
            name for name, value in (("time_s", time_s), ("attempt", attempt))
            if value is None
        ]
        if missing_fields:
            detail.update(
                {
                    "match_status": "not_assessable",
                    "not_assessable_fields": missing_fields,
                    "matched_sample_index": None,
                    "time_delta_s": None,
                }
            )
            unassessable_count += 1
            details.append(detail)
            continue

        if sample_index is not None and int(sample_index) >= sample_count:
            detail.update(
                {
                    "match_status": "mismatch",
                    "mismatch_reason": "sample_index_out_of_range",
                    "matched_sample_index": None,
                    "time_delta_s": None,
                }
            )
            details.append(detail)
            continue

        candidates: list[tuple[float, int]] = []
        for hit_index in available_hits:
            hit = raw_hits[hit_index]
            if hit["attempt"] != attempt:
                continue
            if sample_index is not None and hit["sample_index"] != sample_index:
                continue
            delta_s = abs(float(hit["time_s"]) - float(time_s))
            if delta_s <= tolerance_s + 1e-12:
                candidates.append((delta_s, hit_index))
        if not candidates:
            detail.update(
                {
                    "match_status": "mismatch",
                    "mismatch_reason": "no_raw_hit_with_same_attempt_and_time",
                    "matched_sample_index": None,
                    "time_delta_s": None,
                }
            )
            details.append(detail)
            continue

        delta_s, hit_index = min(candidates)
        available_hits.remove(hit_index)
        hit = raw_hits[hit_index]
        detail.update(
            {
                "match_status": "matched",
                "matched_sample_index": hit["sample_index"],
                "time_delta_s": delta_s,
            }
        )
        details.append(detail)
        for incident_index, incident in enumerate(incidents):
            if (
                incident["attempt"] == hit["attempt"]
                and int(incident["sample_index"])
                <= int(hit["sample_index"])
                <= int(incident["last_sample_index"])
            ):
                matched_incidents.add(incident_index)
                break

    unmatched_incident_count = len(incidents) - len(matched_incidents)
    mismatched_count = sum(
        detail["match_status"] == "mismatch" for detail in details
    )
    if len(stored_events) != len(incidents) or mismatched_count:
        agrees: bool | None = False
        status = "mismatch"
    elif unassessable_count:
        agrees = None
        status = "not_assessable"
    else:
        agrees = unmatched_incident_count == 0
        status = "matched" if agrees else "mismatch"
    return {
        "event_match_status": status,
        "event_time_tolerance_s": tolerance_s,
        "stored_event_agrees_with_recheck": agrees,
        "unassessable_event_count": unassessable_count,
        "unmatched_incident_count": unmatched_incident_count,
        "stored_event_matches": details[:MAX_ISSUES],
        "stored_event_matches_truncated": len(details) > MAX_ISSUES,
    }


def inspect_simulation_result(
    result: Mapping[str, object],
    plan: Mapping[str, object],
    *,
    result_sha256: str,
    plan_sha256: str,
) -> dict[str, object]:
    """Summarize one stored simulator result without replaying the simulator."""

    hardware_accessed = _require_bool(result.get("hardware_accessed"), "hardware_accessed")
    if hardware_accessed:
        raise ValueError("hardware_accessed는 false여야 합니다")
    observation_source = result.get("observation_source")
    if observation_source != "simulator_ground_truth_not_rgb":
        raise ValueError(
            "observation_source는 simulator_ground_truth_not_rgb여야 합니다"
        )
    if plan.get("hardware_accessed") is not False:
        raise ValueError("plan.hardware_accessed는 false여야 합니다")
    if plan.get("observation_source") != observation_source:
        raise ValueError("result와 plan의 observation_source가 다릅니다")

    config = _require_mapping(plan.get("config"), "plan.config")
    threshold_n = _require_finite(
        config.get("minimum_contact_force_n"),
        "plan.config.minimum_contact_force_n",
    )
    if threshold_n < 0:
        raise ValueError("minimum_contact_force_n은 0 이상이어야 합니다")
    physics_dt_s = _require_finite(config.get("physics_dt_s"), "plan.config.physics_dt_s")
    if physics_dt_s <= 0:
        raise ValueError("physics_dt_s는 0보다 커야 합니다")

    samples = result.get("samples")
    if not isinstance(samples, list) or len(samples) < 2:
        raise ValueError("samples는 두 개 이상의 샘플을 포함해야 합니다")

    first_time: float | None = None
    last_time: float | None = None
    first_physics_time: float | None = None
    last_physics_time: float | None = None
    raw_hits: list[dict[str, object]] = []
    previous_time: float | None = None
    previous_physics_time: float | None = None
    previous_attempt: int | None = None
    physics_offset_s: float | None = None
    for index, sample_value in enumerate(samples):
        sample = _require_mapping(sample_value, f"samples[{index}]")
        time_s = _require_finite(sample.get("time_s"), f"samples[{index}].time_s")
        physics_time_s = _require_finite(
            sample.get("physics_time_s"), f"samples[{index}].physics_time_s"
        )
        attempt_value = sample.get("attempt")
        attempt = _require_nonnegative_int(attempt_value, f"samples[{index}].attempt")
        if previous_time is not None:
            sample_delta_s = time_s - previous_time
            physics_delta_s = physics_time_s - float(previous_physics_time)
            if sample_delta_s <= 0 or physics_delta_s <= 0:
                raise ValueError("sample 시간축은 엄격히 증가해야 합니다")
            if (
                abs(sample_delta_s - physics_dt_s) > 1e-6
                or abs(physics_delta_s - physics_dt_s) > 1e-6
            ):
                raise ValueError("sample 시간 간격이 plan.physics_dt_s와 다릅니다")
        if previous_attempt is not None and attempt < previous_attempt:
            raise ValueError("samples.attempt는 감소하면 안 됩니다")
        offset_s = physics_time_s - time_s
        if physics_offset_s is None:
            physics_offset_s = offset_s
        elif abs(offset_s - physics_offset_s) > (
            1e-6 + 1e-6 * max(abs(offset_s), abs(physics_offset_s))
        ):
            raise ValueError("physics_time_s와 time_s의 오프셋이 일정하지 않습니다")
        previous_time = time_s
        previous_physics_time = physics_time_s
        previous_attempt = attempt
        first_time = time_s if first_time is None else first_time
        last_time = time_s
        first_physics_time = (
            physics_time_s if first_physics_time is None else first_physics_time
        )
        last_physics_time = physics_time_s
        phase = sample.get("phase")
        if not isinstance(phase, str) or not phase:
            raise ValueError(f"samples[{index}].phase는 문자열이어야 합니다")
        force_n = _sample_force(sample, index)
        if phase in EARLY_CONTACT_PHASES and force_n >= threshold_n:
            raw_hits.append(
                {
                    "sample_index": index,
                    "time_s": time_s,
                    "phase": phase,
                    "attempt": attempt,
                    "force_n": force_n,
                }
            )

    incidents: list[dict[str, object]] = []
    for hit in raw_hits:
        if (
            incidents
            and incidents[-1]["phase"] == hit["phase"]
            and incidents[-1]["attempt"] == hit["attempt"]
            and incidents[-1]["last_sample_index"] == hit["sample_index"] - 1
        ):
            incident = incidents[-1]
            incident["last_sample_index"] = hit["sample_index"]
            incident["end_time_s"] = hit["time_s"]
            incident["sample_count"] = int(incident["sample_count"]) + 1
            if float(hit["force_n"]) > float(incident["maximum_force_n"]):
                incident["maximum_force_n"] = hit["force_n"]
                incident["maximum_force_sample_index"] = hit["sample_index"]
        else:
            incidents.append(
                {
                    "kind": "premature_cup_contact",
                    "symptom_only": True,
                    "sample_index": hit["sample_index"],
                    "last_sample_index": hit["sample_index"],
                    "time_s": hit["time_s"],
                    "end_time_s": hit["time_s"],
                    "phase": hit["phase"],
                    "attempt": hit["attempt"],
                    "maximum_force_n": hit["force_n"],
                    "maximum_force_sample_index": hit["sample_index"],
                    "threshold_n": threshold_n,
                    "sample_count": 1,
                    "reason_ko": (
                        "그리퍼를 닫기 전 단계에서 접촉력이 임계값 이상이었습니다."
                    ),
                }
            )

    stored_task_pass = _require_bool(result.get("task_pass"), "task_pass")
    completed = _require_bool(result.get("completed"), "completed")
    stop_reason = result.get("stop_reason")
    if not isinstance(stop_reason, str) or not stop_reason:
        raise ValueError("stop_reason은 비어 있지 않은 문자열이어야 합니다")
    recovery_enabled = _require_bool(result.get("recovery_enabled"), "recovery_enabled")
    retries_value = result.get("retry_count", result.get("recovery_attempts", 0))
    recovery_attempts = _require_nonnegative_int(retries_value, "retry_count")
    final_attempt = int(previous_attempt)
    final_attempt_premature_contact_count = sum(
        hit["attempt"] == final_attempt for hit in raw_hits
    )
    past_attempt_premature_contact_count = sum(
        hit["attempt"] < final_attempt for hit in raw_hits
    )
    inferred_recovered = bool(
        recovery_enabled
        and recovery_attempts > 0
        and recovery_attempts == final_attempt
        and stored_task_pass
        and past_attempt_premature_contact_count > 0
        and final_attempt_premature_contact_count == 0
    )
    recovered_value = result.get("recovered")
    stored_recovered = (
        _require_bool(recovered_value, "recovered")
        if recovered_value is not None
        else None
    )
    if stored_recovered is True:
        recovered = inferred_recovered
        recovery_status_agrees: bool | None = inferred_recovered
    elif stored_recovered is False:
        recovered = False
        recovery_status_agrees = not inferred_recovered
    else:
        recovered = inferred_recovered
        recovery_status_agrees = None

    stored_events = _stored_premature_events(result)
    recomputed_detected = bool(incidents)
    event_match = _match_stored_events(
        stored_events,
        raw_hits,
        incidents,
        physics_dt_s=physics_dt_s,
        sample_count=len(samples),
    )
    if recomputed_detected and not recovered:
        expected_stop_reason = "premature_cup_contact"
        stop_reason_agrees: bool | None = stop_reason == expected_stop_reason
    elif stored_task_pass:
        expected_stop_reason = "sequence_finished"
        stop_reason_agrees = stop_reason == expected_stop_reason
    else:
        expected_stop_reason = None
        stop_reason_agrees = None

    issue_count = len(incidents)
    return {
        "schema": "robot-ops-simulation-inspection/1",
        "status": "review_required" if recomputed_detected else "no_issue_in_checked_scope",
        "headline_ko": (
            "닫기 전 접촉력 이상 후보가 발견되었습니다."
            if recomputed_detected
            else "닫기 전 접촉력 이상 후보가 발견되지 않았습니다."
        ),
        "source": {
            "result_sha256": result_sha256,
            "plan_sha256": plan_sha256,
        },
        "runtime": {
            "sample_count": len(samples),
            "sim_time_s": (last_time - first_time) if first_time is not None else 0.0,
            "physics_progress_s": (
                last_physics_time - first_physics_time
                if first_physics_time is not None and last_physics_time is not None
                else 0.0
            ),
            "physics_dt_s": physics_dt_s,
            "time_axis_agrees": True,
            "hardware_accessed": hardware_accessed,
            "observation_source": observation_source,
            "recovery_enabled": recovery_enabled,
            "recovery_attempts": recovery_attempts,
            "final_attempt": final_attempt,
            "final_attempt_premature_contact_count": (
                final_attempt_premature_contact_count
            ),
            "past_attempt_premature_contact_count": (
                past_attempt_premature_contact_count
            ),
            "stored_recovered": stored_recovered,
            "recovered": recovered,
        },
        "stored_outcome": {
            "task_pass": stored_task_pass,
            "completed": completed,
            "stop_reason": stop_reason,
        },
        "premature_contact_check": {
            "rule": "close 전 단계의 max(contact_force_n) >= plan 임계값",
            "checked_phases": sorted(EARLY_CONTACT_PHASES),
            "threshold_n": threshold_n,
            "raw_matching_sample_count": len(raw_hits),
            "issue_count": issue_count,
            "issues": incidents[:MAX_ISSUES],
            "issues_truncated": issue_count > MAX_ISSUES,
        },
        "consistency": {
            "stored_premature_contact_event_count": len(stored_events),
            "stored_premature_contact_events": stored_events[:MAX_ISSUES],
            "stored_events_truncated": len(stored_events) > MAX_ISSUES,
            "recomputed_premature_contact": recomputed_detected,
            "expected_stop_reason": expected_stop_reason,
            "stop_reason_agrees_with_recheck": stop_reason_agrees,
            "recovery_status_agrees_with_samples": recovery_status_agrees,
            "retry_count_agrees_with_final_attempt": (
                recovery_attempts == final_attempt
            ),
            **event_match,
        },
        "interpretation": {
            "cause_status": "undetermined",
            "final_task_pass_is_separate_from_past_contact": True,
            "coordinates": "simulator_ground_truth",
            "limitations_ko": [
                "이 검사는 저장된 시뮬레이터 정답 좌표와 접촉력을 사용합니다.",
                "실물 센서와 RGB 영상에서 같은 증상을 검증한 결과가 아닙니다.",
                "접촉의 물리 원인은 이 결과만으로 확정할 수 없습니다.",
            ],
        },
    }
