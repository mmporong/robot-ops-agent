"""저장 기록의 검사 결과에 따라 MCP 조회를 이어 가고 호출 근거를 남긴다."""

from __future__ import annotations

import json
from pathlib import Path

from .incident_review import review_path
from .incident_report import validate_output_paths


async def investigate_via_mcp(path: Path, out: Path, *, profile=None, events_path: Path | None = None,
                              after_path: Path | None = None) -> dict:
    # MCP는 선택 의존성이다. CPU 코어와 일반 CLI는 SDK 없이도 사용할 수 있다.
    from mcp import Client
    from mcp.types import TextContent
    from .diagnostics import DiagnosticToolService, ToolPolicy
    from .mcp_server import create_server

    inputs = [p.expanduser().absolute() for p in (path, events_path, after_path) if p is not None]
    path = inputs[0]
    validate_output_paths(out, inputs)
    out.mkdir(parents=True, exist_ok=True)
    service = DiagnosticToolService(ToolPolicy(allowed_roots=tuple({p.parent.resolve() for p in inputs}),
                                               audit_log=out / "mcp-audit.jsonl"))
    server = create_server(service)
    calls = []
    async with Client(server) as client:
        async def invoke(arguments, reason):
            response = await client.call_tool("investigate_recording", arguments)
            if response.is_error:
                raise ValueError("MCP 기록 검사가 실패했습니다")
            payload = response.structured_content
            if not isinstance(payload, dict):
                text_blocks = [block.text for block in response.content if isinstance(block, TextContent)]
                if not text_blocks:
                    raise ValueError("MCP 검사 응답에 JSON 본문이 없습니다")
                payload = json.loads(text_blocks[0])
            if not isinstance(payload, dict):
                raise ValueError("MCP 검사 결과는 JSON 객체여야 합니다")
            if payload.get("schema") != "robot-ops-incident-review/1":
                raise ValueError("MCP 검사 결과 형식이 올바르지 않습니다")
            calls.append({"tool": "mcp:investigate_recording", "reason_ko": reason,
                          "result_ko": f"검토 구간 {payload['summary']['selected_count']}개 · 원인 미확정",
                          "source_sha256": payload["source"]["sha256"],
                          "input_roles": sorted(arguments)})
            return payload

        arguments = {"path": str(path)}
        data = await invoke(arguments, "실패 기록의 검토 구간과 필요한 근거를 먼저 조회합니다.")
        if events_path:
            arguments["events_path"] = str(events_path.expanduser().absolute())
            reason = ("경고 구간이 있어 같은 실행의 로그를 추가 대조합니다." if data["windows"]
                      else "제공된 로그의 출처를 검사합니다. 검토 구간이 없으면 원인 판정을 보류합니다.")
            data = await invoke(arguments, reason)
        if after_path:
            arguments["after_path"] = str(after_path.expanduser().absolute())
            data = await invoke(arguments, "제공된 변경 후 기록이 같은 명령·검사 범위인지 확인하고 경고를 비교합니다.")
    data["trace"] = calls + data["trace"]
    data["execution"] = {"transport": "in_process_mcp", "tool_calls": len(calls), "selection_method": "deterministic_evidence_policy",
                         "audit_path": "mcp-audit.jsonl", "hardware_accessed": False}
    return review_path(path, out, profile=profile, events_path=events_path, after_path=after_path, tool_review=data)
