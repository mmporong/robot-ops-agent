"""실패로 지정한 저장 기록의 검토 구간과 근거별 다음 검사를 고른다."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .recording_inspection import LABELS, _matching_media, inspect_source_identity
from .replay.artifacts import sha256_file
from .replay.recording_check import inspect_recording
from .replay.schema import validate_trajectory_header, validate_trajectory_row

SCHEMA = "robot-ops-incident-review/1"
POLICY = "incident_review_v1"
MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100_000
# 기본 검토 길이(초). 사람 검토 시간의 최적값을 실측한 상수는 아니다.
REVIEW_SPAN_S = 2.0


def read_input(path: Path) -> tuple[dict, list[dict], str]:
    if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".jsonl":
        raise ValueError("입력은 일반 .jsonl 궤적 파일이어야 합니다")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("입력이 32 MiB를 넘었습니다")
    lines = raw.decode("utf-8").splitlines()
    values = [json.loads(line) for line in lines if line.strip()]
    if not 3 <= len(values) <= MAX_ROWS + 1:
        raise ValueError("궤적 행은 2..100000개여야 합니다")
    header, rows = values[0], values[1:]
    validate_trajectory_header(header)
    for i, row in enumerate(rows):
        validate_trajectory_row(row, len(header["joint_names"]), i)
    return header, rows, hashlib.sha256(raw).hexdigest()


def _validate_events(events: list[dict], start: float, end: float) -> None:
    if not isinstance(events, list) or len(events) > 10_000:
        raise ValueError("로그 events는 최대 10000개 목록이어야 합니다")
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("로그 항목은 객체여야 합니다")
        t = event.get("t")
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not start <= t <= end:
            raise ValueError("로그 t는 궤적 시간 범위 안의 유한한 숫자여야 합니다")
        if event.get("level") not in ("info", "warning", "error"):
            raise ValueError("로그 level은 info, warning, error 중 하나여야 합니다")
        if not isinstance(event.get("message"), str) or not 1 <= len(event["message"]) <= 2000:
            raise ValueError("로그 message는 1..2000자여야 합니다")


def load_events(path: Path, trajectory_sha256: str) -> list[dict]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("로그 묶음은 32 MiB 이하 일반 JSON 파일이어야 합니다")
    return parse_events_document(json.loads(path.read_text(encoding="utf-8")), trajectory_sha256)


def parse_events_document(doc: dict, trajectory_sha256: str) -> list[dict]:
    if not isinstance(doc, dict) or doc.get("schema") != "robot-ops-incident-events/1":
        raise ValueError("로그 묶음 schema가 올바르지 않습니다")
    if doc.get("trajectory_sha256") != trajectory_sha256 or doc.get("time_basis") != "trajectory":
        raise ValueError("로그의 궤적 해시와 시간 기준이 일치해야 합니다")
    if not isinstance(doc.get("events"), list):
        raise ValueError("로그 events 목록이 필요합니다")
    return doc["events"]


def _windows(issues: list[dict], rows: list[dict], names: list[str]) -> list[dict]:
    """겹친 관절 경고를 한 구간으로 묶는다. 영상 문맥 여백은 점수에 넣지 않는다."""
    intervals = []
    for issue in issues:
        start = float(issue.get("reference_t_s", rows[issue.get("reference_frame", issue["frame_start"])]["t"]))
        end = float(issue["t_end_s"])
        joint = issue["joint"]
        if issue["kind"] == "joint_observation_stagnation":
            first = issue.get("reference_frame", issue["frame_start"])
            last = issue["frame_end"]
            key = "gripper_cmd" if joint == "gripper" else "q_cmd"
            index = names.index(joint) if key == "q_cmd" else None
            base = rows[first][key] if index is None else rows[first][key][index]
            # 정체 전에 명령이 정지해 있던 시간을 검토 구간에서 제외한다.
            epsilon = 0.1 if index is None else math.radians(0.1)
            for frame in range(first + 1, last + 1):
                value = rows[frame][key] if index is None else rows[frame][key][index]
                if abs(value - base) > epsilon:
                    start = float(rows[frame - 1]["t"])
                    break
        intervals.append((start, end, issue))
    intervals.sort(key=lambda item: (item[0], item[1], item[2]["joint"]))
    merged = []
    for start, end, issue in intervals:
        if merged and start <= merged[-1]["end_s"] + 1e-9:
            merged[-1]["end_s"] = max(end, merged[-1]["end_s"])
            merged[-1]["issues"].append(issue)
        else:
            merged.append({"start_s": start, "end_s": end, "issues": [issue]})
    result = []
    for index, group in enumerate(merged):
        items = group.pop("issues")
        kinds = sorted({i["kind"] for i in items})
        joints = sorted({i["joint"] for i in items if i["kind"] == "joint_observation_stagnation"})
        facts = []
        if joints:
            facts.append("전체 경고 범위에서 명령 변화에 비해 관측 변화가 작은 관절: " + ", ".join(str(LABELS.get(j) or j) for j in joints))
        if "sampling_gap" in kinds:
            facts.append("샘플 사이에 검사 한도를 넘는 기록 공백이 있습니다.")
        if "source_identity_stagnation" in kinds:
            facts.append("원본 측정 시각 또는 순서 번호가 반복됩니다. 수집 주기는 별도 확인이 필요합니다.")
        full_end = group["end_s"]
        confirmations = [i["confirmation_t_s"] for i in items if "confirmation_t_s" in i]
        evidence_end = min(confirmations) if confirmations else group["start_s"]
        # 기록 공백은 양쪽 샘플을 함께 보여야 하므로 축약하지 않는다.
        if "sampling_gap" not in kinds:
            group["end_s"] = min(full_end, max(group["start_s"] + REVIEW_SPAN_S, evidence_end))
        result.append({**group, "full_start_s": group["start_s"], "full_end_s": full_end,
                       "id": f"window-{index + 1}", "focus_s": group["start_s"],
                       "title_ko": "기록 공백 확인" if kinds == ["sampling_gap"] else "명령·관측 불일치 확인",
                       "facts_ko": facts, "joints": joints, "kinds": kinds, "issue_count": len(items)})
    return result


def review_rows(header: dict, rows: list[dict], *, events: list[dict] | None = None) -> dict:
    """결정적 검사 선택 정책. 원인을 확정하거나 로봇 명령을 실행하지 않는다."""
    check = inspect_recording(header, rows)
    if events is not None:
        _validate_events(events, rows[0]["t"], rows[-1]["t"])
    issues = list(check["issues"])
    trace = [{"tool": "inspect_recording", "reason_ko": "명령 변화·관측 정체·샘플 공백에서 검토할 구간을 찾습니다.",
              "result_ko": f"관절 및 수집 기록 경고 {len(issues)}개"}]
    next_checks = []
    freshness = {"status": "not_requested", "fields": [], "missing_frames": len(rows)}
    stagnation = any(i["kind"] == "joint_observation_stagnation" for i in issues)
    if stagnation:
        freshness = inspect_source_identity(rows, min_repeat_s=check["parameters"]["min_hold_s"],
                                            max_gap_s=check["parameters"]["effective_max_gap_s"])
        issues.extend(freshness["issues"])
        trace.append({"tool": "inspect_source_identity", "reason_ko": "위치 반복과 원본 측정 식별자 반복을 구분합니다.",
                      "result_ko": freshness["reason_ko"]})
        if freshness["status"] == "not_assessable" or freshness["missing_frames"]:
            next_checks.append({"id": "collect_source_identity", "title_ko": "센서 측정 시각·순서 번호 확보",
                                "reason_ko": "현재 위치 기록만으로 수집 정지와 실제 정지를 구분할 수 없습니다.",
                                "required_fields": ["source_sample_timestamp_s", "source_sequence"]})
        elif freshness["issues"]:
            next_checks.append({"id": "inspect_collector", "title_ko": "센서 발행 주기와 수집기 수신 기록 확인",
                                "reason_ko": "측정 식별자의 반복은 확인됐지만 정상 저속 발행과 수집 지연을 구분할 근거가 필요합니다.",
                                "required_fields": ["sensor_publish_period_s", "collector_receive_timestamp_s", "communication_errors"]})
        else:
            next_checks.append({"id": "inspect_actuator", "title_ko": "구동기 오류·전류·제어 상태 확인",
                                "reason_ko": "측정 식별자는 변했습니다. 이것만으로 실제 위치 측정의 정상 여부나 기구 걸림을 확정할 수 없습니다.",
                                "required_fields": ["actuator_error_code", "motor_current", "controller_state"]})
    if any(i["kind"] == "sampling_gap" for i in issues):
        next_checks.append({"id": "inspect_capture_gap", "title_ko": "기록 공백의 수집기·시계 상태 확인",
                            "reason_ko": "공백 구간의 로봇 상태는 기록만으로 알 수 없습니다.",
                            "required_fields": ["collector_receive_timestamp_s", "dropped_sample_count", "clock_sync_status"]})
    all_windows = _windows(issues, rows, header["joint_names"])
    # 첫 경고는 유지하고, 나머지는 여러 신호가 겹치는 구간과 긴 구간 순으로 고른다.
    selected = all_windows[:1] + sorted(all_windows[1:], key=lambda w: (-len(w["kinds"]), -(w["full_end_s"] - w["start_s"]), w["start_s"]))[:2]
    for window in selected:
        window["logs"] = []
    if selected:
        if events is None:
            next_checks.append({"id": "collect_aligned_logs", "title_ko": "같은 실행의 시간 정렬된 로그 확보",
                                "reason_ko": "구간 주변 오류 기록이 없어 원인 후보를 더 좁힐 수 없습니다.",
                                "required_fields": ["trajectory_sha256", "time_basis", "events"]})
        else:
            matched = 0
            for window in selected:
                nearby = [e for e in events if window["start_s"] - 1 <= e["t"] <= window["end_s"] + 1]
                nearby.sort(key=lambda e: (e["level"] != "error", e["t"]))
                window["logs"] = nearby[:5]
                window["log_count"] = len(nearby)
                matched += len(nearby)
                if any(e["level"] == "error" for e in nearby):
                    window["facts_ko"].append("같은 구간 주변에 오류 로그가 있습니다. 시간상 인접함은 인과관계의 확정이 아닙니다.")
            trace.append({"tool": "correlate_recorded_logs", "reason_ko": "선택 구간의 앞뒤 1초에 남은 로그를 대조합니다.",
                          "result_ko": f"구간별 인접 로그 합계 {matched}개; 로그만으로 원인을 확정하지 않습니다."})
            if any(e["level"] == "error" for w in selected for e in w["logs"]):
                next_checks.insert(0, {"id": "inspect_reported_error", "title_ko": "기록된 오류 코드의 발생 조건 확인",
                                       "reason_ko": "표시한 오류와 관측 정체가 같은 원인에서 나왔는지 제어기 상태와 대조합니다.",
                                       "required_fields": ["controller_state", "error_code_definition"]})
    else:
        next_checks.append({"id": "broaden_evidence", "title_ko": "실패 시점과 작업 성공 기준 확인",
                            "reason_ko": "현재 검사 항목에서 후보를 찾지 못했습니다. 실행 성공이나 고장 없음을 뜻하지 않습니다.",
                            "required_fields": ["operator_failure_time_s", "task_success_criterion", "task_state"]})
    trace.append({"tool": "select_review_windows", "reason_ko": "겹친 관절 경고를 합치고 첫 구간과 우선 구간을 최대 3개 선택합니다.",
                  "result_ko": f"전체 {len(all_windows)}개 구간 중 {len(selected)}개를 제시합니다. 긴 경고는 처음 근거가 확인되는 구간부터 보여 줍니다."})
    return {"schema": SCHEMA, "policy": POLICY, "status": "review_required" if selected else "no_candidate",
            "cause_status": "undetermined", "cause_note_ko": "표시된 내용은 확인된 기록 현상입니다. 고장 원인은 아직 확정하지 않았습니다.",
            "windows": selected, "trace": trace, "next_checks": next_checks,
            "summary": {"issue_count": len(issues), "window_count": len(all_windows), "selected_count": len(selected),
                        "duration_s": rows[-1]["t"] - rows[0]["t"],
                        "selected_duration_s": sum(w["end_s"] - w["start_s"] for w in selected)},
            "coverage": {"checked_joints": check["coverage"]["checked_joints"],
                         "gripper_checked": check["coverage"]["gripper_checked"],
                         "gripper_missing_frames": len(check["coverage"]["gripper_missing_frames"]),
                         "source_identity_fields": freshness["fields"], "source_identity_missing_frames": freshness["missing_frames"],
                         "observation_resolution": check["coverage"]["observation_resolution"]},
            "parameters": {**check["parameters"], "review_span_s": REVIEW_SPAN_S, "maximum_windows": 3},
            "validation": {"field_validated": False, "human_time_saving": "not_measured", "decision_method": "deterministic_evidence_policy"}}


def compare_rows(before_header: dict, before_rows: list[dict], after_header: dict, after_rows: list[dict]) -> dict:
    before, after = review_rows(before_header, before_rows), review_rows(after_header, after_rows)
    def signature(header: dict, rows: list[dict]) -> str:
        config = {k: header.get(k) for k in ("robot_id", "arm", "joint_names", "units", "gripper_units", "fps", "origin", "recording_check")}
        commands = [{k: row.get(k) for k in ("t", "q_cmd", "gripper_cmd")} for row in rows]
        coverage = [[row.get(k) is not None for k in ("gripper_obs", "source_sample_timestamp_s", "source_sequence")] for row in rows]
        return json.dumps([config, commands, coverage], sort_keys=True, allow_nan=False)
    b, a = before["summary"]["issue_count"], after["summary"]["issue_count"]
    result = {"before_issue_count": b, "after_issue_count": a, "policy": POLICY,
              "before_windows": before["windows"], "after_windows": after["windows"], "cause_status": "undetermined",
              "comparison_scope": "same_commands_and_recorded_coverage",
              "task_and_environment_match": "not_verified", "causal_effect": "not_assessed"}
    if signature(before_header, before_rows) != signature(after_header, after_rows):
        return {**result, "status": "incomparable", "reason_ko": "명령·시각·로봇 구성 또는 검사 가능한 기록 범위가 달라 개선 여부를 비교하지 않았습니다."}
    if a > b:
        status, reason = "more_warnings", "동일 명령·검사 범위에서 경고가 늘었습니다. 작업·환경의 일치는 검증하지 않았습니다."
    elif b and not a:
        status, reason = "warnings_removed_in_scope", "동일 명령·검사 범위에서 경고가 사라졌습니다. 작업·환경의 일치와 작업 성공, 원인 해결은 검증하지 않았습니다."
    elif a < b:
        status, reason = "fewer_warnings", "동일 명령·검사 범위에서 경고가 줄었습니다. 작업·환경의 일치는 검증하지 않았으며 남은 구간을 확인해야 합니다."
    elif a:
        status, reason = "warnings_persist", "동일 명령·검사 범위에서 경고가 남아 있습니다. 구간과 근거를 대조해야 합니다."
    else:
        status, reason = "no_candidate_both", "두 기록 모두 현재 검사 항목에서 후보가 없습니다. 성공 여부는 판정하지 않았습니다."
    return {**result, "status": status, "reason_ko": reason}


def review_path(path: Path, out: Path, *, profile=None, events_path: Path | None = None, after_path: Path | None = None,
                tool_review: dict | None = None) -> dict:
    from .incident_report import validate_output_paths, write_report

    validate_output_paths(out, [p for p in (path, events_path, after_path) if p is not None])
    header, rows, digest = read_input(path)
    if tool_review is None:
        events = load_events(events_path, digest) if events_path else None
        data = review_rows(header, rows, events=events)
    else:
        if tool_review.get("source", {}).get("sha256") != digest:
            raise ValueError("MCP 검사 후 원본 궤적이 변경됐습니다")
        data = dict(tool_review)
        if events_path and data.get("source", {}).get("events_sha256") != sha256_file(events_path):
            raise ValueError("MCP 검사 후 로그가 변경됐습니다")
        if after_path and data.get("comparison", {}).get("after_sha256") != sha256_file(after_path):
            raise ValueError("MCP 검사 후 비교 궤적이 변경됐습니다")
    data["source"] = {"label": f"실패 검토 기록 · 에피소드 {header['source']['episode']}", "sha256": digest,
                      "origin": header["origin"]}
    out.mkdir(parents=True, exist_ok=True)
    try:
        data["media"] = _matching_media(header, profile, out, rows)
    except (OSError, ValueError, TypeError, KeyError) as error:
        data["media"] = {"real": None, "ghost": None, "note_ko": "같은 출처의 영상을 확인하지 못했습니다. 기록 검사는 완료했습니다.",
                         "lookup_error": type(error).__name__}
    data["series"] = {"t": [r["t"] for r in rows], "joints": [
        {"name": name, "label_ko": LABELS.get(name, name), "unit": "deg",
         "command": [math.degrees(r["q_cmd"][j]) for r in rows], "observation": [math.degrees(r["q_obs"][j]) for r in rows]}
        for j, name in enumerate(header["joint_names"])]}
    if events_path:
        data["source"]["events_sha256"] = sha256_file(events_path)
    if after_path and tool_review is None:
        after_header, after_rows, after_digest = read_input(after_path)
        data["comparison"] = {**compare_rows(header, rows, after_header, after_rows), "after_sha256": after_digest}
    write_report(data, out)
    return data
