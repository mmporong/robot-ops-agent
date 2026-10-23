"""계획·관측 위치 차이를 대조 실험으로 검사한다. 실물 실행 경로는 없다."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from .cup_diagnosis import derive_plan_correction, evaluate_common_reference
from .replay.artifacts import sha256_file, write_json
from .replay.doctor import check_profile
from .replay.profile import Profile, display_path, load_profile
from .replay.regress import run_regress
from .replay.source_provenance import require_same_tool_manifest, validate_tool_sources
from .simulation_workflow import ASSETS, EARLY_PHASES, _cell, inspect_via_mcp

LABELS = {"original": "원래 목표", "sham": "같은 목표 재실행", "corrected": "관측 위치로 목표 보정", "opposite": "반대 방향으로 목표 변경"}
POSITION_TOLERANCE_M = 0.0001
JOINT_TOLERANCE_RAD = 0.0001


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _vector(value: object, *, length: int | None = None) -> list[float]:
    if (not isinstance(value, list) or not value or (length is not None and len(value) != length)
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value)):
        raise ValueError("통제 조건의 좌표·관절값은 유한한 숫자 배열이어야 합니다")
    return value


def diagnose(result: dict, config: dict) -> dict:
    """주입 정보·실패 코드 없이 계획 좌표와 동작 전 관측만 전달한다."""
    duration = config["recovery"]["observation_duration_s"]
    raw = result["samples"]
    start = raw[0]["time_s"]
    fields = ("time_s", "physics_time_s", "phase", "attempt", "cup_position_m", "contact_force_n")
    window = []
    for sample in raw:
        if sample["time_s"] > start + duration + 1e-9 or sample["phase"] != "RESET":
            break
        window.append({key: sample[key] for key in fields})
    return derive_plan_correction(config["cup_center_m"], window, window_s=duration,
                                  max_motion_m=config["recovery"]["maximum_observation_motion_m"],
                                  contact_threshold_n=config["minimum_contact_force_n"])


def validate_case_source(source_root: Path, case_plan: dict) -> None:
    """GPU 실행 전에 입력 사례가 고정 소스에서 생성됐는지 확인한다."""
    source_config_path = source_root / "config/simulation/cup_contact_experiment.json"
    source_config_sha = sha256_file(source_config_path)
    validate_tool_sources(source_root, case_plan)
    if (case_plan.get("source_config_sha256") != source_config_sha
        or case_plan.get("config_sha256") != source_config_sha):
        raise ValueError("입력 사례의 설정 해시가 고정 소스와 다릅니다")


def verify_pair(baseline: dict, baseline_plan: dict, result: dict, plan: dict) -> dict:
    """계획과 보상용 오프셋 이외의 설정 및 실제 초기 상태를 대조한다."""
    a, b = baseline_plan["config"], plan["config"]
    other_a = {k: v for k, v in a.items() if k not in ("cup_center_m", "cup_spawn_offset_m")}
    other_b = {k: v for k, v in b.items() if k not in ("cup_center_m", "cup_spawn_offset_m")}
    if other_a != other_b:
        raise ValueError("대조 실험에서 물리 설정 또는 판정 임계값이 바뀌었습니다")
    spawn_a = [p + d for p, d in zip(_vector(a["cup_center_m"], length=3), _vector(a["cup_spawn_offset_m"], length=3))]
    spawn_b = [p + d for p, d in zip(_vector(b["cup_center_m"], length=3), _vector(b["cup_spawn_offset_m"], length=3))]
    if math.dist(spawn_a, spawn_b) > 1e-9:
        raise ValueError("대조 실험에서 실제 컵 배치가 바뀌었습니다")
    if a["cup_center_m"][2] != b["cup_center_m"][2] or a["cup_spawn_offset_m"][2] != b["cup_spawn_offset_m"][2]:
        raise ValueError("XY 계획 보정에서 높이 설정이 바뀌었습니다")
    first_a, first_b = baseline["samples"][0], result["samples"][0]
    position_delta = math.dist(_vector(first_a["cup_position_m"], length=3), _vector(first_b["cup_position_m"], length=3))
    if position_delta > POSITION_TOLERANCE_M:
        raise ValueError("대조 실험의 초기 컵 위치가 0.1mm보다 많이 다릅니다")
    reset_delta = math.dist(diagnose(baseline, a)["observed_center_m"], diagnose(result, b)["observed_center_m"])
    if reset_delta > POSITION_TOLERANCE_M:
        raise ValueError("대조 실험의 안정 RESET 위치가 0.1mm보다 많이 다릅니다")
    joint_deltas = []
    for key in ("left_joint_actual_rad", "gripper_positions_rad"):
        qa, qb = _vector(first_a[key]), _vector(first_b[key])
        if len(qa) != len(qb) or not qa:
            raise ValueError("초기 관절 기록을 대조할 수 없습니다")
        joint_deltas.extend(abs(x - y) for x, y in zip(qa, qb))
    if max(joint_deltas) > JOINT_TOLERANCE_RAD:
        raise ValueError("대조 실험의 초기 관절 상태가 다릅니다")
    try:
        require_same_tool_manifest(plan, baseline_plan)
    except ValueError as exc:
        raise ValueError("대조 실험의 시뮬레이터 소스 해시 또는 원천 경로가 다릅니다") from exc
    return {"physical_spawn_equal": True, "physics_and_thresholds_equal": True,
            "source_tools_equal": True, "initial_position_delta_mm": position_delta * 1000,
            "reset_median_position_delta_mm": reset_delta * 1000,
            "initial_joint_max_delta_rad": max(joint_deltas),
            "position_tolerance_mm": POSITION_TOLERANCE_M * 1000,
            "joint_tolerance_rad": JOINT_TOLERANCE_RAD}


def _load_run(out: Path, name: str) -> tuple[dict, dict, Path]:
    _, directory = _cell(out, name)
    path = directory / "isaac/result.json"
    return read_json(path), read_json(path.with_name("plan.json")), directory


def verify_treatment(entry: dict, plan: dict, source_config: dict, request: dict, response: dict) -> None:
    """계획에 실제 적용된 +C/-C를 요청·응답 및 원천 설정과 대조한다."""
    correction = entry["correction_mm"]
    change = [0., 0.] if correction is None else _vector(correction, length=2)
    xy = _vector(entry["position_mm"], length=2)
    source_center = _vector(source_config["cup_center_m"], length=3)
    expected_center = [source_center[i] + (change[i] / 1000 if i < 2 else 0) for i in range(3)]
    expected_offset = [(xy[i] - change[i]) / 1000 if i < 2 else source_config["cup_spawn_offset_m"][2] for i in range(3)]
    for actual, expected in ((plan["config"]["cup_center_m"], expected_center), (plan["config"]["cup_spawn_offset_m"], expected_offset)):
        if math.dist(_vector(actual, length=3), expected) > 1e-9:
            raise ValueError("실제 계획 개입이 요청한 보정 +C/-C와 다릅니다")
    params = request.get("params") or {}
    if (request.get("grid_point") != {"x_mm": xy[0], "y_mm": xy[1]}
        or params.get("recover") is not False or params.get("plan_correction_mm") != correction):
        raise ValueError("실행 요청과 실험 조건이 다릅니다")
    metadata = response.get("plan_intervention")
    if correction is None:
        if metadata is not None:
            raise ValueError("무보정 실행에 계획 개입이 기록되었습니다")
        return
    if (not isinstance(metadata, dict) or metadata.get("correction_mm") != correction
        or metadata.get("original_grid_point_mm") != xy
        or metadata.get("original_planned_center_m") != source_center):
        raise ValueError("보정 요청과 어댑터 개입 기록이 다릅니다")
    for actual, expected in ((metadata.get("corrected_planned_center_m"), expected_center),
                             (metadata.get("effective_grid_point_mm"), [xy[i] - change[i] for i in range(2)]),
                             (metadata.get("actual_spawn_center_m"), [source_center[i] + xy[i] / 1000 if i < 2 else source_center[i] + source_config["cup_spawn_offset_m"][i] for i in range(3)])):
        if math.dist(_vector(actual, length=len(expected)), expected) > 1e-9:
            raise ValueError("어댑터 보정 좌표가 실행 계획과 다릅니다")


def experiment_passed(experiment: dict) -> bool:
    by_name = {e["name"]: e for e in experiment["runs"]}
    core, regression = experiment["core"], experiment["regression"]
    if set(core) != set(LABELS) or any(len(names) != 3 for names in core.values()):
        raise ValueError("네 핵심 조건의 3회 반복이 필요합니다")
    core_names = [name for names in core.values() for name in names]
    if len(set(core_names)) != 12 or len(by_name) != len(experiment["runs"]):
        raise ValueError("실험 실행 이름이 중복되었습니다")
    if len(regression) != 9 or {(c["x_mm"], c["y_mm"]) for c in regression} != {(x, y) for x in (-5, 0, 5) for y in (-5, 0, 5)}:
        raise ValueError("주변 3×3 위치 비교가 필요합니다")
    for cell in regression:
        for key in ("baseline", "corrected"):
            if cell[f"{key}_pass"] is not by_name[cell[key]]["common_evaluation"]["passed"]:
                raise ValueError("격자 표시와 실행 판정이 다릅니다")
    normal = next(c for c in regression if c["x_mm"] == 0 and c["y_mm"] == 0)
    return (all(by_name[n]["common_evaluation"]["passed"] == (k == "corrected") for k, names in core.items() for n in names)
            and all(c["corrected_pass"] for c in regression) and normal["baseline_pass"])


def validate_experiment(root: Path, experiment: dict, audit: Path) -> None:
    """대표 영상 외의 회귀 실행도 원시 파일·공통 판정·MCP로 다시 검사한다."""
    base_name = experiment["core"]["original"][0]
    base_result, base_plan, _ = _load_run(root, base_name)
    actual_commit = subprocess.run(["git", "-C", str(root / "source"), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    dirty = subprocess.run(["git", "--no-optional-locks", "-C", str(root / "source"), "status", "--porcelain"], check=True, capture_output=True, text=True, timeout=30).stdout
    if actual_commit != experiment["source_commit"] or dirty:
        raise ValueError("고정 실행 사본의 커밋 또는 작업 상태가 달라졌습니다")
    source_config_path = root / "source/config/simulation/cup_contact_experiment.json"
    source_config = read_json(source_config_path)
    source_config_sha = sha256_file(source_config_path)
    case_path = root / "input-case/result.json"
    if (sha256_file(case_path) != experiment["case_result_sha256"]
        or sha256_file(case_path.with_name("plan.json")) != experiment["case_plan_sha256"]):
        raise ValueError("입력 사례의 원본 해시가 다릅니다")
    case_plan = read_json(case_path.with_name("plan.json"))
    if diagnose(read_json(case_path), case_plan["config"]) != experiment["proposal"]:
        raise ValueError("입력 사례에서 다시 계산한 제안이 실험 제안과 다릅니다")
    try:
        require_same_tool_manifest(case_plan, base_plan)
    except ValueError as exc:
        raise ValueError("입력 사례와 고정 소스의 출처가 다릅니다") from exc
    if case_plan["source_config_sha256"] != source_config_sha:
        raise ValueError("입력 사례와 고정 소스의 출처가 다릅니다")
    if base_plan["source_config_sha256"] != source_config_sha or base_plan["config_sha256"] != source_config_sha:
        raise ValueError("기준 실행의 설정 해시가 원천 설정과 다릅니다")
    case_analysis = asyncio.run(inspect_via_mcp(case_path, root / "input-case", audit.with_name("case-audit.jsonl")))
    if case_analysis["runtime"]["hardware_accessed"] is not False or case_analysis["runtime"]["observation_source"] != "simulator_ground_truth_not_rgb":
        raise ValueError("시뮬레이터 정답 좌표를 이용한 사례가 필요합니다")
    case_alignment = asyncio.run(inspect_via_mcp(case_path, root / "input-case", audit.with_name("alignment-audit.jsonl"), tool_name="analyze_cup_alignment"))
    if case_alignment["proposal"] != experiment["proposal"]:
        raise ValueError("MCP 위치 차이 검사와 입력 제안이 다릅니다")
    validate_tool_sources(root / "source", base_plan)
    if experiment["proposal"] != diagnose(base_result, base_plan["config"]):
        raise ValueError("입력 사례의 진단이 새 baseline에서 재현되지 않았습니다")
    baseline_config = {k: v for k, v in base_plan["config"].items() if k not in ("cup_center_m", "cup_spawn_offset_m")}
    paired_to = {name: base_name for names in experiment["core"].values() for name in names if name != base_name}
    paired_to.update({cell["corrected"]: cell["baseline"] for cell in experiment["regression"]})
    by_name = {e["name"]: e for e in experiment["runs"]}
    proposal_correction = experiment["proposal"]["correction_mm"]
    core_position = by_name[base_name]["position_mm"]
    expected_core_corrections = {"original": None, "sham": [0., 0.], "corrected": proposal_correction, "opposite": [-v for v in proposal_correction]}
    expected_names = set()
    for kind, names in experiment["core"].items():
        for name in names:
            expected_names.add(name)
            if by_name[name]["correction_mm"] != expected_core_corrections[kind] or by_name[name]["position_mm"] != core_position:
                raise ValueError("핵심 대조군의 실제 개입 또는 컵 위치가 프로토콜과 다릅니다")
    for cell in experiment["regression"]:
        before, after = by_name[cell["baseline"]], by_name[cell["corrected"]]
        expected_names.update((before["name"], after["name"]))
        if (before["position_mm"] != [cell["x_mm"], cell["y_mm"]] or after["position_mm"] != before["position_mm"]
            or before["correction_mm"] is not None or after["correction_mm"] != before["diagnosis"]["correction_mm"]):
            raise ValueError("격자 짝비교가 같은 컵 위치의 관측 기반 보정과 다릅니다")
    if set(by_name) != expected_names:
        raise ValueError("프로토콜에 해당하지 않는 실행이 포함되었습니다")
    frozen_core_reference = diagnose(base_result, base_plan["config"])["observed_center_m"]
    for name, entry in by_name.items():
        result, plan, directory = _load_run(root, name)
        request, response = read_json(directory / "run.request.json"), read_json(directory / "run.response.json")
        verify_treatment(entry, plan, source_config, request, response)
        if entry["correction_mm"] is not None:
            generated = directory / "intervention_config.json"
            metadata = response["plan_intervention"]
            if (metadata["source_config_sha256"] != source_config_sha or sha256_file(generated) != metadata["generated_config_sha256"]
                or plan["source_config_sha256"] != metadata["generated_config_sha256"]):
                raise ValueError("원천·생성·실행 설정 파일 해시가 다릅니다")
            generated_doc = read_json(generated)
            if (generated_doc["cup_center_m"] != metadata["corrected_planned_center_m"]
                or {k: v for k, v in generated_doc.items() if k != "cup_center_m"} != {k: v for k, v in source_config.items() if k != "cup_center_m"}):
                raise ValueError("생성 설정에서 계획 중심 외의 값이 바뀌었습니다")
        for key, path in (("result_sha256", directory / "isaac/result.json"), ("plan_sha256", directory / "isaac/plan.json")):
            if sha256_file(path) != entry[key] or entry["analysis"]["source"][key] != entry[key]:
                raise ValueError("실험 뒤 원시 파일 또는 검사 출처 해시가 바뀌었습니다")
        if {k: v for k, v in plan["config"].items() if k not in ("cup_center_m", "cup_spawn_offset_m")} != baseline_config:
            raise ValueError("실험 전체에서 물리 조건 또는 소스가 달라졌습니다")
        try:
            require_same_tool_manifest(plan, base_plan)
        except ValueError as exc:
            raise ValueError("실험 전체에서 물리 조건 또는 소스가 달라졌습니다") from exc
        baseline_name = paired_to.get(name)
        if baseline_name:
            baseline, baseline_plan, _ = _load_run(root, baseline_name)
            controls = verify_pair(baseline, baseline_plan, result, plan)
            reference = diagnose(baseline, baseline_plan["config"])["observed_center_m"]
            if controls != entry["controls"]:
                raise ValueError("통제 조건 재검사 결과가 저장 기록과 다릅니다")
        else:
            reference = diagnose(result, plan["config"])["observed_center_m"]
        if name.startswith("core_"):
            reference = frozen_core_reference
        if reference != entry["reference_m"]:
            raise ValueError("공통 평가 기준이 baseline 관측 위치와 다릅니다")
        if diagnose(result, plan["config"]) != entry["diagnosis"]:
            raise ValueError("초기 관측 진단과 저장 기록이 다릅니다")
        if evaluate_common_reference(result, plan["config"], reference) != entry["common_evaluation"]:
            raise ValueError("공통 판정 재검사 결과가 저장 기록과 다릅니다")
        analysis = asyncio.run(inspect_via_mcp(directory / "isaac/result.json", root / "artifacts/runs", audit))
        if analysis != entry["analysis"]:
            raise ValueError("MCP 재검사와 저장 기록이 다릅니다")
        alignment = asyncio.run(inspect_via_mcp(directory / "isaac/result.json", root / "artifacts/runs", audit.with_name("alignment-audit.jsonl"), tool_name="analyze_cup_alignment"))
        if alignment["proposal"] != entry["diagnosis"] or alignment["source"] != analysis["source"]:
            raise ValueError("MCP 위치 차이 검사와 저장 진단·출처가 다릅니다")
        record = read_json(root / f"{name}.json")
        if (record.get("gpu_check") or {}).get("all_released") is not True or record.get("gpu_check") != entry["gpu_check"]:
            raise ValueError("GPU 메모리 반환 기록이 없습니다")
        if record["provenance"]["source_repo_commit"] != experiment["source_commit"]:
            raise ValueError("실행 소스 커밋이 실험 프로토콜과 다릅니다")
    audit_rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    started = [r for r in audit_rows if r.get("layer") == "mcp" and r.get("phase") == "started"]
    completed = [r for r in audit_rows if r.get("layer") == "mcp" and r.get("phase") == "completed"]
    expected_paths = {str(_load_run(root, n)[2] / "isaac/result.json") for n in by_name}
    if (len(started) != len(by_name) or len(completed) != len(by_name)
        or any(r.get("ok") is not True for r in completed)
        or {r["trace_id"] for r in started} != {r["trace_id"] for r in completed}
        or {r.get("arguments", {}).get("path") for r in started} != expected_paths):
        raise ValueError("전체 실험 실행과 일치하는 MCP 감사 기록이 필요합니다")
    if experiment_passed(experiment) is not experiment["success"]:
        raise ValueError("실험 성공 표시와 재계산한 판정이 다릅니다")


def run_experiment(profile: Profile, case: Path, out: Path, *, source_commit: str | None = None) -> dict:
    """네 조건 3회와 3×3 짝비교를 고정 소스에서 순차 실행한다."""
    if profile.source_repo is None:
        raise ValueError("원천 저장소가 필요합니다")
    out, case = out.resolve(), case.resolve()
    if out.is_relative_to(profile.source_repo.resolve()):
        raise ValueError("원천 저장소에 실험 결과를 쓸 수 없습니다")
    if out.exists() and any(out.iterdir()):
        raise ValueError("새 실험에는 비어 있는 출력 경로가 필요합니다")
    case_result, case_plan = read_json(case), read_json(case.with_name("plan.json"))
    proposal = diagnose(case_result, case_plan["config"])
    if proposal["status"] != "candidate_available":
        raise ValueError("검증할 위치 보정 후보가 없습니다")
    rev = source_commit or profile.project.get("pinned_commit")
    if not isinstance(rev, str) or not rev:
        raise ValueError("고정 원천 커밋이 필요합니다")
    commit = subprocess.run(["git", "--no-optional-locks", "-C", str(profile.source_repo), "rev-parse", "--verify", "--end-of-options", rev + "^{commit}"],
                            check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    out.mkdir(parents=True, exist_ok=True)
    case_copy = out / "input-case"
    case_copy.mkdir()
    shutil.copy2(case, case_copy / "result.json")
    shutil.copy2(case.with_name("plan.json"), case_copy / "plan.json")
    asyncio.run(inspect_via_mcp(case_copy / "result.json", case_copy, out / "input-case-audit.jsonl"))
    alignment = asyncio.run(inspect_via_mcp(case_copy / "result.json", case_copy, out / "proposal-audit.jsonl", tool_name="analyze_cup_alignment"))
    if alignment["proposal"] != proposal:
        raise ValueError("MCP 위치 차이 검사와 입력 제안이 다릅니다")
    proposal = alignment["proposal"]
    write_json(out / "proposal.json", proposal)
    # 실행 결과를 보기 전에 개입값·평가 기준·성공 조건을 기록한다.
    protocol = {"schema": "robot-ops-cup-cause-experiment/1", "development_case": True,
                "source_commit": commit, "case_result_sha256": sha256_file(case),
                "case_plan_sha256": sha256_file(case.with_name("plan.json")), "proposal": proposal,
                "core_repetitions": 3, "grid_mm": [-5, 0, 5],
                "intervention": "P_new=P+C; D_new=D-C; actual spawn unchanged",
                "evaluation": "frozen baseline observed center; same thresholds; recovery disabled",
                "criteria": "original/sham/opposite fail 3/3; corrected pass 3/3; corrected grid pass 9/9",
                "claim_scope": "rigid-pad simulation with simulator ground-truth positions; development experiment"}
    write_json(out / "protocol.json", protocol)
    snapshot = out / "source"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", "--", str(profile.source_repo), str(snapshot)], check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(snapshot), "checkout", "--detach", commit], check=True, capture_output=True, timeout=60)
    validate_case_source(snapshot, case_plan)
    profile = replace(profile, source_repo=snapshot, artifacts_root=out / "artifacts",
                      project={**profile.project, "source_repo": str(snapshot), "pinned_commit": commit})
    prerequisites = check_profile(profile, task="cup_contact")
    write_json(out / "runtime-prerequisites.json", prerequisites)
    if prerequisites["status"] != "ready":
        raise ValueError("시뮬 실행 전제 조건이 부족합니다: runtime-prerequisites.json")
    template = read_json(profile.base_dir / "scenarios/bimanual-cup-premature-contact/scenario.json")
    entries = []

    def execute(name: str, xy: list[float], correction: list[float] | None, *, record: bool,
                baseline_name: str | None = None, reference: list[float] | None = None) -> dict:
        scenario = copy.deepcopy(template)
        params: dict = {"recover": False}
        if correction is not None:
            params["plan_correction_mm"] = correction
        scenario["conditions"] = {name: {"params": params, "label_ko": name, "judgement_basis": "fixed_reference_no_recovery"}}
        scenario["grid"] = {"spawn_x_offset_mm": [xy[0]], "spawn_y_offset_mm": [xy[1]], "repeat_points": 0, "repeats": 0}
        print(json.dumps({"event": "run_started", "name": name, "position_mm": xy, "correction_mm": correction}, ensure_ascii=False), flush=True)
        record_doc = run_regress(profile, scenario, conditions=[name], record_cells={name: [tuple(xy)]} if record else None)
        write_json(out / f"{name}.json", record_doc)
        result, plan, directory = _load_run(out, name)
        alignment = asyncio.run(inspect_via_mcp(directory / "isaac/result.json", out / "artifacts/runs", out / "alignment-execution-audit.jsonl", tool_name="analyze_cup_alignment"))
        proposal_now = alignment["proposal"]
        reference = reference if reference is not None else proposal_now["observed_center_m"]
        common = evaluate_common_reference(result, plan["config"], reference)
        analysis = asyncio.run(inspect_via_mcp(directory / "isaac/result.json", out / "artifacts/runs", out / "mcp-audit.jsonl"))
        if analysis["runtime"].get("time_axis_agrees") is not True:
            raise ValueError("물리 시간 진행을 확인할 수 없습니다")
        controls = None
        if baseline_name:
            baseline, baseline_plan, _ = _load_run(out, baseline_name)
            controls = verify_pair(baseline, baseline_plan, result, plan)
        entry = {"name": name, "position_mm": xy, "correction_mm": correction, "diagnosis": proposal_now,
                 "reference_m": reference, "common_evaluation": common, "controls": controls,
                 "analysis": analysis, "run_id": directory.name, "gpu_check": record_doc.get("gpu_check"),
                 "result_sha256": sha256_file(directory / "isaac/result.json"),
                 "plan_sha256": sha256_file(directory / "isaac/plan.json")}
        write_json(out / f"{name}-evaluation.json", entry)
        entries.append(entry)
        write_json(out / "checkpoint.json", {"runs": entries})
        print(json.dumps({"event": "run_finished", "name": name, "common_verdict": common["verdict"],
                          "contact_count": analysis["premature_contact_check"]["issue_count"],
                          "gpu_released": (entry["gpu_check"] or {}).get("all_released")}, ensure_ascii=False), flush=True)
        return entry

    offset = case_plan["config"]["cup_spawn_offset_m"]
    core_xy = [offset[0] * 1000, offset[1] * 1000]
    correction = proposal["correction_mm"]
    core_reference = None
    core = {name: [] for name in LABELS}
    for repeat in range(3):
        for kind, change in (("original", None), ("sham", [0., 0.]), ("corrected", correction), ("opposite", [-v for v in correction])):
            name = f"core_{kind}_{repeat}"
            baseline_name = None if name == "core_original_0" else "core_original_0"
            entry = execute(name, core_xy, change, record=repeat == 0, baseline_name=baseline_name, reference=core_reference)
            if core_reference is None:
                core_reference = entry["reference_m"]
                current = entry["diagnosis"]["correction_mm"]
                if math.dist(current, correction) > POSITION_TOLERANCE_M * 1000:
                    raise ValueError("새 실행에서 진단한 보정량이 입력 사례와 다릅니다")
            core[kind].append(name)

    regression = []
    for x in (-5, 0, 5):
        for y in (-5, 0, 5):
            if [x, y] == core_xy:
                before, after = entries[0], next(e for e in entries if e["name"] == "core_corrected_0")
            else:
                before = execute(f"grid_{x}_{y}_baseline", [x, y], None, record=False)
                change = before["diagnosis"]["correction_mm"]
                after = execute(f"grid_{x}_{y}_corrected", [x, y], change, record=False,
                                baseline_name=before["name"], reference=before["reference_m"])
            regression.append({"x_mm": x, "y_mm": y, "baseline": before["name"], "corrected": after["name"],
                               "baseline_pass": before["common_evaluation"]["passed"], "corrected_pass": after["common_evaluation"]["passed"]})
    experiment = {**protocol, "runs": entries, "core": core, "regression": regression}
    experiment["success"] = experiment_passed(experiment)
    write_json(out / "experiment.json", experiment)
    build_cause_report(out, out / "report")
    return experiment


def build_cause_report(root: Path, out: Path) -> dict:
    root, out = root.resolve(), out.resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("보고서 출력 경로는 비어 있어야 합니다")
    experiment = read_json(root / "experiment.json")
    out.mkdir(parents=True, exist_ok=True)
    validate_experiment(root, experiment, out / "mcp-audit.jsonl")
    (out / "media").mkdir()
    stages = []
    for kind in LABELS:
        name = experiment["core"][kind][0]
        entry = next(e for e in experiment["runs"] if e["name"] == name)
        result, plan, directory = _load_run(root, name)
        if sha256_file(directory / "isaac/result.json") != entry["result_sha256"] or sha256_file(directory / "isaac/plan.json") != entry["plan_sha256"]:
            raise ValueError("실험 뒤 원시 결과 또는 계획이 바뀌었습니다")
        response = read_json(directory / "run.response.json")
        video = directory / "cup_contact.mp4"
        if video.is_symlink() or sha256_file(video) != response["video"]["sha256"]:
            raise ValueError("녹화 파일과 실행 응답 해시가 다릅니다")
        shutil.copy2(video, out / "media" / f"{kind}.mp4")
        common = evaluate_common_reference(result, plan["config"], entry["reference_m"])
        if common != entry["common_evaluation"]:
            raise ValueError("저장된 실험 판정과 재검사 결과가 다릅니다")
        analysis = entry["analysis"]
        issues = analysis["premature_contact_check"]["issues"]
        compact = [{"t_s": s["time_s"], "phase": s["phase"], "force_n": max(s["contact_force_n"]),
                    "preclose": s["phase"] in EARLY_PHASES,
                    "cup_z_mm": (s["cup_position_m"][2] - entry["reference_m"][2]) * 1000,
                    "attempt": s["attempt"]} for s in result["samples"]]
        title = ("목표 보정 후 조기 접촉 없이 상승 통과" if kind == "corrected" else "이 목표로 컵 상승 통과") if common["passed"] else "같은 컵 위치에서 실패"
        stages.append({"id": kind, "label_ko": LABELS[kind], "description_ko": f"계획 Y={plan['config']['cup_center_m'][1] * 1000:.1f}mm · 실제 컵 Y={entry['reference_m'][1] * 1000:.1f}mm · 사후 복구 끔",
                       "video": f"media/{kind}.mp4", "verdict": common["verdict"], "samples": compact,
                       "sim_time_s": result["samples"][-1]["time_s"], "sample_count": len(compact),
                       "event_t_s": issues[0]["time_s"] if issues else None,
                       "force_threshold_n": plan["config"]["minimum_contact_force_n"],
                       "result_title_ko": title, "result_description_ko": "컵 배치·물리 설정·초기 관절·판정 기준을 고정하고 계획 좌표만 비교했습니다.",
                       "analysis": analysis, "common_evaluation": common,
                       "source": {"run_id": entry["run_id"], "result_sha256": entry["result_sha256"], "plan_sha256": entry["plan_sha256"], "video_sha256": sha256_file(video)}})
    by_name = {e["name"]: e for e in experiment["runs"]}
    proposal = experiment["proposal"]
    summary = "계획과 관측 위치의 차이를 보정해 문제를 예방했습니다." if experiment["success"] else "목표 보정의 효과와 남은 실패를 대조 실험으로 확인했습니다."
    payload = {"schema": "robot-ops-simulation-proof/1", "stage_order": list(LABELS), "primary_stage": "original",
               "title_ko": "컵은 같은 자리에 두고, 로봇의 목표만 고칩니다",
               "question_ko": "접촉 표시에서 끝내지 않고, 계획 좌표 오차를 찾고 목표 보정의 효과를 비교합니다.",
               "execution_ko": "고정 소스에서 새로 실행한 물리 시뮬레이션 녹화입니다. 모든 조건에서 사후 복구는 꺼져 있습니다.",
               "stages": stages, "diagnosis": {"summary_ko": summary, **{k: proposal[k] for k in ("planned_center_m", "observed_center_m", "correction_mm")},
                  "core_repetitions": 3, "core_summary": [{"label_ko": LABELS[k], "passed": sum(by_name[n]["common_evaluation"]["passed"] for n in names), "total": len(names)} for k, names in experiment["core"].items()],
                  "regression": experiment["regression"], "limitations_ko": ["시뮬레이터 정답 좌표를 이용한 개발용 실험입니다.", "카메라 인식·실물 성능·LLM 원인 추론은 검증하지 않았습니다."]},
               "scope": {"basis_ko": experiment["claim_scope"], "limitations_ko": proposal["interpretation"]["limitations_ko"]},
               "tool_trace": {"tool": "analyze_cup_alignment + analyze_simulation", "transport": "mcp_in_memory", "calls": 2 * len(experiment["runs"]) + 2}}
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", r"\u003c")
    (out / "index.html").write_text(f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><meta http-equiv="Content-Security-Policy" content="default-src \'self\'; script-src \'self\'; style-src \'self\'; media-src \'self\'; img-src \'self\' data:; object-src \'none\'; base-uri \'none\'"><title>계획 위치 오차 대조 실험</title><link rel="icon" href="data:,"><link rel="stylesheet" href="sim-proof.css"><script src="sim-proof.js" defer></script></head><body><main id="sim-proof-app"></main><script type="application/json" id="sim-proof-data">{encoded}</script></body></html>', encoding="utf-8")
    for asset in ("sim-proof.js", "sim-proof.css"):
        shutil.copy2(ASSETS / asset, out / asset)
    write_json(out / "proof.json", payload)
    evidence = {"schema": "robot-ops-cup-cause-evidence/1", "source_commit": experiment["source_commit"],
                "development_case": True, "llm_used": False, "camera_used": False, "hardware_accessed": False,
                "diagnostic_method": "deterministic planned-vs-RESET-median position comparison",
                "observation_source": "simulator_ground_truth_not_rgb", "proposal": proposal,
                "intervention": experiment["intervention"], "evaluation": experiment["evaluation"],
                "core_summary": payload["diagnosis"]["core_summary"], "regression": experiment["regression"],
                "experiment_criteria_passed": experiment["success"],
                "runs": [{key: entry[key] for key in ("name", "position_mm", "correction_mm", "reference_m", "common_evaluation", "controls", "gpu_check", "result_sha256", "plan_sha256")} for entry in experiment["runs"]],
                "source_tools_sha256": read_json(_load_run(root, experiment["core"]["original"][0])[2] / "isaac/plan.json")["tool_sha256"],
                "representative_videos": [{"condition": s["id"], "sha256": s["source"]["video_sha256"]} for s in stages],
                "mcp": {"run_rechecks": len(experiment["runs"]), "audit_sha256": sha256_file(out / "mcp-audit.jsonl"),
                        "input_case_audit_sha256": sha256_file(out / "case-audit.jsonl"),
                        "alignment_rechecks": len(experiment["runs"]) + 1, "alignment_audit_sha256": sha256_file(out / "alignment-audit.jsonl"),
                        "alignment_tool_use_in_this_evidence": "post_run_recheck"},
                "claim_scope": experiment["claim_scope"]}
    write_json(out / "evidence.json", evidence)
    return payload


def command(args: argparse.Namespace) -> int:
    try:
        if args.from_experiment:
            build_cause_report(args.from_experiment.expanduser(), args.out.expanduser())
            report = args.out.resolve() / "index.html"
            success = read_json(args.from_experiment.expanduser() / "experiment.json")["success"]
        else:
            if not args.profile or not args.case:
                raise ValueError("새 실험에는 --profile과 --case가 필요합니다")
            doc = run_experiment(load_profile(args.profile), args.case.expanduser(), args.out.expanduser(), source_commit=args.source_commit)
            success, report = doc["success"], args.out.resolve() / "report/index.html"
        print(json.dumps({"report": display_path(report), "experiment_criteria_passed": success}, ensure_ascii=False))
        return 0 if success else 1
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="관측 기반 목표 보정을 무보정·반대 보정과 비교합니다")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--case", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--from-experiment", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    return command(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
