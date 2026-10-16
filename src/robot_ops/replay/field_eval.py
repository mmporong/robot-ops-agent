"""저장 기록의 회고적 경고 감사와 정답이 있는 통제 시험. CPU·표준 라이브러리 전용."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import random
import time
from pathlib import Path
from typing import Any

from .artifacts import sha256_file, write_json
from .divergence import detect_freezes, read_trajectory
from .profile import display_path, expand_path
from .schema import validate_trajectory_header, validate_trajectory_row

SCHEMA = "robot-ops-field-evaluation/1"
SEED = 20261016
MATCH_IOU = 0.1
PARAMETERS = {"eps_deg": 0.1, "delta_deg": 0.5, "min_frames": 3}


def input_identity(header: dict, rows: list[dict]) -> str:
    digest = hashlib.sha256()
    keys = ("schema", "robot_id", "arm", "joint_names", "units", "units_in", "gripper_units", "fps")
    for value in [{k: header.get(k) for k in keys}, *rows]:
        digest.update((json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode())
    return digest.hexdigest()


def repeat_baseline(rows: list[dict], min_frames: int = 3) -> list[dict]:
    """명령과 무관하게 관측이 완전히 반복되는 연속 전환을 찾는 단순 기준선."""
    result = []
    start = None
    for i in range(1, len(rows) + 1):
        same = i < len(rows) and all(rows[i].get(k) == rows[i - 1].get(k) for k in ("q_obs", "gripper_obs"))
        if same and start is None:
            start = i
        if not same and start is not None:
            if i - start >= min_frames:
                result.append({"frame_start": start, "frame_end": i - 1, "n_frames": i - start,
                               "t_start_s": rows[start]["t"], "t_end_s": rows[i - 1]["t"]})
            start = None
    return result


def _frames(intervals: list[tuple[int, int]]) -> set[int]:
    return {i for a, b in intervals for i in range(a, b + 1)}


def score_events(expected: list[tuple[int, int]], predicted: list[dict], n_frames: int) -> dict:
    """완전 라벨된 합성 시계열만 점수화한다. 프레임 0은 차분이 없어 제외한다."""
    actual = [(p["frame_start"], p["frame_end"]) for p in predicted]
    universe = set(range(1, n_frames))
    if any(a < 1 or b < a or b >= n_frames for a, b in [*expected, *actual]):
        raise ValueError("평가 구간은 프레임 1..N-1 안이어야 합니다")
    truth, flags = _frames(expected), _frames(actual)
    tp, fp, fn = len(truth & flags), len(flags - truth), len(truth - flags)
    candidates = []
    for ei, (ea, eb) in enumerate(expected):
        for pi, (pa, pb) in enumerate(actual):
            overlap = max(0, min(eb, pb) - max(ea, pa) + 1)
            iou = overlap / ((eb - ea + 1) + (pb - pa + 1) - overlap)
            if iou >= MATCH_IOU:
                candidates.append((iou, ei, pi))
    neighbors = {ei: sorted(((-iou, pi) for iou, e, pi in candidates if e == ei)) for ei in range(len(expected))}
    owners = {}

    def assign(ei: int, seen: set[int]) -> bool:
        for _, pi in neighbors[ei]:
            if pi in seen:
                continue
            seen.add(pi)
            if pi not in owners or assign(owners[pi], seen):
                owners[pi] = ei
                return True
        return False

    for ei in range(len(expected)):
        assign(ei, set())
    matches = []
    for pi, ei in sorted(owners.items(), key=lambda item: (item[1], item[0])):
        iou = next(value for value, e, p in candidates if e == ei and p == pi)
        ea, eb = expected[ei]
        pa, pb = actual[pi]
        matches.append({"expected_index": ei, "predicted_index": pi, "iou": iou,
                        "start_error_frames": pa - ea, "end_error_frames": pb - eb,
                        "minimum_confirmation_delay_frames": pa + PARAMETERS["min_frames"] - 1 - ea})
    return {"frame_tp": tp, "frame_fp": fp, "frame_fn": fn,
            "frame_tn": len(universe - truth - flags),
            "frame_precision": tp / (tp + fp) if tp + fp else None,
            "frame_recall": tp / (tp + fn) if tp + fn else None,
            "event_expected": len(expected), "event_predicted": len(actual),
            "event_hits": len(matches), "event_missed": len(expected) - len(matches),
            "event_matching": matches}


def _signal(*, fps: int = 10, speed_deg_s: float = 8.0, n: int = 120) -> list[dict]:
    return [{"t": i / fps, "q_cmd": [math.radians(i * speed_deg_s / fps), 0.0, 0.0, 0.0, 0.0],
             "q_obs": [math.radians(i * speed_deg_s / fps), 0.0, 0.0, 0.0, 0.0],
             "gripper_cmd": 0.0, "gripper_obs": 0.0} for i in range(n)]


def _hold(rows: list[dict], start: int, end: int, joints: tuple[int, ...] = (0, 1, 2, 3, 4)) -> None:
    for i in range(start, end + 1):
        for j in joints:
            rows[i]["q_obs"][j] = rows[start - 1]["q_obs"][j]


def build_challenges() -> list[dict]:
    """검출 결과와 무관하게 고정한 신호·주입 구간을 정답으로 사용한다."""
    cases = []

    def add(name: str, rows: list[dict], expected=(), *, kind="normal", scope="in_contract", fps=10, cause=None):
        cases.append({"id": name, "kind": kind, "scope": scope, "cause": cause,
                      "header": {"schema": "robot-ops-trajectory/1", "units": "rad", "fps": fps,
                                 "robot_id": "synthetic_so101", "arm": "left", "origin": "fault_injection",
                                 "source": {"kind": "synthetic", "path": "src/robot_ops/replay/field_eval.py",
                                            "episode": 0, "sha256": sha256_file(Path(__file__))},
                                 "joint_names": ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]},
                      "rows": rows, "expected": list(expected)})

    add("normal_fast_tracking", _signal())
    add("normal_slow_tracking", _signal(speed_deg_s=2.0))
    add("normal_stationary", _signal(speed_deg_s=0.0))
    pause = _signal()
    for i in range(30, 60):
        pause[i]["q_cmd"] = list(pause[29]["q_cmd"])
        pause[i]["q_obs"] = list(pause[29]["q_obs"])
    add("normal_pause_resume", pause)
    quantized = _signal(speed_deg_s=0.4)
    for r in quantized:
        r["q_obs"][0] = math.radians(round(math.degrees(r["q_obs"][0]) / 0.5) * 0.5)
    add("normal_quantized_slow", quantized)
    grip = _signal(speed_deg_s=0.0)
    for i, r in enumerate(grip):
        r["gripper_cmd"] = r["gripper_obs"] = float(i % 80)
    add("normal_gripper_only", grip)
    noisy = _signal(speed_deg_s=0.0)
    rng = random.Random(SEED)
    for r in noisy:
        r["q_obs"][0] = math.radians(rng.uniform(-0.02, 0.02))
    add("normal_stationary_sensor_noise", noisy)

    for name, speed, fps, scope in (
        ("fault_fast_full_hold", 8.0, 10, "in_contract"),
        ("fault_slow_full_hold", 2.0, 10, "coverage_extension"),
        ("fault_below_command_threshold", 4.9, 10, "coverage_extension"),
        ("fault_above_command_threshold", 5.1, 10, "in_contract"),
        ("fault_high_fps_full_hold", 8.0, 100, "coverage_extension"),
    ):
        rows = _signal(fps=fps, speed_deg_s=speed)
        _hold(rows, 20, 49)
        add(name, rows, [(20, 49)], kind="injected", scope=scope, fps=fps, cause="stale_payload")
    two = _signal()
    _hold(two, 20, 39)
    _hold(two, 60, 79)
    add("fault_two_separate_holds", two, [(20, 39), (60, 79)], kind="injected", cause="stale_payload")
    partial = _signal()
    for i, r in enumerate(partial):
        r["q_cmd"][1] = r["q_obs"][1] = math.radians(i * 0.3)
    _hold(partial, 20, 49, (0,))
    add("fault_partial_joint_hold", partial, [(20, 49)], kind="injected", scope="coverage_extension", cause="stale_payload")
    live_grip = _signal()
    _hold(live_grip, 20, 49)
    for i, r in enumerate(live_grip):
        r["gripper_cmd"] = r["gripper_obs"] = float(i % 80)
    add("fault_arm_hold_gripper_live", live_grip, [(20, 49)], kind="injected", scope="coverage_extension", cause="stale_payload")
    intermittent = _signal()
    angle = 0.0
    for i, r in enumerate(intermittent):
        angle += 0.8 if i % 3 else 0.0
        r["q_cmd"][0] = r["q_obs"][0] = math.radians(angle)
    _hold(intermittent, 20, 59)
    add("fault_intermittent_command", intermittent, [(20, 59)], kind="injected", scope="coverage_extension", cause="stale_payload")
    short = _signal()
    _hold(short, 20, 21)
    add("fault_two_frame_hold", short, [(20, 21)], kind="injected", scope="below_minimum", cause="stale_payload")
    pair = _signal()
    _hold(pair, 20, 49)
    add("ambiguous_stale", copy.deepcopy(pair), [(20, 49)], kind="ambiguity", scope="cause_ambiguity", cause="stale_payload")
    add("ambiguous_physical_block", pair, [(20, 49)], kind="ambiguity", scope="cause_ambiguity", cause="fresh_sensor_physical_block")
    return cases


def _run_methods(rows: list[dict]) -> dict[str, list[dict]]:
    return {"current": detect_freezes(rows, **PARAMETERS), "repeat_baseline": repeat_baseline(rows)}


def run(corpus_path: Path, out: Path) -> dict[str, Any]:
    manifest = json.loads(corpus_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "robot-ops-field-corpus/1" or manifest.get("errors"):
        raise ValueError("평가용 corpus가 없거나 읽기 오류가 있습니다")
    out.mkdir(parents=True, exist_ok=True)
    cases = build_challenges()
    for case in cases:
        validate_trajectory_header(case["header"])
        for i, row in enumerate(case["rows"]):
            validate_trajectory_row(row, len(case["header"]["joint_names"]), i)
    protocol = {"schema": "robot-ops-field-protocol/1", "detector_sha256": sha256_file(Path(__file__).with_name("divergence.py")),
                "evaluator_sha256": sha256_file(Path(__file__)), "corpus_sha256": sha256_file(corpus_path),
                "parameters": PARAMETERS, "seed": SEED, "event_iou_min": MATCH_IOU,
                "event_matching": "maximum_cardinality_iou_ordered_neighbors",
                "case_specs": [{k: c[k] for k in ("id", "kind", "scope", "expected", "cause")} for c in cases],
                "synthetic_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest()}
    # 실행 전에 입력과 설정을 고정한다. 이 실행에서는 검출기를 튜닝하지 않는다.
    write_json(out / "protocol.json", protocol)
    retrospective = []
    first_started = time.perf_counter()
    loaded = []
    identities, entries_by_id = {}, {}
    for entry in manifest["episodes"]:
        path = expand_path(entry["trajectory_path"], corpus_path.parent)
        if sha256_file(path) != entry["trajectory_sha256"]:
            raise ValueError(f"평가 입력 해시 불일치: {entry['id']}")
        header, rows = read_trajectory(path)
        identity = input_identity(header, rows)
        if identity != entry["identity_sha256"] or entry["id"] in entries_by_id:
            raise ValueError(f"평가 입력 identity·ID 불일치: {entry['id']}")
        entries_by_id[entry["id"]] = entry
        if entry.get("duplicate_of"):
            target = entries_by_id.get(entry["duplicate_of"])
            if not target or target is entry or target.get("duplicate_of") or target["identity_sha256"] != identity:
                raise ValueError(f"중복 대상 identity 불일치: {entry['id']}")
            continue
        if identity in identities:
            raise ValueError(f"중복 표시가 없는 동일 입력: {entry['id']}")
        identities[identity] = entry["id"]
        if len(rows) != entry["frame_count"] or any(rows[i]["t"] <= rows[i - 1]["t"] for i in range(1, len(rows))):
            raise ValueError(f"평가 입력 길이·시각 불일치: {entry['id']}")
        loaded.append((entry, header, rows))
        predictions = _run_methods(rows)
        retrospective.append({"id": entry["id"], "dataset": entry["dataset"], "episode": entry["episode"],
                              "frames": len(rows), "duration_s": entry["duration_s"],
                              "identity_sha256": entry["identity_sha256"], "prior_inventory_match": entry["prior_inventory_match"],
                              "label": "unknown", "predictions": predictions,
                              "state_unique_rows": len({(*r["q_obs"], r.get("gripper_obs")) for r in rows})})
    first_s = time.perf_counter() - first_started
    method_times = {}
    for name, method in (("current", lambda rows: detect_freezes(rows, **PARAMETERS)), ("repeat_baseline", repeat_baseline)):
        started = time.perf_counter()
        for _, _, rows in loaded:
            method(rows)
        method_times[name] = time.perf_counter() - started
    synthetic = []
    for case in cases:
        if case["scope"] == "cause_ambiguity":
            continue
        predictions = _run_methods(case["rows"])
        synthetic.append({k: case[k] for k in ("id", "kind", "scope", "cause", "expected")} | {
            "fps": case["header"]["fps"], "frame_count": len(case["rows"]),
            "metrics": {name: score_events(case["expected"], pred, len(case["rows"])) for name, pred in predictions.items()},
            "predictions": predictions})
    ambiguity_cases = [case for case in cases if case["scope"] == "cause_ambiguity"]
    first, second = ambiguity_cases
    cause_ambiguity = {"case_ids": [case["id"] for case in ambiguity_cases],
                       "rows_equal": first["rows"] == second["rows"],
                       "predictions_equal": _run_methods(first["rows"]) == _run_methods(second["rows"]),
                       "cause_labels_differ": first["cause"] != second["cause"],
                       "root_cause_identifiable": False,
                       "note": "같은 증상에 대한 경고는 두 원인 모두에서 타당하며 원인 구분 점수로 사용하지 않습니다"}
    # 실제 기록 주입은 주입 밖을 정상으로 가정하지 않고 원본 대비 경고 변화를 계산한다.
    injections = []
    for entry, header, rows in loaded:
        if len(rows) < 80:
            continue
        start, end = len(rows) // 3, min(len(rows) // 3 + 29, len(rows) - 2)
        changed = copy.deepcopy(rows)
        _hold(changed, start, end)
        for i in range(start, end + 1):
            changed[i]["gripper_obs"] = changed[start - 1].get("gripper_obs")
        changed_frames = sum(changed[i]["q_obs"] != rows[i]["q_obs"] or changed[i].get("gripper_obs") != rows[i].get("gripper_obs") for i in range(start, end + 1))
        qualifying_run = max_run = 0
        for i in range(start, end + 1):
            obs_delta = [abs(math.degrees(a - b)) for a, b in zip(changed[i]["q_obs"], changed[i - 1]["q_obs"])]
            cmd_delta = [abs(math.degrees(a - b)) for a, b in zip(changed[i]["q_cmd"], changed[i - 1]["q_cmd"])]
            for key, values in (("gripper_obs", obs_delta), ("gripper_cmd", cmd_delta)):
                if changed[i].get(key) is not None and changed[i - 1].get(key) is not None:
                    values.append(abs(changed[i][key] - changed[i - 1][key]))
            qualifies = all(v < PARAMETERS["eps_deg"] for v in obs_delta) and any(v > PARAMETERS["delta_deg"] for v in cmd_delta)
            qualifying_run = qualifying_run + 1 if qualifies else 0
            max_run = max(max_run, qualifying_run)
        before, after = _run_methods(rows), _run_methods(changed)
        methods = {}
        for name in before:
            b = _frames([(p["frame_start"], p["frame_end"]) for p in before[name]])
            a = _frames([(p["frame_start"], p["frame_end"]) for p in after[name]])
            target = set(range(start, end + 1))
            methods[name] = {"original_already_overlaps": bool(b & target), "injected_overlap_frames": len(a & target),
                             "new_alert_frames_in_injection": len((a - b) & target),
                             "new_alert_frames_outside_injection": len((a - b) - target),
                             "removed_alert_frames": len(b - a)}
        cmd_span = [math.degrees(max(r["q_cmd"][j] for r in rows[start:end + 1]) - min(r["q_cmd"][j] for r in rows[start:end + 1])) for j in range(len(header["joint_names"]))]
        injections.append({"id": entry["id"], "injected_frame_start": start, "injected_frame_end": end,
                           "observation_changed_frames": changed_frames,
                           "max_qualifying_transition_run_frames": max_run,
                           "arm_command_span_deg": cmd_span,
                           "max_arm_command_span_deg": max(cmd_span),
                           "methods": methods})
    alert_counts = {name: {"episodes_with_alert": sum(bool(r["predictions"][name]) for r in retrospective),
                           "intervals": sum(len(r["predictions"][name]) for r in retrospective)} for name in method_times}
    result = {"schema": SCHEMA, "scope": "retrospective_and_controlled_challenge",
              "deployment_verdict": "not_validated", "protocol": protocol,
              "corpus": {"dataset_count": manifest["dataset_count"], "raw_episodes": len(manifest["episodes"]),
                         "unique_episodes": len(retrospective), "duplicate_episodes": len(manifest["episodes"]) - len(retrospective),
                         "frames": sum(r["frames"] for r in retrospective),
                         "all_prior_inventory_match": all(r["prior_inventory_match"] for r in manifest["episodes"])},
              "retrospective_alert_counts": alert_counts, "retrospective": retrospective,
              "synthetic": synthetic, "cause_ambiguity": cause_ambiguity, "real_injection_delta": injections,
              "timing": {"first_pass_including_jsonl_read_and_both_detectors_s": first_s,
                         "repeat_loaded_rows_detector_only_s": method_times, "os_cache_controlled": False},
              "unmeasured": ["real_world_precision_recall", "human_review_time_saved", "prospective_holdout_performance", "root_cause_accuracy"],
              "limitations": ["실제 기록은 과거 기준 선택에 사용됐고 독립 정답이 없어 정확도를 계산하지 않습니다.",
                              "통제 시험 점수는 생성한 신호와 주입 조건에만 해당합니다.",
                              "같은 관측 정체가 기록 갱신 정지와 기구 걸림에서 모두 생길 수 있습니다.",
                              "실제 기록 주입의 구간 밖은 미라벨이므로 오탐으로 부르지 않습니다."]}
    write_json(out / "result.json", result)
    summary = {k: result[k] for k in ("schema", "scope", "deployment_verdict", "protocol", "corpus",
                                     "retrospective_alert_counts", "synthetic", "cause_ambiguity", "timing", "unmeasured", "limitations")}
    summary["full_result_sha256"] = sha256_file(out / "result.json")
    summary["acquisition_independence"] = "not_established_derived_datasets_remain"
    summary["retrospective_episodes"] = [
        {k: r[k] for k in ("id", "dataset", "episode", "frames", "duration_s", "identity_sha256", "prior_inventory_match", "label", "state_unique_rows")} |
        {"alert_intervals": {name: len(pred) for name, pred in r["predictions"].items()}}
        for r in retrospective
    ]
    summary["real_injection_summary"] = {name: {
        "episodes": len(injections),
        "effective_mutations": sum(i["observation_changed_frames"] > 0 for i in injections),
        "noop_mutations": sum(i["observation_changed_frames"] == 0 for i in injections),
        "contract_qualifying_windows": sum(i["max_qualifying_transition_run_frames"] >= PARAMETERS["min_frames"] for i in injections),
        "original_already_overlaps": sum(i["methods"][name]["original_already_overlaps"] for i in injections),
        "injection_overlaps": sum(i["methods"][name]["injected_overlap_frames"] > 0 for i in injections),
        "new_alert_in_injection": sum(i["methods"][name]["new_alert_frames_in_injection"] > 0 for i in injections),
        "new_alert_frames_outside_injection": sum(i["methods"][name]["new_alert_frames_outside_injection"] for i in injections),
        "warning": "미라벨 실제 기록에 대한 경고 변화이며 정확도가 아닙니다",
    } for name in method_times}
    write_json(out / "summary.json", summary)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="기존 기록 경고 분포와 통제된 오류 주입 평가(CPU 전용)")
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.corpus, args.out)
    print(json.dumps({"result": display_path((args.out / "result.json").resolve()),
                      "corpus": result["corpus"], "alerts": result["retrospective_alert_counts"],
                      "synthetic_cases": len(result["synthetic"]), "deployment_verdict": result["deployment_verdict"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
