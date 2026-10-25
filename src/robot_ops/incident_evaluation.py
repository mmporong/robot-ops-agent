"""사고 검토기의 라벨 고정 평가와 결정적 합성 challenge 생성.

라벨은 manifest 또는 생성기의 주입 일정에서만 읽는다. 검토기가 낸 구간을
정답으로 되읽지 않으며, 제시 구간 비율과 사람 비교 결과를 시간 절감이나
인과 효과로 해석하지 않는다.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import statistics
from pathlib import Path
from typing import Any, TypeGuard

from .replay.artifacts import sha256_file, write_json
from .replay.divergence import read_trajectory
from .replay.field_eval import input_identity

SCHEMA = "robot-ops-incident-evaluation/1"
RESULT_SCHEMA = "robot-ops-incident-evaluation-result/1"
CASE_KEYS = {
    "id",
    "group_id",
    "split",
    "trajectory_path",
    "trajectory_sha256",
    "expected_onset_s",
    "label_source",
    "label_reference",
}
TRIAL_KEYS = {"case_id", "reviewer", "method", "duration_s", "correct"}
SPLITS = {"development", "evaluation"}
LABEL_SOURCES = {"synthetic_injection", "independent_review"}
METHODS = {"manual", "assisted"}


def _default_parameters(function: Any) -> dict[str, Any]:
    return {
        name: parameter.default
        for name, parameter in inspect.signature(function).parameters.items()
        if parameter.default is not inspect.Parameter.empty
    }


def _protocol(
    manifest_path: Path,
    cases: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]],
    tolerance_s: float,
) -> dict[str, Any]:
    from . import incident_review, recording_inspection
    from .replay import recording_check

    sources = {
        "incident_review": Path(incident_review.__file__),
        "recording_check": Path(recording_check.__file__),
        "recording_inspection": Path(recording_inspection.__file__),
        "incident_evaluation": Path(__file__),
    }
    return {
        "schema": "robot-ops-incident-evaluation-protocol/1",
        "manifest_sha256": sha256_file(manifest_path),
        "source_sha256": {name: sha256_file(path) for name, path in sources.items()},
        "policy": incident_review.POLICY,
        "parameters": {
            "recording_check_defaults": _default_parameters(recording_check.inspect_recording),
            "maximum_windows": 3,
            "onset_tolerance_s": tolerance_s,
            "window_time_basis": "raw_start_end",
        },
        "cases": [
            {
                "id": case["id"],
                "split": case["split"],
                "trajectory_sha256": case["trajectory_sha256"],
                "expected_onset_s": case["expected_onset_s"],
                "label_source": case["label_source"],
                "label_reference": case["label_reference"],
            }
            for case, _, _ in cases
        ],
    }


def _finite_number(value: Any, *, positive: bool = False) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and (not positive or value > 0)
    )


def _exact_keys(value: Any, required: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where}: 객체여야 합니다")
    missing = required - value.keys()
    extra = value.keys() - required
    if missing:
        raise ValueError(f"{where}: 필수 키가 없습니다: {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"{where}: 허용되지 않은 키: {', '.join(sorted(extra))}")
    return value


def _required_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: 비어 있지 않은 문자열이어야 합니다")
    return value


def _resolve_trajectory(raw: str, manifest_path: Path) -> Path:
    expanded = Path(raw).expanduser()
    path = expanded if expanded.is_absolute() else manifest_path.parent / expanded
    if path.is_symlink() or not path.is_file():
        raise ValueError("평가 trajectory가 일반 파일이 아닙니다")
    return path


def _run_review(header: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    from .incident_review import review_rows

    return review_rows(header, rows)


def _validate_review(
    review: Any,
    case_id: str,
    *,
    trajectory_start_s: float,
    trajectory_end_s: float,
) -> tuple[list[dict[str, Any]], float]:
    if not isinstance(review, dict) or review.get("cause_status") != "undetermined":
        raise ValueError(f"검토 결과가 원인 불확정 계약을 지키지 않습니다: {case_id}")
    windows = review.get("windows")
    summary = review.get("summary")
    if not isinstance(windows, list) or not isinstance(summary, dict):
        raise ValueError(f"검토 결과의 windows·summary가 올바르지 않습니다: {case_id}")
    duration = summary.get("duration_s")
    if not _finite_number(duration) or duration < 0:
        raise ValueError(f"검토 결과 duration_s가 올바르지 않습니다: {case_id}")
    expected_duration = trajectory_end_s - trajectory_start_s
    if not math.isclose(float(duration), expected_duration, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError(f"검토 결과 duration_s가 trajectory 시간 범위와 다릅니다: {case_id}")
    checked: list[dict[str, Any]] = []
    for index, window in enumerate(windows):
        if not isinstance(window, dict):
            raise ValueError(f"검토 구간이 객체가 아닙니다: {case_id}[{index}]")
        start, end = window.get("start_s"), window.get("end_s")
        if (
            not _finite_number(start)
            or not _finite_number(end)
            or start < trajectory_start_s
            or end < start
            or end > trajectory_end_s
        ):
            raise ValueError(f"검토 구간의 원시 시간이 올바르지 않습니다: {case_id}[{index}]")
        checked.append(window)
    return checked, expected_duration


def _union_duration(
    windows: list[dict[str, Any]],
    trajectory_start_s: float,
    trajectory_end_s: float,
) -> float:
    intervals = sorted(
        (
            max(trajectory_start_s, float(item["start_s"])),
            min(trajectory_end_s, float(item["end_s"])),
        )
        for item in windows
        if float(item["start_s"]) <= trajectory_end_s
    )
    total = 0.0
    current_start: float | None = None
    current_end: float | None = None
    for start, end in intervals:
        if end < start:
            continue
        if current_start is None:
            current_start, current_end = start, end
        elif current_end is not None and start <= current_end:
            current_end = max(current_end, end)
        else:
            assert current_end is not None
            total += current_end - current_start
            current_start, current_end = start, end
    if current_start is not None and current_end is not None:
        total += current_end - current_start
    return total


def _aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    faults = [case for case in cases if case["expected_onset_s"] is not None]
    normal = [case for case in cases if case["expected_onset_s"] is None]
    onset_hits = sum(case["onset_hit"] for case in faults)
    normal_flagged = sum(case["normal_case_flagged"] for case in normal)
    return {
        "status": "measured" if cases else "not_measured",
        "case_count": len(cases),
        "fault_case_count": len(faults),
        "onset_hits": onset_hits,
        "onset_hit_rate": onset_hits / len(faults) if faults else None,
        "normal_case_count": len(normal),
        "normal_flagged": normal_flagged,
        "normal_flag_rate": normal_flagged / len(normal) if normal else None,
    }


def _human_effect(trials: list[dict[str, Any]]) -> dict[str, Any]:
    by_pair: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for trial in trials:
        by_pair.setdefault((trial["case_id"], trial["reviewer"]), {})[trial["method"]] = trial
    pairs = [methods for methods in by_pair.values() if set(methods) == METHODS]
    if not pairs:
        return {
            "status": "not_measured",
            "paired_count": 0,
            "reason": "같은 reviewer·case의 manual/assisted 쌍이 없습니다.",
            "claim": "not_measured",
        }
    deltas = [pair["manual"]["duration_s"] - pair["assisted"]["duration_s"] for pair in pairs]
    manual_durations = [pair["manual"]["duration_s"] for pair in pairs]
    assisted_durations = [pair["assisted"]["duration_s"] for pair in pairs]
    manual_correct = sum(pair["manual"]["correct"] for pair in pairs) / len(pairs)
    assisted_correct = sum(pair["assisted"]["correct"] for pair in pairs) / len(pairs)
    return {
        "status": "descriptive_paired_observation",
        "effect_status": "not_measured",
        "paired_count": len(pairs),
        "median_manual_duration_s": statistics.median(manual_durations),
        "median_assisted_duration_s": statistics.median(assisted_durations),
        "median_duration_delta_s": statistics.median(deltas),
        "manual_correctness": manual_correct,
        "assisted_correctness": assisted_correct,
        "claim": "association_only",
        "interpretation": "양수 시간 차이는 이 표본에서 manual 시간이 더 길었다는 관찰이며 인과 효과가 아닙니다.",
    }


def _load_manifest(manifest_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("평가 manifest는 객체여야 합니다")
    allowed = {"schema", "cases", "human_trials"}
    extra = document.keys() - allowed
    if extra:
        raise ValueError(f"manifest: 허용되지 않은 키: {', '.join(sorted(extra))}")
    if document.get("schema") != SCHEMA:
        raise ValueError(f"manifest.schema는 {SCHEMA}여야 합니다")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("manifest.cases는 비어 있지 않은 목록이어야 합니다")
    trials = document.get("human_trials", [])
    if not isinstance(trials, list):
        raise ValueError("manifest.human_trials는 목록이어야 합니다")
    return cases, trials


def evaluate_manifest(
    manifest_path: Path,
    out: Path,
    *,
    tolerance_s: float = 0.2,
) -> dict[str, Any]:
    """고정 라벨 manifest를 검토 결과와 대조하고 근거 JSON을 쓴다."""
    manifest_path = Path(manifest_path)
    out = Path(out)
    if not _finite_number(tolerance_s) or tolerance_s < 0:
        raise ValueError("tolerance_s는 0 이상의 유한한 수여야 합니다")
    raw_cases, raw_trials = _load_manifest(manifest_path)
    from .incident_report import validate_output_paths
    input_paths = [manifest_path] + [
        _resolve_trajectory(case["trajectory_path"], manifest_path) for case in raw_cases
        if isinstance(case, dict) and isinstance(case.get("trajectory_path"), str)
    ]
    validate_output_paths(out, input_paths, names=("protocol.json", "incident-evaluation.json"))

    ids: set[str] = set()
    group_splits: dict[str, set[str]] = {}
    digests: set[str] = set()
    identities: dict[str, str] = {}
    loaded: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    for index, raw in enumerate(raw_cases):
        case = _exact_keys(raw, CASE_KEYS, f"cases[{index}]")
        case_id = _required_text(case["id"], f"cases[{index}].id")
        group_id = _required_text(case["group_id"], f"cases[{index}].group_id")
        if case_id in ids:
            raise ValueError(f"중복 사례 ID: {case_id}")
        ids.add(case_id)
        if case["split"] not in SPLITS:
            raise ValueError(f"split이 올바르지 않습니다: {case_id}")
        group_splits.setdefault(group_id, set()).add(case["split"])
        if case["label_source"] not in LABEL_SOURCES:
            raise ValueError(f"label_source가 올바르지 않습니다: {case_id}")
        _required_text(case["label_reference"], f"cases[{index}].label_reference")
        onset = case["expected_onset_s"]
        if onset is not None and (not _finite_number(onset) or onset < 0):
            raise ValueError(f"expected_onset_s가 올바르지 않습니다: {case_id}")
        digest = case["trajectory_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"trajectory_sha256이 올바르지 않습니다: {case_id}")
        path = _resolve_trajectory(_required_text(case["trajectory_path"], "trajectory_path"), manifest_path)
        if sha256_file(path) != digest:
            raise ValueError(f"평가 trajectory 해시 불일치: {case_id}")
        if digest in digests:
            raise ValueError(f"중복 사례: 동일한 trajectory 파일 내용: {case_id}")
        digests.add(digest)
        header, rows = read_trajectory(path)
        if not rows:
            raise ValueError(f"평가 trajectory 행이 비어 있습니다: {case_id}")
        if onset is not None and not float(rows[0]["t"]) <= float(onset) <= float(rows[-1]["t"]):
            raise ValueError(f"expected_onset_s가 trajectory 시간 범위 밖입니다: {case_id}")
        identity = input_identity(header, rows)
        if identity in identities:
            raise ValueError(
                f"중복 사례: 동일한 trajectory identity: {identities[identity]} / {case_id}"
            )
        identities[identity] = case_id
        loaded.append((case, header, rows))
    leaking = sorted(group for group, splits in group_splits.items() if len(splits) > 1)
    if leaking:
        raise ValueError(f"group development/evaluation 누수: {', '.join(leaking)}")

    trials: list[dict[str, Any]] = []
    trial_keys: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(raw_trials):
        trial = _exact_keys(raw, TRIAL_KEYS, f"human_trials[{index}]")
        case_id = _required_text(trial["case_id"], "human_trials.case_id")
        reviewer = _required_text(trial["reviewer"], "human_trials.reviewer")
        if case_id not in ids:
            raise ValueError(f"human trial이 알 수 없는 case를 참조합니다: {case_id}")
        if trial["method"] not in METHODS:
            raise ValueError(f"human trial method가 올바르지 않습니다: {case_id}")
        if not _finite_number(trial["duration_s"], positive=True):
            raise ValueError(f"human trial duration_s는 양수여야 합니다: {case_id}")
        if not isinstance(trial["correct"], bool):
            raise ValueError(f"human trial correct는 bool이어야 합니다: {case_id}")
        key = (case_id, reviewer, trial["method"])
        if key in trial_keys:
            raise ValueError(f"중복 human trial: {case_id}/{reviewer}/{trial['method']}")
        trial_keys.add(key)
        trials.append(trial)

    out.mkdir(parents=True, exist_ok=True)
    protocol = _protocol(manifest_path, loaded, float(tolerance_s))
    protocol_path = out / "protocol.json"
    write_json(protocol_path, protocol)
    protocol_sha256 = sha256_file(protocol_path)

    results: list[dict[str, Any]] = []
    total_duration = 0.0
    total_selected = 0.0
    for case, header, rows in loaded:
        review = _run_review(header, rows)
        trajectory_start_s = float(rows[0]["t"])
        trajectory_end_s = float(rows[-1]["t"])
        windows, duration = _validate_review(
            review,
            case["id"],
            trajectory_start_s=trajectory_start_s,
            trajectory_end_s=trajectory_end_s,
        )
        top_windows = windows[:3]
        selected_duration = _union_duration(
            top_windows,
            trajectory_start_s,
            trajectory_end_s,
        )
        total_duration += duration
        total_selected += selected_duration
        item: dict[str, Any] = {
            "id": case["id"],
            "group_id": case["group_id"],
            "split": case["split"],
            "label_source": case["label_source"],
            "label_reference": case["label_reference"],
            "trajectory_sha256": case["trajectory_sha256"],
            "expected_onset_s": case["expected_onset_s"],
            "review_status": review.get("status"),
            "cause_status": "undetermined",
            "window_count": len(windows),
            "evaluated_window_count": len(top_windows),
            "selected_duration_s": selected_duration,
            "duration_s": duration,
            "selected_time_ratio": selected_duration / duration if duration > 0 else None,
        }
        onset = case["expected_onset_s"]
        if onset is None:
            item["normal_case_flagged"] = bool(top_windows)
        else:
            item["onset_hit"] = any(
                float(window["start_s"]) <= float(onset) + tolerance_s
                and float(window["end_s"]) >= float(onset) - tolerance_s
                for window in top_windows
            )
        results.append(item)

    by_split = {
        split: _aggregate([case for case in results if case["split"] == split])
        for split in sorted(SPLITS)
    }
    evaluation_cases = [case for case in results if case["split"] == "evaluation"]
    independent = [case for case in evaluation_cases if case["label_source"] == "independent_review"]
    # 독립 검토 라벨의 존재만으로 현장 성능이 검증되지는 않는다. 표본 선정,
    # 사전 고정 protocol, 대표성까지 별도로 입증되기 전에는 항상 보류한다.
    field_validated = False
    human_effect = _human_effect(trials)
    result = {
        "schema": RESULT_SCHEMA,
        "status": "measured" if evaluation_cases else "insufficient_evaluation_cases",
        "cause_status": "undetermined",
        "scope": {
            "valid_case_count": len(results),
            "development_case_count": len(results) - len(evaluation_cases),
            "evaluation_case_count": len(evaluation_cases),
            "evaluation_case_ids": [case["id"] for case in evaluation_cases],
            "synthetic_evaluation_count": sum(
                case["label_source"] == "synthetic_injection" for case in evaluation_cases
            ),
            "independent_evaluation_count": len(independent),
            "evaluation_interpretation": "evaluation split만 평가 범위이며 development 결과는 개발 회귀입니다.",
        },
        "parameters": {"tolerance_s": float(tolerance_s), "maximum_windows": 3},
        "reproducibility": {
            **protocol,
            "protocol_sha256": protocol_sha256,
        },
        "field_validated": field_validated,
        "field_validation": {
            "status": "not_measured",
            "independent_evaluation_count": len(independent),
            "reason": "독립 검토 라벨 수만으로 전향적 현장 성능을 입증할 수 없습니다.",
        },
        "cases": results,
        "aggregate": by_split,
        "presentation": {
            "selected_duration_s": total_selected,
            "recording_duration_s": total_duration,
            "selected_time_ratio": total_selected / total_duration if total_duration > 0 else None,
            "claim": "presented_interval_ratio_only",
            "interpretation": "제시 구간 비율이며 사람의 검토 시간 절감을 측정한 값이 아닙니다.",
        },
        "human_effect": human_effect,
        "claims": {
            "human_time_savings": "not_measured",
            "causal_efficacy": "not_claimed",
            "field_holdout_performance": "not_measured" if not field_validated else "independent_review_subset",
        },
    }
    write_json(out / "incident-evaluation.json", result)
    return result


def _challenge_rows(case_id: str, *, fps: int = 10, frames: int = 101) -> list[dict[str, Any]]:
    phase = int(hashlib.sha256(case_id.encode()).hexdigest()[:4], 16) / 65535 * 0.01
    rows = []
    for index in range(frames):
        angle = phase + math.radians(index * 0.8)
        rows.append({
            "t": index / fps,
            "q_cmd": [angle, angle / 2],
            "q_obs": [angle, angle / 2],
            "source_sample_timestamp_s": index / fps,
            "source_sequence": index,
        })
    return rows


def _freeze(rows: list[dict[str, Any]], start: int, end: int, joints: tuple[int, ...]) -> None:
    for index in range(start, end + 1):
        for joint in joints:
            rows[index]["q_obs"][joint] = rows[start - 1]["q_obs"][joint]


def build_challenges(out: Path) -> Path:
    """검출 결과를 참조하지 않는 결정적 합성 challenge manifest를 만든다."""
    out = Path(out)
    trajectories = out / "trajectories"
    trajectories.mkdir(parents=True, exist_ok=True)
    specs = [
        ("normal_motion", "development", None, "normal_motion"),
        ("normal_stationary", "development", None, "normal_stationary"),
        ("normal_quantized", "evaluation", None, "normal_quantized"),
        ("recording_freeze", "development", 4.0, "recording_freeze"),
        ("partial_joint_freeze", "evaluation", 4.0, "partial_joint_freeze"),
        ("partial_metadata", "evaluation", 4.0, "partial_metadata"),
        ("identical_sequence_cause_unknown", "evaluation", 4.0, "cause_unknown"),
        ("time_gap", "evaluation", 5.5, "time_gap"),
        ("source_sequence_stall", "evaluation", 4.0, "source_sequence_stall"),
    ]
    cases = []
    for case_id, split, onset, kind in specs:
        rows = _challenge_rows(case_id)
        schedule: list[dict[str, Any]] = []
        if kind == "normal_stationary":
            for row in rows:
                row["q_cmd"] = [0.0, 0.0]
                row["q_obs"] = [0.0, 0.0]
        elif kind == "normal_quantized":
            for row in rows:
                row["q_obs"] = [math.radians(round(math.degrees(value))) for value in row["q_obs"]]
        elif kind in {"recording_freeze", "partial_metadata", "cause_unknown"}:
            _freeze(rows, 40, 65, (0, 1))
            schedule = [{"kind": kind, "start_s": 4.0, "end_s": 6.5}]
        elif kind == "partial_joint_freeze":
            _freeze(rows, 40, 65, (0,))
            schedule = [{"kind": kind, "start_s": 4.0, "end_s": 6.5}]
        elif kind == "time_gap":
            for index in range(50, len(rows)):
                rows[index]["t"] += 0.5
            schedule = [{"kind": kind, "start_s": 5.5, "end_s": 5.5}]
        elif kind == "source_sequence_stall":
            for index in range(40, 66):
                rows[index]["source_sequence"] = 39
                rows[index]["source_sample_timestamp_s"] = 3.9
            schedule = [{"kind": kind, "start_s": 4.0, "end_s": 6.5}]
        if kind == "partial_metadata":
            for index, row in enumerate(rows):
                if index % 2:
                    row.pop("source_sequence", None)
                    row.pop("source_sample_timestamp_s", None)

        header: dict[str, Any] = {
            "schema": "robot-ops-trajectory/1",
            "robot_id": "synthetic_so101",
            "arm": "left",
            "joint_names": ["shoulder_pan", "shoulder_lift"],
            "units": "rad",
            "fps": 10,
            "source": {
                "kind": "synthetic",
                "path": f"generated/incident-challenges/{case_id}.jsonl",
                "episode": 0,
                "sha256": hashlib.sha256(case_id.encode()).hexdigest(),
            },
            "origin": "fault_injection",
            "injection_schedule": schedule,
            "cause_status": "undetermined" if kind == "cause_unknown" else "synthetic_fixture",
        }
        if kind == "normal_quantized":
            header["recording_check"] = {
                "observation_resolution_deg": {"shoulder_pan": 1.0, "shoulder_lift": 1.0}
            }
        path = trajectories / f"{case_id}.jsonl"
        payload = "\n".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in [header, *rows]
        ) + "\n"
        path.write_text(payload, encoding="utf-8")
        expected = schedule[0]["start_s"] if schedule else None
        cases.append({
            "id": case_id,
            "group_id": case_id,
            "split": split,
            "trajectory_path": str(path.resolve()),
            "trajectory_sha256": sha256_file(path),
            "expected_onset_s": expected,
            "label_source": "synthetic_injection",
            "label_reference": f"injection_schedule:{case_id}:v1",
        })
    manifest = {"schema": SCHEMA, "cases": cases}
    manifest_path = out / "manifest.json"
    write_json(manifest_path, manifest)
    return manifest_path
