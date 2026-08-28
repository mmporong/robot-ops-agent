from __future__ import annotations

import contextvars
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .secrets import key_is_sensitive, redact_text


_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "robot_ops_trace_id", default=None
)


class AuditUnavailable(RuntimeError):
    """Raised before execution when the append-only audit sink is unavailable."""


def current_trace_id() -> str | None:
    return _TRACE_ID.get()


def set_trace_id(trace_id: str):
    return _TRACE_ID.set(trace_id)


def reset_trace_id(token: contextvars.Token[str | None]) -> None:
    _TRACE_ID.reset(token)


def sanitize_arguments(arguments: Mapping[str, object]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for key, value in arguments.items():
        if key_is_sensitive(key):
            sanitized[key] = "[REDACTED]"
        elif isinstance(value, str):
            sanitized[key] = redact_text(value)[:500]
        elif isinstance(value, (bool, int, float)) or value is None:
            sanitized[key] = value
        else:
            sanitized[key] = f"<{type(value).__name__}>"
    return sanitized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class AuditSpan:
    logger: "AuditLogger"
    file_descriptor: int
    trace_id: str
    tool: str

    def finish(self, record: Mapping[str, object]) -> None:
        try:
            self.logger._append_to_open_file(
                self.file_descriptor,
                {
                    "timestamp": _utc_now(),
                    "phase": "completed",
                    "trace_id": self.trace_id,
                    "tool": self.tool,
                    **record,
                },
            )
        except OSError as error:
            raise AuditUnavailable("감사 로그 완료 기록에 실패했습니다") from error
        finally:
            os.close(self.file_descriptor)


class AuditLogger:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def begin(
        self,
        tool: str,
        arguments: Mapping[str, object],
        *,
        trace_id: str | None = None,
        layer: str = "core",
    ) -> AuditSpan:
        trace_id = trace_id or current_trace_id() or uuid.uuid4().hex
        tool = redact_text(str(tool))[:200] or "<empty>"
        file_descriptor: int | None = None
        try:
            parent_existed = self.path.parent.exists()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not parent_existed:
                os.chmod(self.path.parent, 0o700)
            file_descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            os.fchmod(file_descriptor, 0o600)
            self._append_to_open_file(
                file_descriptor,
                {
                    "timestamp": _utc_now(),
                    "phase": "started",
                    "trace_id": trace_id,
                    "layer": layer,
                    "tool": tool,
                    "arguments": sanitize_arguments(arguments),
                },
            )
        except OSError as error:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            raise AuditUnavailable("감사 로그 시작 기록에 실패해 실행을 차단했습니다") from error
        if file_descriptor is None:
            raise AuditUnavailable("감사 로그 파일을 열지 못해 실행을 차단했습니다")
        return AuditSpan(self, file_descriptor, trace_id, tool)

    @staticmethod
    def _append_to_open_file(file_descriptor: int, record: Mapping[str, object]) -> None:
        payload = (
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(file_descriptor, view)
            if written <= 0:
                raise OSError("감사 로그에 데이터를 쓰지 못했습니다")
            view = view[written:]
        os.fsync(file_descriptor)
