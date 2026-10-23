"""새 컵 접촉 실행 세 건과 MCP 재검사를 한 흐름으로 묶는다."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .replay.artifacts import sha256_file, write_json
from .replay.doctor import check_profile
from .replay.profile import Profile, display_path, expand_path, load_profile
from .replay.regress import run_regress

STAGES = ("normal", "fault", "recovery")
LABELS = {"normal": "정상 위치", "fault": "컵 위치 5mm 오차", "recovery": "같은 오차에서 복구"}
EARLY_PHASES = {"RESET", "REORIENT_ABOVE", "PREGRASP_ABOVE", "ALIGN_MIDDLE", "APPROACH"}
ASSETS = Path(__file__).parent / "replay/report_assets"


async def inspect_via_mcp(path: Path, allowed: Path, audit: Path, *, tool_name: str = "analyze_simulation") -> dict:
    from mcp import Client
    from .diagnostics import DiagnosticToolService, ToolPolicy
    from .mcp_server import create_server

    service = DiagnosticToolService(ToolPolicy(audit_log=audit, allowed_roots=(allowed,)))
    schemas = {"analyze_simulation": "robot-ops-simulation-inspection/1", "analyze_cup_alignment": "robot-ops-cup-alignment-analysis/1"}
    if tool_name not in schemas:
        raise ValueError("지원하지 않는 시뮬 검사 도구입니다")
    try:
        async with Client(create_server(service), raise_exceptions=False) as client:
            result = await client.call_tool(tool_name, {"path": str(path)})
    except Exception as exc:
        # SDK의 TaskGroup 예외도 CLI의 구조화된 실패 응답으로 전달한다.
        raise ValueError(f"MCP 검사 요청을 완료하지 못했습니다: {type(exc).__name__}") from exc
    if result.is_error:
        raise ValueError("시뮬 결과 MCP 검사에 실패했습니다. 감사 기록을 확인해 주세요.")
    data = result.structured_content
    if data is None:
        data = json.loads(result.content[0].text)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if not isinstance(data, dict) or data.get("schema") != schemas[tool_name]:
        raise ValueError("MCP 검사 응답 형식이 올바르지 않습니다")
    return data


def _cell(run_root: Path, stage: str) -> tuple[dict, Path]:
    record = json.loads((run_root / f"{stage}.json").read_text(encoding="utf-8"))
    cells = record["conditions"][stage]["cells"]
    if len(cells) != 1:
        raise ValueError(f"{stage}: 정상 종료한 새 실행 한 건이 필요합니다")
    cell = cells[0]
    status = cell.get("status")
    if status != "ok":
        detail = {
            "timeout": "timeout: 실행 시간 제한을 초과했습니다",
            "infra": "infra: 실행 환경 또는 시뮬레이터 오류입니다",
        }.get(status, "실행 기록이 정상 종료 상태가 아닙니다")
        raise ValueError(f"{stage}: {detail}. 작업 성공·실패는 판정하지 않았습니다.")
    identifier = cell.get("run_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", identifier):
        raise ValueError("실행 ID 형식이 올바르지 않습니다")
    return cell, run_root / "artifacts/runs" / identifier


def _verify_expected(stage: str, analysis: dict) -> None:
    passed = analysis["stored_outcome"]["task_pass"]
    incidents = analysis["premature_contact_check"]["issue_count"]
    expected = (passed and incidents == 0) if stage == "normal" else (
        not passed and incidents > 0 if stage == "fault" else
        passed and incidents > 0 and analysis["runtime"]["recovered"]
    )
    if not expected:
        raise ValueError(f"{stage}: 기대한 정상→조기 접촉→복구 흐름이 검증되지 않았습니다")
    if not analysis["consistency"]["stored_event_agrees_with_recheck"]:
        raise ValueError(f"{stage}: 원시 접촉력과 저장된 접촉 이벤트가 다릅니다")
    if analysis["consistency"].get("stop_reason_agrees_with_recheck") is not True:
        raise ValueError(f"{stage}: 접촉 검사와 종료 사유가 다릅니다")
    if analysis["runtime"].get("time_axis_agrees") is not True:
        raise ValueError(f"{stage}: 물리 시간 진행을 확인하지 못했습니다")
    if stage == "recovery":
        runtime = analysis["runtime"]
        if runtime.get("final_attempt_premature_contact_count") != 0 or not isinstance(runtime.get("final_attempt"), int):
            raise ValueError("recovery: 마지막 시도의 조기 접촉 여부를 확인하지 못했습니다")
        final = runtime["final_attempt"]
        if not any(issue["attempt"] < final for issue in analysis["premature_contact_check"]["issues"]):
            raise ValueError("recovery: 이전 시도와 새 시도를 구분할 수 없습니다")


def build_report(run_root: Path, out: Path) -> dict:
    """세 실행을 실제 MCP로 읽고, 해당 실행의 영상·수치만 확인 화면에 연결한다."""
    run_root, out = run_root.resolve(), out.resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("새 확인 화면에는 비어 있는 출력 경로가 필요합니다")
    out.mkdir(parents=True, exist_ok=True)
    stages, analyses, provenance, gpu_checks = [], {}, {}, {}
    for stage in STAGES:
        cell, directory = _cell(run_root, stage)
        result_path = directory / "isaac/result.json"
        analysis = asyncio.run(inspect_via_mcp(result_path, run_root / "artifacts/runs", out / "mcp-audit.jsonl"))
        _verify_expected(stage, analysis)
        expected_verdict = "pass" if analysis["stored_outcome"]["task_pass"] else "fail"
        if cell.get("verdict") != expected_verdict:
            raise ValueError("실행 목록과 MCP 검사 결과의 성공 판정이 다릅니다")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        plan_path = result_path.with_name("plan.json")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        for key, path in (("result_sha256", result_path), ("plan_sha256", plan_path)):
            if sha256_file(path) != analysis["source"][key]:
                raise ValueError("검사 뒤 원본 파일이 바뀌었습니다")
        response = json.loads((directory / "run.response.json").read_text(encoding="utf-8"))
        video = directory / "cup_contact.mp4"
        if video.is_symlink() or not video.is_file() or sha256_file(video) != (response.get("video") or {}).get("sha256"):
            raise ValueError("실행 응답과 녹화 파일 해시가 다릅니다")
        if expand_path(cell["video_path"]) != video or expand_path(response["result_path"]) != result_path:
            raise ValueError("실행 목록과 원본·영상 경로가 다릅니다")
        # 기록 주기는 영상 10fps, 접촉력 120Hz다. 시계열은 물리 실행 샘플을 보존한다.
        samples = result["samples"]
        base_z = float(plan["config"]["cup_center_m"][2])
        compact = [{"t_s": s["time_s"], "phase": s["phase"], "force_n": max(s["contact_force_n"]),
                    "preclose": s["phase"] in EARLY_PHASES,
                    "cup_z_mm": (s["cup_position_m"][2] - base_z) * 1000,
                    "attempt": s["attempt"]} for s in samples]
        if any(not math.isfinite(s[key]) for s in compact for key in ("t_s", "force_n", "cup_z_mm")):
            raise ValueError("표시할 물리 샘플이 유한한 값이 아닙니다")
        media_dir = out / "media"
        media_dir.mkdir(exist_ok=True)
        shutil.copy2(video, media_dir / f"{stage}.mp4")
        issues = analysis["premature_contact_check"]["issues"]
        stages.append({"id": stage, "label_ko": LABELS[stage],
                       "description_ko": {"normal": "컵을 계획한 위치에 놓고 실행했습니다.",
                                          "fault": "계획은 유지하고 실제 컵 위치만 5mm 옮겼습니다.",
                                          "recovery": "같은 위치 오차에서 기존 후퇴·재접근 복구를 켰습니다."}[stage],
                       "video": f"media/{stage}.mp4", "verdict": cell["verdict"],
                       "sim_time_s": analysis["runtime"]["sim_time_s"], "sample_count": len(samples),
                       "event_t_s": issues[0]["time_s"] if issues else None,
                       "force_threshold_n": analysis["premature_contact_check"]["threshold_n"],
                       "samples": compact, "analysis": analysis,
                       "source": {**analysis["source"], "run_id": cell["run_id"], "video_sha256": sha256_file(video)}})
        analyses[stage] = analysis
        record = json.loads((run_root / f"{stage}.json").read_text(encoding="utf-8"))
        provenance[stage] = record["provenance"]
        gpu_checks[stage] = record.get("gpu_check")
    audit = [json.loads(line) for line in (out / "mcp-audit.jsonl").read_text(encoding="utf-8").splitlines()]
    started = [r for r in audit if r.get("layer") == "mcp" and r.get("phase") == "started"]
    completed = [r for r in audit if r.get("layer") == "mcp" and r.get("phase") == "completed"]
    expected_paths = {str(_cell(run_root, stage)[1] / "isaac/result.json") for stage in STAGES}
    if (len(started) != 3 or len(completed) != 3 or any(not r.get("ok") for r in completed)
        or {r["trace_id"] for r in started} != {r["trace_id"] for r in completed}
        or {r.get("arguments", {}).get("path") for r in started} != expected_paths):
        raise ValueError("세 실행에 대응하는 MCP 완료 감사 기록이 필요합니다")
    payload = {"schema": "robot-ops-simulation-proof/1", "title_ko": "컵을 닫기 전에 건드리는 문제를 검사합니다",
               "stages": stages, "scope": {
                   "basis_ko": "왼팔 강체 패드 접촉 시뮬레이션 · 복구는 시뮬레이터 정답 좌표 사용",
                   "execution_ko": "물리 시뮬레이션에서 생성한 실행 녹화입니다. 화면은 저장된 실행을 재생합니다.",
                   "limitations_ko": ["실물 로봇·카메라 인식 성능을 검증한 결과가 아닙니다.",
                                      "영상은 10fps, 접촉력은 120Hz입니다. 작은 접촉은 수치로 확인합니다.",
                                      "고정된 실행·검사 흐름이며 LLM 판단이나 자동 코드 수정은 없습니다."]},
               "tool_trace": {"tool": "analyze_simulation", "transport": "mcp_in_memory", "calls": 3,
                              "trace_ids": [r["trace_id"] for r in completed]}}
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", r"\u003c")
    (out / "index.html").write_text(f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'"><title>컵 접촉 시뮬레이션 검사</title><link rel="icon" href="data:,"><link rel="stylesheet" href="sim-proof.css"><script src="sim-proof.js" defer></script></head><body><main id="sim-proof-app"></main><script type="application/json" id="sim-proof-data">{encoded}</script><noscript>검사 결과 JSON을 확인해 주세요.</noscript></body></html>''', encoding="utf-8")
    for name in ("sim-proof.js", "sim-proof.css"):
        shutil.copy2(ASSETS / name, out / name)
    write_json(out / "proof.json", payload)
    evidence = {"schema": "robot-ops-simulation-workflow/1", "execution": "actual_simulator_runs_and_mcp_calls",
                "llm_used": False, "automatic_code_edit": False, "scope": payload["scope"],
                "stages": [{key: value for key, value in s.items() if key not in ("samples", "video")} for s in stages],
                "provenance": provenance, "gpu_checks": gpu_checks,
                "audit_sha256": sha256_file(out / "mcp-audit.jsonl")}
    write_json(out / "evidence.json", evidence)
    return evidence


def run_fresh(profile: Profile, out: Path, *, source_commit: str | None = None) -> dict:
    """깨끗한 커밋 사본에서 정상·실패·복구를 실행하며 각 결과를 MCP로 확인한다."""
    if profile.source_repo is None:
        raise ValueError("프로파일에 원천 저장소가 필요합니다")
    out = out.resolve()
    if out.is_relative_to(profile.source_repo.resolve()):
        raise ValueError("원천 저장소 안에 실행 결과를 쓸 수 없습니다")
    snapshot = out / "source"
    if snapshot.exists() or (out / "artifacts").exists():
        raise ValueError("새 실행에는 비어 있는 출력 경로가 필요합니다")
    rev = source_commit or profile.project.get("pinned_commit")
    if not isinstance(rev, str) or not rev:
        raise ValueError("실행할 source_commit 또는 pinned_commit이 필요합니다")
    commit = subprocess.run(["git", "--no-optional-locks", "-C", str(profile.source_repo), "rev-parse", "--verify", "--end-of-options", rev + "^{commit}"],
                            check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--shared", "--no-checkout", "--", str(profile.source_repo), str(snapshot)], check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(snapshot), "checkout", "--detach", commit], check=True, capture_output=True, timeout=60)
    profile = replace(profile, source_repo=snapshot, artifacts_root=out / "artifacts",
                      project={**profile.project, "source_repo": str(snapshot), "pinned_commit": commit})
    check = check_profile(profile, task="cup_contact")
    write_json(out / "runtime-prerequisites.json", check)
    if check["status"] != "ready":
        raise ValueError("시뮬 실행 전제 조건이 부족합니다. 출력 경로의 runtime-prerequisites.json을 확인해 주세요.")
    scenario = json.loads((profile.base_dir / "scenarios/bimanual-cup-premature-contact/scenario.json").read_text(encoding="utf-8"))
    scenario = copy.deepcopy(scenario)
    original = scenario["conditions"]
    scenario["conditions"] = {"normal": copy.deepcopy(original["A"]), "fault": copy.deepcopy(original["A"]), "recovery": copy.deepcopy(original["B"])}
    scenario["grid"] = {"spawn_x_offset_mm": [0], "spawn_y_offset_mm": [0, -5], "repeat_points": 0, "repeats": 0}
    for stage in STAGES:
        point = (0., 0.) if stage == "normal" else (0., -5.)
        record = run_regress(profile, scenario, conditions=[stage], record_cells={stage: [point]})
        write_json(out / f"{stage}.json", record)
        _, directory = _cell(out, stage)
        analysis = asyncio.run(inspect_via_mcp(directory / "isaac/result.json", out / "artifacts/runs", out / "execution-audit.jsonl"))
        _verify_expected(stage, analysis)
        write_json(out / f"{stage}-analysis.json", analysis)
    return build_report(out, out / "report")


def command(args: argparse.Namespace) -> int:
    out = (args.out or Path(".local/simulation-check") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")).expanduser()
    try:
        if args.from_runs:
            evidence = build_report(args.from_runs.expanduser(), out)
            report = out.resolve() / "index.html"
        else:
            if not args.profile:
                raise ValueError("새 실행에는 --profile이 필요합니다")
            evidence = run_fresh(load_profile(args.profile), out, source_commit=args.source_commit)
            report = out.resolve() / "report/index.html"
        print(json.dumps({"report": display_path(report), "stages": [{"id": s["id"], "verdict": s["verdict"]} for s in evidence["stages"]]}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="정상→컵 위치 오차→복구를 새 물리 실행과 MCP 검사로 확인합니다")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--from-runs", type=Path, help="생성한 세 실행 결과를 다시 검사하고 화면만 생성")
    parser.add_argument("--out", type=Path)
    return command(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
