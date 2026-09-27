"""`judge` (stdlib 전용): 결과 파일만 읽어 판정한다.

cup_contact
- verdict = 저장된 `task_pass`(있으면) 또는 `rigid_proxy_lift_pass`. 판정을 새로 만들지 않는다.
- checks = 저장값 + 같은 폴더 `plan.json`의 config로 `samples`에서 다시 계산한 상승 판정 항목
  (팀 `cup_contact_model.evaluate_lift`와 같은 식: 마지막 시도의 LIFT_HOLD 끝 1초 창).
  다시 계산한 값이 저장값과 다르면 `sample_recheck.agrees=false`로 남긴다(발견 목록용, 수정하지 않음).
- 조기 종료(실패 이벤트로 시뮬이 끝나 LIFT_HOLD 샘플이 없음)면 샘플로 잴 수 없는 검사는 `pass=None`
  (측정 안 됨)으로 낸다. 판정을 결정한 원인은 첫 행 `failure_event`(값 = 실패 코드, `t_s` = 이벤트 시각)로
  남긴다. `sample_recheck`의 재계산·일치 비교는 내부 bool로 하므로 영향이 없다.
- judgement_basis = recovery_enabled가 참이면 `oracle_recovery_sim_coordinates`, 아니면 `rigid_proxy_lift`.
- assembly = `left_arm_proxy`(assembly_mode `left_contact_proxy` 또는 기록 없음).

drive_kinematic
- `final_alignment_bounded`: 지평 안 도착 + 마지막 창 반전율 ≤ 임계.
  임계는 `evaluations/replay/f1004_thresholds.json`(결과 전에 고정)에서 읽는다.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
F1004_THRESHOLDS = REPO_ROOT / "evaluations" / "replay" / "f1004_thresholds.json"


def _load(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"결과 파일이 JSON 객체가 아닙니다: {path}")
    return doc


def _check(check_id: str, passed, value, threshold, unit: str) -> dict:
    return {"id": check_id, "pass": passed, "value": value, "threshold": threshold, "unit": unit}


def _finite(values) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)


def _cup_failure_code(result: dict) -> str | None:
    for event in reversed(result.get("events") or []):
        if isinstance(event, dict) and event.get("failure_code"):
            return str(event["failure_code"])
    reason = result.get("stop_reason") or result.get("failure")
    if isinstance(reason, str) and reason and reason != "sequence_finished":
        return reason
    return None


def _cup_failure_event(result: dict) -> dict | None:
    """마지막 실패 이벤트(코드·시각)."""
    for event in reversed(result.get("events") or []):
        if isinstance(event, dict) and event.get("failure_code"):
            t = event.get("time_s")
            return {"code": str(event["failure_code"]),
                    "t_s": float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None}
    return None


NOT_MEASURED_ON_EARLY_STOP = ("hold_window_samples", "samples_finite")


def _mark_not_measured(checks: list[dict]) -> list[dict]:
    """조기 종료로 잴 수 없었던 검사를 pass=None으로 바꾼 사본(값이 없는 검사 + 샘플 창 검사)."""
    out = []
    for check in checks:
        if check["value"] is None or check["id"] in NOT_MEASURED_ON_EARLY_STOP:
            check = {**check, "pass": None}
        out.append(check)
    return out


def _first_failed(recheck: dict | None) -> str | None:
    """완료됐지만 판정이 거짓인 결과(기록된 실패 코드 없음)의 대표 코드: 재계산에서 처음 실패한 항목."""
    for check in (recheck or {}).get("checks", []):
        if not check["pass"]:
            return f"{check['id']}_out_of_bound"
    return None


def recheck_lift(result: dict, config: dict) -> dict | None:
    """저장된 samples로 evaluate_lift의 상승 판정 항목을 다시 계산한다(stdlib)."""
    samples = [s for s in result.get("samples") or [] if isinstance(s, dict)]
    required = ("physics_dt_s", "minimum_lift_m", "minimum_contact_force_n", "maximum_tracking_error_rad",
                "maximum_midbody_height_error_m", "maximum_contact_center_error_m",
                "maximum_cup_lateral_drift_m", "maximum_cup_tilt_deg", "cup_center_m")
    if not samples or any(k not in config for k in required):
        return None
    last_attempt = max((s.get("attempt", 0) for s in samples), default=0)
    current = [s for s in samples if s.get("attempt", 0) == last_attempt]
    hold = [s for s in current if s.get("phase") == "LIFT_HOLD"]
    need = max(2, math.ceil(1.0 / float(config["physics_dt_s"])))
    terminal = hold[-need:]
    center = result.get("evaluation_center_m") or config["cup_center_m"]
    enough = len(terminal) == need
    if terminal:
        lift_mm = min((s["cup_position_m"][2] - center[2]) * 1000.0 for s in terminal)
        force_n = min(min(s["contact_force_n"]) for s in terminal)
        arm_rad = max(s["arm_error_rad"] for s in terminal)
        mid_mm = max(abs(s["midbody_height_error_m"]) * 1000.0 for s in terminal)
        ctr_mm = max(s["contact_center_error_m"] * 1000.0 for s in terminal)
        drift_mm = max(math.dist(s["cup_position_m"][:2], center[:2]) * 1000.0 for s in terminal)
        tilt_deg = max(s["cup_tilt_deg"] for s in terminal)
        finite = all(
            _finite([*s["cup_position_m"], *s["contact_force_n"], s["arm_error_rad"],
                     s["contact_center_error_m"], s["cup_tilt_deg"]])
            for s in terminal
        )
    else:
        lift_mm = force_n = arm_rad = mid_mm = ctr_mm = drift_mm = tilt_deg = None
        finite = False
    checks = [
        _check("hold_window_samples", enough, len(terminal), need, "samples"),
        _check("samples_finite", finite, finite, True, "bool"),
        _check("proxy_lift_height", lift_mm is not None and lift_mm >= config["minimum_lift_m"] * 1000.0,
               lift_mm, config["minimum_lift_m"] * 1000.0, "mm"),
        _check("finger_contact_force_min", force_n is not None and force_n >= config["minimum_contact_force_n"],
               force_n, config["minimum_contact_force_n"], "N"),
        _check("arm_tracking_error_max", arm_rad is not None and arm_rad <= config["maximum_tracking_error_rad"],
               arm_rad, config["maximum_tracking_error_rad"], "rad"),
        _check("midbody_height_error_max",
               mid_mm is not None and mid_mm <= config["maximum_midbody_height_error_m"] * 1000.0,
               mid_mm, config["maximum_midbody_height_error_m"] * 1000.0, "mm"),
        _check("contact_center_error_max",
               ctr_mm is not None and ctr_mm <= config["maximum_contact_center_error_m"] * 1000.0,
               ctr_mm, config["maximum_contact_center_error_m"] * 1000.0, "mm"),
        _check("cup_lateral_drift_max",
               drift_mm is not None and drift_mm <= config["maximum_cup_lateral_drift_m"] * 1000.0,
               drift_mm, config["maximum_cup_lateral_drift_m"] * 1000.0, "mm"),
        _check("cup_tilt_max", tilt_deg is not None and tilt_deg <= config["maximum_cup_tilt_deg"],
               tilt_deg, config["maximum_cup_tilt_deg"], "deg"),
    ]
    valid = all(c["pass"] for c in checks)
    # 비교 대상: lift_stage_pass(완료 여부 무관) > rigid_proxy_lift_pass(완료 and 유효)
    if "lift_stage_pass" in result:
        key, recomputed = "lift_stage_pass", valid
    else:
        key, recomputed = "rigid_proxy_lift_pass", bool(result.get("completed")) and valid
    stored = result.get(key)
    return {
        "compared_key": key,
        "stored": stored,
        "recomputed": recomputed,
        "agrees": stored is recomputed,
        "attempt": last_attempt,
        "checks": checks,
    }


def judge_cup(result_path: Path) -> dict:
    result = _load(result_path)
    plan_path = result_path.parent / "plan.json"
    plan = _load(plan_path) if plan_path.is_file() else {}
    if "task_pass" in result:
        key = "task_pass"
    elif "rigid_proxy_lift_pass" in result:
        key = "rigid_proxy_lift_pass"
    else:
        raise ValueError("task_pass·rigid_proxy_lift_pass가 모두 없는 결과입니다")
    stored = result[key]
    if not isinstance(stored, bool):
        raise ValueError(f"{key}가 bool이 아닙니다: {stored!r}")
    recovery = result.get("recovery_enabled", plan.get("recovery_enabled"))
    assembly_mode = result.get("assembly_mode", "left_contact_proxy")
    if assembly_mode != "left_contact_proxy":
        raise ValueError(f"알 수 없는 assembly_mode: {assembly_mode}")
    recheck = recheck_lift(result, plan.get("config") or {})
    event = None if stored else _cup_failure_event(result)
    checks = []
    if event is not None:
        checks.append({**_check("failure_event", False, event["code"], None, None), "t_s": event["t_s"]})
    checks.append(_check(f"stored_{key}", stored, stored, True, "bool"))
    if recheck:
        early_stop = event is not None and not result.get("completed") and recheck["checks"][0]["value"] == 0
        checks += _mark_not_measured(recheck["checks"]) if early_stop else recheck["checks"]
    return {
        "verdict": "pass" if stored else "fail",
        "judgement_basis": "oracle_recovery_sim_coordinates" if recovery is True else "rigid_proxy_lift",
        "assembly": "left_arm_proxy",
        "checks": checks,
        "failure_code": None if stored else (_cup_failure_code(result) or _first_failed(recheck) or f"{key}_false"),
        "stored_key": key,
        "recovery_enabled": recovery,
        "sample_recheck": (
            {k: v for k, v in recheck.items() if k != "checks"} if recheck else None
        ),
        "conditions": {
            "real_finray_grasp_verified": result.get("real_finray_grasp_verified"),
            "observation_source": result.get("observation_source", plan.get("observation_source")),
            "hardware_accessed": result.get("hardware_accessed"),
        },
    }


def judge_drive(result_path: Path, thresholds_path: Path) -> dict:
    result = _load(result_path)
    th = _load(thresholds_path)
    horizon = float(th["horizon_s"])
    rate_max = float(th["reversal_rate_max_per_s"])
    arrived = result.get("arrived")
    rate = result.get("reversal_rate_per_s")
    elapsed = result.get("elapsed_s")
    if not isinstance(arrived, bool) or not _finite([rate, elapsed]):
        raise ValueError("drive 결과에 arrived·reversal_rate_per_s·elapsed_s가 없습니다")
    if float(result.get("horizon_s", horizon)) != horizon:
        raise ValueError(f"결과 지평 {result.get('horizon_s')}이 고정 임계 {horizon}와 다릅니다")
    arrived_ok = arrived and float(elapsed) <= horizon
    rate_ok = float(rate) <= rate_max
    checks = [
        _check("arrived_within_horizon", arrived_ok, float(elapsed), horizon, "s"),
        _check("reversal_rate_last_window", rate_ok, float(rate), rate_max, "1/s"),
    ]
    failure = None
    if not arrived_ok:
        failure = "not_arrived_within_horizon"
    elif not rate_ok:
        failure = "reversal_rate_exceeded"
    return {
        "verdict": "pass" if arrived_ok and rate_ok else "fail",
        "judgement_basis": "kinematic_drive",
        "assembly": "bimanual_full",
        "checks": checks,
        "failure_code": failure,
        "verifier": th.get("verifier", "final_alignment_bounded"),
        "thresholds_sha256": hashlib.sha256(thresholds_path.read_bytes()).hexdigest(),
    }


def handle(verb: str, task: str, request: dict, workdir: Path) -> dict:
    result_path = Path(str(request["result_path"]))
    if not result_path.is_absolute():
        result_path = workdir / result_path
    if task == "cup_contact":
        return {"status": "ok", **judge_cup(result_path)}
    if task == "drive_kinematic":
        thresholds = Path(str(request.get("thresholds_path") or F1004_THRESHOLDS))
        return {"status": "ok", **judge_drive(result_path, thresholds)}
    raise ValueError(f"judge가 모르는 과제: {task}")
