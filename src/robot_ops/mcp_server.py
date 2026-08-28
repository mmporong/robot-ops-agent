from __future__ import annotations

import os
import time
import uuid
from typing import Any, Mapping
from pathlib import Path
from mcp.server import MCPServer
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver.exceptions import ToolError

from .audit import reset_trace_id, set_trace_id
from .diagnostics import DiagnosticToolService, ToolPolicy
from .search import MAX_QUERY_CHARS, keyword_search


REGISTERED_TOOL_NAMES = frozenset(
    {
        "search_documents",
        "ros_nodes",
        "ros_topics",
        "ros_services",
        "ros_topic_sample",
        "tf_snapshot",
        "diagnostics_snapshot",
        "rosbag_info",
        "journal_tail",
        "log_tail",
    }
)


class ToolAuditMiddleware:
    """Audit every MCP tools/call attempt before SDK lookup or validation."""

    def __init__(self, service: DiagnosticToolService) -> None:
        self.audit_logger = service.audit_logger

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)

        params = ctx.params if isinstance(ctx.params, Mapping) else {}
        raw_name = params.get("name", "<invalid-tool-name>")
        tool = str(raw_name)
        raw_arguments = params.get("arguments", {})
        if isinstance(raw_arguments, Mapping):
            arguments = {str(key): value for key, value in raw_arguments.items()}
        else:
            arguments = {"_arguments_type": type(raw_arguments).__name__}

        trace_id = uuid.uuid4().hex
        span = self.audit_logger.begin(
            tool,
            arguments,
            trace_id=trace_id,
            layer="mcp",
        )
        token = set_trace_id(trace_id)
        started = time.perf_counter()
        try:
            result = await call_next(ctx)
        except BaseException as error:
            span.finish(
                {
                    "layer": "mcp",
                    "status": "request_error",
                    "ok": False,
                    "error_type": type(error).__name__,
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
                }
            )
            raise
        else:
            is_error = isinstance(result, Mapping) and bool(
                result.get("isError", result.get("is_error", False))
            )
            completed_record: dict[str, object] = {
                "layer": "mcp",
                "status": "tool_error" if is_error else "ok",
                "ok": not is_error,
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            }
            if is_error:
                completed_record["rejection_reason"] = (
                    "registered_tool_error_or_schema_rejection"
                    if tool in REGISTERED_TOOL_NAMES
                    else "tool_not_registered"
                )
            span.finish(
                completed_record
            )
            return result
        finally:
            reset_trace_id(token)


def _tool_result_or_error(
    service: DiagnosticToolService,
    tool: str,
    arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    result = service.execute(tool, arguments)
    if not result.ok:
        raise ToolError(result.error or result.status)
    return result.to_dict()


def create_server(
    service: DiagnosticToolService,
    *,
    db_path: Path | None = None,
) -> MCPServer:
    server = MCPServer(
        "Robot Ops Agent",
        middleware=(ToolAuditMiddleware(service),),
    )

    @server.tool()
    def search_documents(query: str, k: int = 5) -> list[dict[str, object]]:
        """로컬 로봇 문서 인덱스에서 근거 청크를 조회합니다."""
        if db_path is None or not db_path.is_file():
            raise ToolError("index_unavailable: ROBOT_OPS_DB_PATH의 DB를 찾지 못했습니다")
        if not query.strip():
            raise ToolError("invalid_query: query가 비어 있습니다")
        if len(query) > MAX_QUERY_CHARS:
            raise ToolError(
                f"invalid_query: query는 {MAX_QUERY_CHARS}자 이하여야 합니다"
            )
        if not 1 <= k <= 20:
            raise ToolError("invalid_k: k는 1..20이어야 합니다")
        return [
            hit.to_dict(include_text=True)
            for hit in keyword_search(db_path, query, k=k)
        ]

    @server.tool()
    def ros_nodes() -> dict[str, object]:
        """ROS 2 노드 이름을 조회합니다."""
        return _tool_result_or_error(service, "ros_nodes")

    @server.tool()
    def ros_topics() -> dict[str, object]:
        """ROS 2 토픽과 타입을 조회합니다."""
        return _tool_result_or_error(service, "ros_topics")

    @server.tool()
    def ros_services() -> dict[str, object]:
        """ROS 2 서비스와 타입을 조회합니다. 서비스를 호출하지 않습니다."""
        return _tool_result_or_error(service, "ros_services")

    @server.tool()
    def ros_topic_sample(topic: str) -> dict[str, object]:
        """지정한 ROS 2 토픽 메시지 한 건을 읽습니다. 발행 기능은 없습니다."""
        return _tool_result_or_error(service, "ros_topic_sample", {"topic": topic})

    @server.tool()
    def tf_snapshot() -> dict[str, object]:
        """동적 TF 메시지 한 건을 읽습니다."""
        return _tool_result_or_error(service, "tf_snapshot")

    @server.tool()
    def diagnostics_snapshot() -> dict[str, object]:
        """ROS diagnostics 메시지 한 건을 읽습니다."""
        return _tool_result_or_error(service, "diagnostics_snapshot")

    @server.tool()
    def rosbag_info(path: str) -> dict[str, object]:
        """허용된 경로의 rosbag 또는 MCAP 메타데이터를 조회합니다."""
        return _tool_result_or_error(service, "rosbag_info", {"path": path})

    @server.tool()
    def journal_tail(unit: str, lines: int = 100) -> dict[str, object]:
        """allowlist에 등록된 systemd unit의 최근 로그를 조회합니다."""
        return _tool_result_or_error(
            service, "journal_tail", {"unit": unit, "lines": lines}
        )

    @server.tool()
    def log_tail(path: str, lines: int = 100) -> dict[str, object]:
        """허용된 읽기 루트에 있는 텍스트 로그의 마지막 줄을 조회합니다."""
        return _tool_result_or_error(
            service, "log_tail", {"path": path, "lines": lines}
        )

    return server


def _service_from_environment() -> tuple[DiagnosticToolService, Path | None]:
    raw_roots = os.environ.get("ROBOT_OPS_ALLOWED_ROOTS", str(Path.cwd()))
    roots = tuple(Path(value) for value in raw_roots.split(os.pathsep) if value)
    raw_units = os.environ.get("ROBOT_OPS_JOURNAL_UNITS", "")
    units = frozenset(value.strip() for value in raw_units.split(",") if value.strip())
    audit_log = Path(
        os.environ.get("ROBOT_OPS_AUDIT_LOG", str(Path.cwd() / ".local" / "tool_audit.jsonl"))
    )
    db_value = os.environ.get("ROBOT_OPS_DB_PATH")
    db_path = Path(db_value).expanduser().resolve() if db_value else None
    service = DiagnosticToolService(
        ToolPolicy(
            audit_log=audit_log,
            allowed_roots=roots,
            allowed_journal_units=units,
        )
    )
    return service, db_path


def main() -> None:
    service, db_path = _service_from_environment()
    server = create_server(service, db_path=db_path)
    server.run()


if __name__ == "__main__":
    main()
