"""실제 MCP 호출을 저장해 리포트에서 다시 보여 주는 로컬 시연 기록기."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import IndexSettings
from .diagnostics import DiagnosticToolService, ToolPolicy
from .embedding import HashEmbedder
from .indexer import sync_index
from .replay.artifacts import sha256_file, write_json
from .replay.profile import Profile, expand_path, display_path

SCHEMA = "robot-ops-agent-demo/1"
TOOLS = ("search_documents", "log_tail", "analyze_trajectory")
PUBLIC_DOCS = ("docs/replay-regress.md", "README.md")
QUESTION = "명령은 움직이는데 관측 기록이 멈춘 구간을 찾아줘"
NOTE = "실제 MCP 도구 실행 기록 재생 · 저장된 실측·모델 영상"


def _payload(result: Any) -> Any:
    if result.is_error:
        raise RuntimeError("MCP 도구 실행 실패: " + str(result.content))
    data = result.structured_content
    if data is None:
        data = json.loads(result.content[0].text)
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


async def record(profile: Profile, out: Path) -> dict[str, Any]:
    """공개 관측 동결 사례의 저장 파일만 실제 MCP로 읽고 분석한다."""
    from mcp import Client
    from .mcp_server import create_server
    from .replay.report import latest_replay_results, scrub
    from .replay.scenario_bank import load_scenario, is_public

    scenario = load_scenario(profile.base_dir / "scenarios/so101-observation-freeze/scenario.json")
    if not is_public(scenario, profile.public_exclude_status):
        raise ValueError("공개 사례만 시연할 수 있습니다")
    condition = "teleop_bench_ep0"
    params = scenario["conditions"][condition]["params"]
    source = params["source"].partition(":")[2]
    key = (display_path(expand_path(source, profile.base_dir)), params["episode"])
    replay = latest_replay_results(profile.artifacts_root).get(key)
    if replay is None:
        raise ValueError("저장된 공개 재생 결과가 없습니다")
    trajectory = expand_path(replay["trajectory_path"], profile.base_dir)
    ghost = expand_path(replay["video_path"], profile.base_dir)
    out.mkdir(parents=True, exist_ok=True)
    # 검색은 BM25 키워드 방식이다. 임베딩은 검색에 사용하지 않는다.
    db = out / "index.db"
    sync_index(IndexSettings(root=profile.base_dir, db_path=db, include_dirs=PUBLIC_DOCS), HashEmbedder(32))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    audit_path = out / f"audit-{stamp}.jsonl"
    service = DiagnosticToolService(ToolPolicy(audit_log=audit_path, allowed_roots=(trajectory.parent,)))
    calls = [
        ("search_documents", {"query": "명령 관측 동결 기록", "k": 2}),
        ("log_tail", {"path": str(trajectory), "lines": 2}),
        ("analyze_trajectory", {"path": str(trajectory)}),
    ]
    steps = []
    responses = []
    async with Client(create_server(service, db_path=db), raise_exceptions=True) as client:
        for tool, arguments in calls:
            response = _payload(await client.call_tool(tool, arguments))
            responses.append({"tool": tool, "result": response})
            audit = [json.loads(line) for line in audit_path.read_text().splitlines()]
            receipt = next(r for r in reversed(audit) if r.get("layer") == "mcp" and r["tool"] == tool and r["phase"] == "completed")
            if not receipt.get("ok"):
                raise RuntimeError("MCP 완료 감사가 성공 상태가 아닙니다")
            steps.append({"tool": tool, "arguments": scrub(arguments), "trace_id": receipt["trace_id"],
                          "status": receipt["status"], "duration_ms": receipt["duration_ms"]})
    hits = responses[0]["result"]
    if not isinstance(hits, list) or not hits:
        raise ValueError("검색 근거가 없습니다")
    steps[0]["citations"] = [{"path": path, "sha256": sha256_file(profile.base_dir / path)}
                             for path in dict.fromkeys(hit["path"] for hit in hits)]
    steps[0]["summary_ko"] = f"공개 문서에서 관련 근거 {len(steps[0]['citations'])}건 검색"
    steps[1]["summary_ko"] = "저장된 궤적 로그의 마지막 2프레임 읽기"
    analysis = responses[2]["result"]
    freezes = [i for i in analysis["incidents"] if i["kind"] == "freeze"]
    if not freezes:
        raise ValueError("시연 대상에서 관측 동결이 검출되지 않았습니다")
    incident = max(freezes, key=lambda i: i["n_frames"])
    steps[2]["summary_ko"] = f"관측 동결 {incident['t_start_s']:g}–{incident['t_end_s']:g}초 · {incident['n_frames']}프레임 검출"
    responses_path = out / f"responses-{stamp}.json"
    write_json(responses_path, responses)
    trace = {
        "schema": SCHEMA, "scenario_id": scenario["scenario_id"], "condition": condition,
        "question_ko": QUESTION,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "execution": "actual_mcp_calls", "retrieval_method": "keyword",
        "source": {"trajectory_path": display_path(trajectory), "trajectory_sha256": analysis["source_sha256"],
                   "ghost_sha256": sha256_file(ghost), "run_id": replay["run_id"]},
        "steps": steps,
        "analysis": {"frame_count": analysis["frame_count"], "incident": incident,
                     "max_tcp_mm": (analysis.get("tcp") or {}).get("max_mm")},
        "audit": {"path": display_path(audit_path.resolve()), "sha256": sha256_file(audit_path)},
        "responses": {"path": display_path(responses_path.resolve()), "sha256": sha256_file(responses_path)},
        "note_ko": NOTE,
    }
    write_json(out / "trace.json", scrub(trace))
    return trace


def verified_trace(path: Path, profile: Profile, scenario_id: str, view: dict[str, Any]) -> dict[str, Any]:
    """다른 녹화의 결과나 바뀐 감사 로그를 대표 영상에 섞지 않는다."""
    from .replay.report import scrub

    trace = json.loads(path.read_text(encoding="utf-8"))
    if trace.get("schema") != SCHEMA or trace.get("execution") != "actual_mcp_calls":
        raise ValueError("실제 MCP 실행 기록 형식이 아닙니다")
    if (trace.get("scenario_id"), trace.get("condition")) != (scenario_id, view["condition"]):
        raise ValueError("도구 실행 기록과 영상 사례가 다릅니다")
    if trace["source"]["run_id"] != view["run_id"]:
        raise ValueError("도구 실행 기록과 재생 실행 ID가 다릅니다")
    items = view["provenance"]["items"]
    for label, key in (("궤적(trajectory/1)", "trajectory_sha256"), ("고스트 렌더", "ghost_sha256")):
        item = next(i for i in items if i["label_ko"] == label)
        source = expand_path(item["path"], profile.base_dir)
        if sha256_file(source) != trace["source"][key] or item["sha256"] != trace["source"][key]:
            raise ValueError("분석 원본과 영상 출처 해시가 다릅니다")
    for key in ("audit", "responses"):
        src = expand_path(trace[key]["path"], profile.base_dir)
        if sha256_file(src) != trace[key]["sha256"]:
            raise ValueError(f"{key} 원본 해시가 다릅니다")
    receipts = [json.loads(line) for line in expand_path(trace["audit"]["path"], profile.base_dir).read_text().splitlines()]
    if [s["tool"] for s in trace["steps"]] != list(TOOLS):
        raise ValueError("시연 도구 호출 순서가 다릅니다")
    public_steps = []
    for step in trace["steps"]:
        matched = [r for r in receipts if r.get("layer") == "mcp" and r["trace_id"] == step["trace_id"] and r["tool"] == step["tool"]]
        if [r["phase"] for r in matched] != ["started", "completed"] or not matched[-1].get("ok"):
            raise ValueError("성공한 MCP 시작·완료 감사 쌍이 없습니다")
        if step["status"] != matched[-1]["status"] or step["duration_ms"] != matched[-1]["duration_ms"]:
            raise ValueError("표시할 실행 상태가 감사 기록과 다릅니다")
        if step["arguments"] != scrub(matched[0]["arguments"]):
            raise ValueError("표시할 입력 arguments가 감사 기록과 다릅니다")
        public_steps.append({k: step[k] for k in ("tool", "arguments", "trace_id", "status", "duration_ms")})
    # 지표는 실제 응답과 다시 대조하고, 영상에 묶인 기존 분석과도 일치해야 한다.
    responses = json.loads(expand_path(trace["responses"]["path"], profile.base_dir).read_text())
    if [r["tool"] for r in responses] != list(TOOLS):
        raise ValueError("도구 응답 순서가 다릅니다")
    analysis = responses[2]["result"]
    if analysis["source_sha256"] != trace["source"]["trajectory_sha256"]:
        raise ValueError("분석 응답의 source_sha256이 궤적 원본과 다릅니다")
    primary = max((i for i in analysis["incidents"] if i["kind"] == "freeze"), key=lambda i: i["n_frames"])
    expected = {"frame_count": analysis["frame_count"], "incident": primary, "max_tcp_mm": (analysis.get("tcp") or {}).get("max_mm")}
    if trace["analysis"] != expected or any(primary[k] != view["incident"][k] for k in ("t_start_s", "t_end_s", "n_frames")):
        raise ValueError("도구 응답·재생 영상의 동결 구간이 다릅니다")
    hits = responses[0]["result"]
    if not isinstance(hits, list) or not hits:
        raise ValueError("검색 근거가 없습니다")
    paths = list(dict.fromkeys(hit["path"] for hit in hits))
    if any(p not in PUBLIC_DOCS for p in paths):
        raise ValueError("공개 문서 허용 목록 밖의 검색 근거입니다")
    citations = [{"path": p, "sha256": sha256_file(profile.base_dir / p)} for p in paths]
    public_steps[0].update(citations=citations, summary_ko=f"공개 문서에서 관련 근거 {len(paths)}건 검색")
    public_steps[1]["summary_ko"] = "저장된 궤적 로그의 마지막 2프레임 읽기"
    public_steps[2]["summary_ko"] = f"관측 동결 {primary['t_start_s']:g}–{primary['t_end_s']:g}초 · {primary['n_frames']}프레임 검출"
    return {"schema": SCHEMA, "execution": "actual_mcp_calls", "scenario_id": scenario_id,
            "condition": view["condition"], "question_ko": QUESTION,
            "recorded_at": matched[-1]["timestamp"], "retrieval_method": "keyword",
            "steps": public_steps, "analysis": expected, "note_ko": NOTE}


def main() -> None:
    import argparse
    from .replay.profile import load_profile
    parser = argparse.ArgumentParser(description="Robot Ops Agent의 실제 MCP 실행 시연 기록")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    profile = load_profile(args.profile)
    out = args.out or profile.base_dir / ".local/agent-demo"
    trace = asyncio.run(record(profile, out))
    print(json.dumps({"trace": display_path((out / "trace.json").resolve()), "tools": [s["tool"] for s in trace["steps"]],
                      "analysis": trace["analysis"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
