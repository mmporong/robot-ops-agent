"""관절별 시간 기반 검사기의 개발 회귀 비교. CPU·표준 라이브러리 전용."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from robot_ops.recording_inspection import check_rows

from .artifacts import sha256_file, write_json
from .divergence import detect_freezes, read_trajectory
from .field_eval import PARAMETERS as V1_PARAMETERS
from .field_eval import build_challenges, input_identity, score_events
from .profile import display_path, expand_path

SCHEMA = "robot-ops-recording-check-evaluation/1"


def _frames_to_intervals(frames: set[int]) -> list[dict[str, int]]:
    """프레임 합집합을 겹치지 않는 연속 구간으로 바꾼다."""
    if not frames:
        return []
    ordered = sorted(frames)
    intervals: list[dict[str, int]] = []
    start = previous = ordered[0]
    for frame in ordered[1:]:
        if frame != previous + 1:
            intervals.append({"frame_start": start, "frame_end": previous})
            start = frame
        previous = frame
    intervals.append({"frame_start": start, "frame_end": previous})
    return intervals


def union_joint_issues(issues: list[dict[str, Any]]) -> list[dict[str, int]]:
    """관절별 중복 경고를 프레임 합집합으로 통합한다."""
    frames: set[int] = set()
    for issue in issues:
        if issue.get("kind") != "joint_observation_stagnation":
            continue
        start, end = issue.get("frame_start"), issue.get("frame_end")
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
            raise ValueError("v2 경고 구간이 올바르지 않습니다")
        frames.update(range(start, end + 1))
    return _frames_to_intervals(frames)


def _v2_predictions(header: dict, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    issues = check_rows(header, rows)["issues"]
    joint_issues = [issue for issue in issues if issue.get("kind") == "joint_observation_stagnation"]
    return union_joint_issues(joint_issues), joint_issues


def _score(expected: list[tuple[int, int]], predicted: list[dict], n_frames: int) -> dict:
    """기존 점수 계약을 쓰되 v1 전용 확인 지연 계산은 제거한다."""
    score = score_events(expected, predicted, n_frames)
    for match in score["event_matching"]:
        match.pop("minimum_confirmation_delay_frames", None)
    score["boundary_limit"] = {
        "false_positive_frames": score["frame_fp"],
        "start_end_errors_are_reported_in_frames": True,
        "note_ko": "경고 구간은 정체 직전 기준 프레임을 포함할 수 있어 경계에서 여분 프레임이 생길 수 있습니다.",
    }
    return score


def _quantized_case(*, known_resolution: bool) -> tuple[dict, list[dict]]:
    fps = 10
    rows = []
    for index in range(80):
        command_deg = index * 0.2
        observed_deg = round(command_deg)
        rows.append({
            "t": index / fps,
            "q_cmd": [math.radians(command_deg), 0.0, 0.0, 0.0, 0.0],
            "q_obs": [math.radians(observed_deg), 0.0, 0.0, 0.0, 0.0],
            "gripper_cmd": 0.0,
            "gripper_obs": 0.0,
        })
    header = {
        "schema": "robot-ops-trajectory/1",
        "units": "rad",
        "fps": fps,
        "robot_id": "synthetic_so101",
        "arm": "left",
        "origin": "quantization_probe",
        "source": {"kind": "synthetic", "path": "recording_check_evaluation", "episode": 0,
                   "sha256": "0" * 64},
        "joint_names": ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"],
    }
    if known_resolution:
        header["recording_check"] = {"observation_resolution_deg": {"shoulder_pan": 1.0}}
    return header, rows


def _load_corpus(corpus_path: Path) -> tuple[dict, list[tuple[dict, dict, list[dict]]]]:
    manifest = json.loads(corpus_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "robot-ops-field-corpus/1" or manifest.get("errors"):
        raise ValueError("오류 없는 평가용 corpus manifest가 필요합니다")
    episodes = manifest.get("episodes")
    if not isinstance(episodes, list):
        raise ValueError("corpus episodes가 목록이 아닙니다")
    by_id: dict[str, dict] = {}
    loaded_by_id: dict[str, tuple[dict, list[dict], str]] = {}
    for entry in episodes:
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in by_id:
            raise ValueError("평가 입력 ID가 비었거나 중복되었습니다")
        path = expand_path(entry["trajectory_path"], corpus_path.parent)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"평가 입력이 일반 파일이 아닙니다: {identifier}")
        if sha256_file(path) != entry.get("trajectory_sha256"):
            raise ValueError(f"평가 입력 해시 불일치: {identifier}")
        header, rows = read_trajectory(path)
        identity = input_identity(header, rows)
        if identity != entry.get("identity_sha256"):
            raise ValueError(f"평가 입력 identity 불일치: {identifier}")
        if len(rows) != entry.get("frame_count"):
            raise ValueError(f"평가 입력 길이 불일치: {identifier}")
        by_id[identifier] = entry
        loaded_by_id[identifier] = (header, rows, identity)

    unique: list[tuple[dict, dict, list[dict]]] = []
    identity_owner: dict[str, str] = {}
    for entry in episodes:
        identifier = entry["id"]
        header, rows, identity = loaded_by_id[identifier]
        duplicate_of = entry.get("duplicate_of")
        if duplicate_of:
            target = by_id.get(duplicate_of)
            if (target is None or target.get("duplicate_of") or
                    loaded_by_id[duplicate_of][2] != identity or
                    loaded_by_id[duplicate_of][0].get("recording_check") != header.get("recording_check")):
                raise ValueError(f"중복 대상 identity 불일치: {identifier}")
            continue
        if identity in identity_owner:
            raise ValueError(f"중복 표시가 없는 동일 입력: {identifier}")
        identity_owner[identity] = identifier
        unique.append((entry, header, rows))
    return manifest, unique


def _retrospective(corpus_path: Path) -> dict[str, Any]:
    manifest, loaded = _load_corpus(corpus_path)
    records = []
    totals = {
        "v1": {"episodes_with_alert": 0, "union_intervals": 0, "per_joint_issues": None},
        "v2": {"episodes_with_alert": 0, "union_intervals": 0, "per_joint_issues": 0},
    }
    for entry, header, rows in loaded:
        v1 = detect_freezes(rows, **V1_PARAMETERS)
        v2, joint_issues = _v2_predictions(header, rows)
        totals["v1"]["episodes_with_alert"] += bool(v1)
        totals["v1"]["union_intervals"] += len(v1)
        totals["v2"]["episodes_with_alert"] += bool(v2)
        totals["v2"]["union_intervals"] += len(v2)
        totals["v2"]["per_joint_issues"] += len(joint_issues)
        records.append({
            "id": entry["id"],
            "label": "unknown",
            "frames": len(rows),
            "identity_sha256": entry["identity_sha256"],
            "alerts": {"v1_intervals": len(v1), "v2_union_intervals": len(v2),
                       "v2_per_joint_issues": len(joint_issues)},
        })
    return {
        "corpus_sha256": sha256_file(corpus_path),
        "dataset_count": manifest.get("dataset_count"),
        "raw_episodes": len(manifest["episodes"]),
        "unique_episodes": len(loaded),
        "duplicate_episodes": len(manifest["episodes"]) - len(loaded),
        "unique_frames": sum(record["frames"] for record in records),
        "labels_available": False,
        "accuracy_computed": False,
        "alert_counts": totals,
        "records": records,
    }


def run(out: Path, *, corpus_path: Path | None = None) -> dict[str, Any]:
    cases = [case for case in build_challenges() if case["scope"] != "cause_ambiguity"]
    synthetic = []
    for case in cases:
        v1 = detect_freezes(case["rows"], **V1_PARAMETERS)
        v2, joint_issues = _v2_predictions(case["header"], case["rows"])
        synthetic.append({
            **{key: case[key] for key in ("id", "kind", "scope", "expected", "cause")},
            "frame_count": len(case["rows"]),
            "metrics": {
                "v1": _score(case["expected"], v1, len(case["rows"])),
                "v2": _score(case["expected"], v2, len(case["rows"])),
            },
            "predictions": {"v1": v1, "v2_union": v2, "v2_per_joint": joint_issues},
        })

    aggregate = {}
    for detector in ("v1", "v2"):
        aggregate[detector] = {
            "event_hits": sum(case["metrics"][detector]["event_hits"] for case in synthetic),
            "event_total": sum(case["metrics"][detector]["event_expected"] for case in synthetic),
            "false_positive_frames": sum(case["metrics"][detector]["frame_fp"] for case in synthetic),
            "normal_cases_with_alert": sum(
                case["kind"] == "normal" and bool(case["predictions"]["v2_union" if detector == "v2" else "v1"])
                for case in synthetic
            ),
        }

    quantization = []
    for known in (False, True):
        header, rows = _quantized_case(known_resolution=known)
        predictions, joint_issues = _v2_predictions(header, rows)
        quantization.append({
            "id": "normal_coarse_quantization_known" if known else "normal_coarse_quantization_unknown",
            "normal_case": True,
            "observation_resolution_deg": 1.0 if known else None,
            "v2_union_intervals": predictions,
            "v2_per_joint_issue_count": len(joint_issues),
            "warning": bool(predictions),
        })

    sample_check = check_rows(cases[0]["header"], cases[0]["rows"])
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "scope": "known_development_regression",
        "deployment_verdict": "not_validated",
        "field_validated": False,
        "protocol": {
            "challenge_source": "field_eval.build_challenges",
            "challenge_count": len(cases),
            "challenge_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
            "v1_detector_sha256": sha256_file(Path(__file__).with_name("divergence.py")),
            "v2_detector_sha256": sha256_file(Path(__file__).with_name("recording_check.py")),
            "inspection_sha256": sha256_file(Path(__file__).parents[1] / "recording_inspection.py"),
            "evaluator_sha256": sha256_file(Path(__file__)),
            "v1_parameters": V1_PARAMETERS,
            "v2_parameters": sample_check["parameters"],
            "v2_parameter_basis": "proposed_default",
            "per_joint_overlap_policy": "frame_set_union_then_contiguous_intervals",
        },
        "aggregate": aggregate,
        "synthetic": synthetic,
        "quantization_probes": quantization,
        "intentional_below_minimum": {
            "case_id": "fault_two_frame_hold",
            "expected_behavior": "miss",
            "reason_ko": "0.3초 최소 근거 시간보다 짧아 의도적으로 경고하지 않는 구간입니다.",
        },
        "cause_status": "undetermined",
        "retrospective": _retrospective(corpus_path) if corpus_path else None,
        "unmeasured": ["real_world_precision_recall", "prospective_holdout_performance",
                       "root_cause_accuracy", "human_review_time_saved"],
        "limitations": [
            "17개 통제 사례는 검출기 개발에 사용한 회귀 자료이며 독립 holdout이나 실제 정확도가 아닙니다.",
            "실제 기록에는 정답 라벨이 없어 경고 건수만 집계하며 정확도를 계산하지 않습니다.",
            "센서 해상도를 모르면 정상적인 계단형 양자화도 정체 후보로 경고할 수 있습니다.",
            "경고는 증상 구간이며 기록 갱신 정지와 물리적 걸림의 원인을 구분하지 않습니다.",
        ],
    }
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "result.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="v1·v2 저장 기록 검사기의 개발 회귀 비교(CPU 전용)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--corpus", type=Path)
    args = parser.parse_args()
    result = run(args.out.expanduser().resolve(),
                 corpus_path=args.corpus.expanduser().resolve() if args.corpus else None)
    print(json.dumps({
        "result": display_path((args.out.expanduser().resolve() / "result.json")),
        "aggregate": result["aggregate"],
        "retrospective_alert_counts": (result["retrospective"] or {}).get("alert_counts"),
        "field_validated": result["field_validated"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
