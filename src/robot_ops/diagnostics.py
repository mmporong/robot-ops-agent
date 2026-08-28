from __future__ import annotations

import hashlib
import os
import re
import selectors
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from .audit import AuditLogger, AuditSpan
from .secrets import redact_text


ROS_NAME_RE = re.compile(r"^/[A-Za-z0-9_/]+$")
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    audit_log: Path
    allowed_roots: tuple[Path, ...]
    allowed_journal_units: frozenset[str] = frozenset()
    command_timeout_seconds: float = 5.0
    max_output_chars: int = 20_000
    audit_preview_chars: int = 500
    ros2_binary: str | None = None

    def normalized(self) -> "ToolPolicy":
        roots = tuple(root.expanduser().resolve() for root in self.allowed_roots)
        if not roots:
            raise ValueError("allowed_roots는 하나 이상이어야 합니다")
        if self.command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds는 0보다 커야 합니다")
        if self.max_output_chars <= 0:
            raise ValueError("max_output_chars는 1 이상이어야 합니다")
        ros2_binary = self.ros2_binary or shutil.which("ros2")
        return ToolPolicy(
            audit_log=self.audit_log.expanduser().resolve(),
            allowed_roots=roots,
            allowed_journal_units=self.allowed_journal_units,
            command_timeout_seconds=self.command_timeout_seconds,
            max_output_chars=self.max_output_chars,
            audit_preview_chars=self.audit_preview_chars,
            ros2_binary=ros2_binary,
        )


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float
    truncated: bool = False


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProcessResult: ...


class SubprocessRunner:
    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        max_output_bytes: int = 80_000,
    ) -> None:
        self.environment = dict(environment or os.environ)
        self.max_output_bytes = max_output_bytes

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProcessResult:
        started = time.perf_counter()
        process = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=self.environment,
        )
        assert process.stdout is not None and process.stderr is not None
        streams = {"stdout": bytearray(), "stderr": bytearray()}
        captured_bytes = 0
        truncated = False
        timed_out = False
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = started + timeout_seconds
        termination_deadline: float | None = None
        hard_deadline: float | None = None

        try:
            while selector.get_map():
                now = time.perf_counter()
                if not timed_out and now >= deadline:
                    timed_out = True
                    process.terminate()
                    termination_deadline = now + 0.5
                    hard_deadline = now + 1.0
                elif (
                    timed_out
                    and termination_deadline is not None
                    and now >= termination_deadline
                    and process.poll() is None
                ):
                    process.kill()
                    termination_deadline = None
                if timed_out and hard_deadline is not None and now >= hard_deadline:
                    if process.poll() is None:
                        process.kill()
                    truncated = True
                    break

                events = selector.select(timeout=0.05)
                for key, _ in events:
                    chunk = os.read(key.fd, 65_536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = self.max_output_bytes - captured_bytes
                    if remaining > 0:
                        kept = chunk[:remaining]
                        streams[str(key.data)].extend(kept)
                        captured_bytes += len(kept)
                    if len(chunk) > max(remaining, 0):
                        truncated = True
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()

        returncode = process.wait()
        stderr = streams["stderr"].decode("utf-8", errors="replace")
        if timed_out and not stderr:
            stderr = f"timeout after {timeout_seconds:.1f}s"
        return ProcessResult(
            returncode=124 if timed_out else returncode,
            stdout=streams["stdout"].decode("utf-8", errors="replace"),
            stderr=stderr,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            truncated=truncated,
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    trace_id: str
    tool: str
    ok: bool
    status: str
    output: str
    error: str | None
    duration_ms: float
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ToolRejected(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DiagnosticToolService:
    """Read-only diagnostic tool boundary.

    Every external command is built from fixed argv templates and executed with
    shell=False. Unknown tools are rejected before any process is created.
    """

    TOOL_DESCRIPTIONS = {
        "ros_nodes": "ROS 2 노드 이름을 조회합니다.",
        "ros_topics": "ROS 2 토픽과 타입을 조회합니다.",
        "ros_services": "ROS 2 서비스와 타입을 조회합니다. 서비스를 호출하지 않습니다.",
        "ros_topic_sample": "지정한 ROS 2 토픽 메시지 한 건을 읽습니다.",
        "tf_snapshot": "동적 TF 메시지 한 건을 읽습니다.",
        "diagnostics_snapshot": "diagnostics 메시지 한 건을 읽습니다.",
        "rosbag_info": "허용된 경로의 rosbag/MCAP 메타데이터를 조회합니다.",
        "journal_tail": "허용된 systemd unit의 최근 로그만 조회합니다.",
        "log_tail": "허용된 경로의 텍스트 로그 마지막 줄을 읽습니다.",
    }

    def __init__(self, policy: ToolPolicy, runner: CommandRunner | None = None) -> None:
        self.policy = policy.normalized()
        self.audit_logger = AuditLogger(self.policy.audit_log)
        self.runner = runner or SubprocessRunner(
            _ros_environment(self.policy.ros2_binary),
            max_output_bytes=self.policy.max_output_chars * 4,
        )
        self._handlers: dict[str, Callable[[Mapping[str, object]], ProcessResult]] = {
            "ros_nodes": self._ros_nodes,
            "ros_topics": self._ros_topics,
            "ros_services": self._ros_services,
            "ros_topic_sample": self._ros_topic_sample,
            "tf_snapshot": lambda _: self._ros_topic_sample({"topic": "/tf"}),
            "diagnostics_snapshot": lambda _: self._ros_topic_sample({"topic": "/diagnostics"}),
            "rosbag_info": self._rosbag_info,
            "journal_tail": self._journal_tail,
            "log_tail": self._log_tail,
        }

    def list_tools(self) -> list[dict[str, str]]:
        return [
            {"name": name, "description": description}
            for name, description in self.TOOL_DESCRIPTIONS.items()
        ]

    def execute(
        self,
        tool: str,
        arguments: Mapping[str, object] | None = None,
        *,
        trace_id: str | None = None,
    ) -> ToolResult:
        arguments = arguments or {}
        span = self.audit_logger.begin(tool, arguments, trace_id=trace_id, layer="core")
        trace_id = span.trace_id
        started = time.perf_counter()
        handler = self._handlers.get(tool)
        if handler is None:
            result = self._rejected(
                trace_id,
                tool,
                "tool_not_allowed: 조회 전용 allowlist에 없는 도구입니다",
                started,
            )
            self._finish_audit(span, result)
            return result

        try:
            process = handler(arguments)
        except ToolRejected as error:
            result = self._rejected(trace_id, tool, error.reason, started)
            self._finish_audit(span, result)
            return result
        except Exception as error:
            process = ProcessResult(
                returncode=1,
                stdout="",
                stderr=f"internal_error: {type(error).__name__}",
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )

        combined = process.stdout
        if process.stderr:
            combined = f"{combined}\n[stderr]\n{process.stderr}" if combined else process.stderr
        redacted = redact_text(combined)
        truncated = process.truncated or len(redacted) > self.policy.max_output_chars
        output = redacted[: self.policy.max_output_chars]
        ok = process.returncode == 0
        status = "ok" if ok else ("timeout" if process.returncode == 124 else "command_error")
        result = ToolResult(
            trace_id=trace_id,
            tool=tool,
            ok=ok,
            status=status,
            output=output,
            error=None if ok else f"process_returncode={process.returncode}",
            duration_ms=round(process.duration_ms, 3),
            truncated=truncated,
        )
        self._finish_audit(span, result)
        return result

    def _rejected(
        self,
        trace_id: str,
        tool: str,
        reason: str,
        started: float,
    ) -> ToolResult:
        result = ToolResult(
            trace_id=trace_id,
            tool=tool,
            ok=False,
            status="rejected",
            output="",
            error=reason,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            truncated=False,
        )
        return result

    def _require_ros2(self) -> str:
        if not self.policy.ros2_binary:
            raise ToolRejected("ros2_unavailable: ros2 실행 파일을 찾지 못했습니다")
        return self.policy.ros2_binary

    def _run(self, argv: Sequence[str]) -> ProcessResult:
        return self.runner.run(argv, timeout_seconds=self.policy.command_timeout_seconds)

    def _ros_nodes(self, arguments: Mapping[str, object]) -> ProcessResult:
        _require_no_arguments(arguments)
        return self._run((self._require_ros2(), "node", "list"))

    def _ros_topics(self, arguments: Mapping[str, object]) -> ProcessResult:
        _require_no_arguments(arguments)
        return self._run((self._require_ros2(), "topic", "list", "-t"))

    def _ros_services(self, arguments: Mapping[str, object]) -> ProcessResult:
        _require_no_arguments(arguments)
        return self._run((self._require_ros2(), "service", "list", "-t"))

    def _ros_topic_sample(self, arguments: Mapping[str, object]) -> ProcessResult:
        _require_only(arguments, {"topic"})
        topic = _require_string(arguments, "topic", max_length=256)
        if not ROS_NAME_RE.fullmatch(topic) or "//" in topic:
            raise ToolRejected("invalid_topic: 절대 ROS 이름만 허용합니다")
        return self._run((self._require_ros2(), "topic", "echo", topic, "--once"))

    def _rosbag_info(self, arguments: Mapping[str, object]) -> ProcessResult:
        _require_only(arguments, {"path"})
        path = self._resolve_allowed_path(
            _require_string(arguments, "path", max_length=4096)
        )
        if not path.exists():
            raise ToolRejected("path_not_found: rosbag 경로가 없습니다")
        if path.is_file() and path.suffix.lower() not in {".db3", ".mcap", ".yaml"}:
            raise ToolRejected("invalid_bag_path: db3, mcap, yaml 또는 bag 디렉터리만 허용합니다")
        return self._run((self._require_ros2(), "bag", "info", str(path)))

    def _journal_tail(self, arguments: Mapping[str, object]) -> ProcessResult:
        _require_only(arguments, {"unit", "lines"})
        unit = _require_string(arguments, "unit", max_length=256)
        if not UNIT_RE.fullmatch(unit) or unit not in self.policy.allowed_journal_units:
            raise ToolRejected("unit_not_allowed: 허용된 systemd unit이 아닙니다")
        lines = _bounded_integer(arguments.get("lines", 100), minimum=1, maximum=500, name="lines")
        return self._run(
            (
                "/usr/bin/journalctl",
                "--unit",
                unit,
                "--lines",
                str(lines),
                "--no-pager",
                "--output",
                "short-iso",
            )
        )

    def _log_tail(self, arguments: Mapping[str, object]) -> ProcessResult:
        _require_only(arguments, {"path", "lines"})
        path = self._resolve_allowed_path(
            _require_string(arguments, "path", max_length=4096)
        )
        lines = _bounded_integer(arguments.get("lines", 100), minimum=1, maximum=500, name="lines")
        if not path.is_file():
            raise ToolRejected("path_not_found: 로그 파일이 없습니다")
        if path.suffix.lower() not in {".json", ".jsonl", ".log", ".md", ".txt", ".yaml", ".yml"}:
            raise ToolRejected("invalid_log_path: 허용된 텍스트 로그 확장자가 아닙니다")
        started = time.perf_counter()
        try:
            content, truncated = _tail_text_file(
                path,
                lines=lines,
                max_bytes=self.policy.max_output_chars * 4,
            )
        except OSError as error:
            return ProcessResult(
                returncode=1,
                stdout="",
                stderr=f"read_error: {type(error).__name__}",
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
        return ProcessResult(
            returncode=0,
            stdout=content,
            stderr="",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            truncated=truncated,
        )

    def _resolve_allowed_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser().resolve()
        for root in self.policy.allowed_roots:
            try:
                candidate.relative_to(root)
                return candidate
            except ValueError:
                continue
        raise ToolRejected("path_not_allowed: 허용된 읽기 루트 밖의 경로입니다")

    def _finish_audit(self, span: AuditSpan, result: ToolResult) -> None:
        redacted_output = redact_text(result.output)
        span.finish(
            {
                "layer": "core",
                "status": result.status,
                "ok": result.ok,
                "error": result.error,
                "duration_ms": result.duration_ms,
                "truncated": result.truncated,
                "output_sha256": hashlib.sha256(
                    redacted_output.encode("utf-8")
                ).hexdigest(),
                "output_preview": redacted_output[: self.policy.audit_preview_chars],
            }
        )


def _prepend_environment_path(environment: dict[str, str], name: str, value: str) -> None:
    current = environment.get(name, "")
    parts = [part for part in current.split(os.pathsep) if part]
    if value not in parts:
        parts.insert(0, value)
    environment[name] = os.pathsep.join(parts)


def _ros_environment(ros2_binary: str | None) -> dict[str, str]:
    """Build the minimal sourced-setup equivalent without invoking a shell."""

    environment = os.environ.copy()
    if not ros2_binary:
        return environment
    binary = Path(ros2_binary).resolve()
    if binary.parent.name != "bin":
        return environment
    prefix = binary.parent.parent
    _prepend_environment_path(environment, "PATH", str(prefix / "bin"))
    _prepend_environment_path(environment, "AMENT_PREFIX_PATH", str(prefix))
    _prepend_environment_path(environment, "CMAKE_PREFIX_PATH", str(prefix))
    _prepend_environment_path(environment, "LD_LIBRARY_PATH", str(prefix / "lib"))
    for site_packages in sorted((prefix / "lib").glob("python*/site-packages")):
        _prepend_environment_path(environment, "PYTHONPATH", str(site_packages))
    environment.setdefault("ROS_DISTRO", prefix.name)
    return environment


def _require_no_arguments(arguments: Mapping[str, object]) -> None:
    if arguments:
        raise ToolRejected("unexpected_arguments: 이 도구는 인자를 받지 않습니다")


def _tail_text_file(path: Path, *, lines: int, max_bytes: int) -> tuple[str, bool]:
    """Read the final lines while keeping captured bytes within a fixed bound."""

    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        captured = b""
        while position > 0 and len(captured) < max_bytes:
            read_size = min(65_536, position, max_bytes - len(captured))
            position -= read_size
            stream.seek(position)
            captured = stream.read(read_size) + captured
            if captured.count(b"\n") > lines:
                break
    selected = b"\n".join(captured.splitlines()[-lines:])
    truncated = position > 0
    return selected.decode("utf-8", errors="replace"), truncated


def _require_only(arguments: Mapping[str, object], allowed: set[str]) -> None:
    unexpected = sorted(set(arguments) - allowed)
    if unexpected:
        raise ToolRejected(f"unexpected_arguments: 허용하지 않는 인자 {','.join(unexpected)}")


def _require_string(
    arguments: Mapping[str, object], name: str, *, max_length: int
) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolRejected(f"invalid_argument: {name}은 비어 있지 않은 문자열이어야 합니다")
    if len(value) > max_length:
        raise ToolRejected(
            f"invalid_argument: {name}은 {max_length}자 이하여야 합니다"
        )
    return value


def _bounded_integer(value: object, *, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ToolRejected(f"invalid_argument: {name}은 {minimum}..{maximum} 정수여야 합니다")
    return value
