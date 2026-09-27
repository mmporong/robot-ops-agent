"""어댑터 subprocess 호출 (`robot-ops-adapter/1`).

- 호출: `<interp> <entry> <verb> --task T --request req.json --response resp.json --workdir D`
- 응답 파일만 진실이다. stdout/stderr는 `<verb>.log`로만 남긴다.
- 종료 코드 0/2/3은 응답 파일이 없을 때만 참고한다(그 밖 = crash → infra_error).
- 모든 subprocess 환경에 `PYTHONDONTWRITEBYTECODE=1`과 run 표식 `ROBOT_OPS_RUN_ID`를 넣는다.
- 하드웨어 가드: `run` 응답이 ok이면 응답과 결과 파일의 `hardware_accessed`가 정확히 False여야
  한다. 응답이 ok가 아니어도 키가 있으면 False여야 한다. 어기면 `HardwareGuardError`.
- 자원 격리
  * systemd: `systemd-run --user --scope --unit robot-ops-<run_id> -p MemoryMax=<m>
    timeout --kill-after=<k> <t> ...` 뒤 `systemctl --user stop`, 남은 unit 0 확인.
  * pgid(대체 경로): `start_new_session=True` + `killpg`, 이어서 `/proc/*/environ`에서
    이 run 표식을 가진 자기 소유 프로세스를 찾아 종료(setsid로 세션을 벗어난 손자 포함).
    명령줄 패턴 매칭(`pkill -f`)은 쓰지 않는다. MemoryMax는 이 경로에서 적용되지 않는다.
- 단일 실행: `run_lock()`이 profile `lock_file`에 `fcntl.flock`(LOCK_EX|LOCK_NB)을 건다.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import ADAPTER_PROTOCOL, VERBS
from .artifacts import write_json
from .profile import Profile
from .schema import SchemaError, validate_adapter_response

RUN_MARKER_ENV = "ROBOT_OPS_RUN_ID"
EXIT_CODES = {0: "ok", 2: "unsupported", 3: "infra_error"}
TIMEOUT_EXIT_CODES = (124, 137)
_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


class AdapterError(RuntimeError):
    pass


class VerbRejected(AdapterError):
    pass


class AllowlistRejected(AdapterError):
    pass


class HardwareGuardError(AdapterError):
    pass


class LockBusy(AdapterError):
    pass


@dataclass
class AdapterOutcome:
    verb: str
    task: str
    run_id: str
    status: str  # ok | unsupported | infra_error | timeout
    response: dict[str, Any]
    exit_code: int | None
    timed_out: bool
    wall_s: float
    workdir: Path
    log_path: Path
    isolation: str
    error: str | None = None


def check_verb(verb: str) -> None:
    if verb not in VERBS:
        raise VerbRejected(f"지원하지 않는 동사입니다: {verb} (convert|run|judge만)")


def _normalize_team_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise AllowlistRejected(f"팀 파일 경로가 올바르지 않습니다: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise AllowlistRejected(f"팀 파일은 저장소 상대경로여야 합니다: {value}")
    return path.as_posix()


def check_allowlist(request: dict[str, Any], profile: Profile) -> None:
    """요청의 `scripts`·`imports`가 profile allowlist 안에 있는지 확인한다."""
    for key, allowed in (
        ("scripts", profile.allowlist_scripts),
        ("imports", profile.allowlist_imports),
    ):
        values = request.get(key, [])
        if not isinstance(values, list):
            raise AllowlistRejected(f"request.{key}는 목록이어야 합니다")
        allowed_set = {PurePosixPath(a).as_posix() for a in allowed}
        for value in values:
            normalized = _normalize_team_path(value)
            if normalized not in allowed_set:
                raise AllowlistRejected(f"allowlist 밖 {key} 요청 거부: {value}")


def adapter_env(run_id: str, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env[RUN_MARKER_ENV] = run_id
    return env


def build_adapter_argv(
    profile: Profile,
    verb: str,
    task: str,
    request_path: Path,
    response_path: Path,
    workdir: Path,
) -> list[str]:
    check_verb(verb)
    return [
        *profile.interpreter_for(task, verb),
        str(profile.adapter_entry),
        verb,
        "--task",
        task,
        "--request",
        str(request_path),
        "--response",
        str(response_path),
        "--workdir",
        str(workdir),
    ]


def scope_unit(run_id: str) -> str:
    return f"robot-ops-{run_id}"


def build_scope_argv(
    argv: list[str],
    *,
    run_id: str,
    memory_max: str,
    timeout_s: float,
    kill_after_s: float,
) -> list[str]:
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--unit",
        scope_unit(run_id),
        "-p",
        f"MemoryMax={memory_max}",
        "timeout",
        f"--kill-after={_fmt_seconds(kill_after_s)}",
        _fmt_seconds(timeout_s),
        *argv,
    ]


def _fmt_seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def systemd_available() -> bool:
    if not shutil.which("systemd-run") or not shutil.which("systemctl"):
        return False
    try:
        state = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return state in ("running", "degraded")


def list_scope_units(run_id: str) -> list[str]:
    result = subprocess.run(
        ["systemctl", "--user", "list-units", f"{scope_unit(run_id)}.scope", "--no-legend", "--plain"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def stop_scope(run_id: str) -> None:
    subprocess.run(
        ["systemctl", "--user", "stop", f"{scope_unit(run_id)}.scope"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def gpu_memory_used_mib() -> int | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
        return sum(int(line.strip()) for line in out.splitlines() if line.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def find_marked_processes(run_id: str) -> list[int]:
    """`ROBOT_OPS_RUN_ID=<run_id>` 환경을 가진 자기 소유 프로세스 pid 목록."""
    token = f"{RUN_MARKER_ENV}={run_id}".encode()
    uid = os.getuid()
    me = os.getpid()
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if token in environ.split(b"\0"):
            pids.append(pid)
    return pids


def sweep_marked_processes(run_id: str, grace_s: float = 1.0) -> list[int]:
    pids = find_marked_processes(run_id)
    for pid in pids:
        _signal(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and find_marked_processes(run_id):
        time.sleep(0.05)
    for pid in find_marked_processes(run_id):
        _signal(pid, signal.SIGKILL)
    return pids


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


@contextmanager
def run_lock(lock_file: Path) -> Iterator[Path]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusy(f"다른 실행이 잠금을 쥐고 있습니다: {lock_file}") from None
        yield lock_file
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _resolve_path(value: Any, workdir: Path) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else workdir / path


def check_hardware_guard(verb: str, response: dict[str, Any], workdir: Path) -> None:
    if verb != "run":
        return
    if response.get("status") == "ok":
        if response.get("hardware_accessed") is not False:
            raise HardwareGuardError(
                f"run 응답 hardware_accessed가 False가 아닙니다: {response.get('hardware_accessed')!r}"
            )
        result_path = _resolve_path(response.get("result_path"), workdir)
        if result_path is None or not result_path.is_file():
            raise HardwareGuardError("결과 파일이 없어 hardware_accessed를 확인할 수 없습니다")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HardwareGuardError(f"결과 파일을 읽을 수 없습니다: {error}") from None
        if not isinstance(result, dict) or result.get("hardware_accessed") is not False:
            value = result.get("hardware_accessed") if isinstance(result, dict) else None
            raise HardwareGuardError(f"결과 파일 hardware_accessed가 False가 아닙니다: {value!r}")
    elif "hardware_accessed" in response and response["hardware_accessed"] is not False:
        raise HardwareGuardError("비정상 응답이 hardware_accessed를 False가 아닌 값으로 보고했습니다")


def _resolve_isolation(profile: Profile, verb: str, isolation: str | None) -> str:
    mode = isolation or profile.isolation
    if mode == "pgid":
        return "pgid"
    if mode == "systemd":
        if not systemd_available():
            raise AdapterError("isolation=systemd이지만 systemd --user를 쓸 수 없습니다")
        return "systemd"
    if mode != "auto":
        raise AdapterError(f"알 수 없는 isolation: {mode}")
    return "systemd" if verb == "run" and systemd_available() else "pgid"


def call_adapter(
    profile: Profile,
    verb: str,
    task: str,
    request: dict[str, Any],
    workdir: Path,
    *,
    run_id: str,
    timeout_s: float | None = None,
    isolation: str | None = None,
    kill_after_s: float | None = None,
) -> AdapterOutcome:
    check_verb(verb)
    if not _RUN_ID.match(run_id):
        raise AdapterError(f"run_id 형식이 올바르지 않습니다: {run_id!r}")
    check_allowlist(request, profile)

    workdir.mkdir(parents=True, exist_ok=True)
    request_path = workdir / f"{verb}.request.json"
    response_path = workdir / f"{verb}.response.json"
    log_path = workdir / f"{verb}.log"
    if response_path.exists():
        response_path.unlink()
    write_json(request_path, request)

    argv = build_adapter_argv(profile, verb, task, request_path, response_path, workdir)
    timeout = float(timeout_s if timeout_s is not None else profile.limits.get("timeout_s", 600))
    kill_after = float(kill_after_s if kill_after_s is not None else profile.limits.get("kill_after_s", 30))
    mode = _resolve_isolation(profile, verb, isolation)
    env = adapter_env(run_id)

    started = time.monotonic()
    timed_out = False
    with log_path.open("wb") as log:
        if mode == "systemd":
            scope_argv = build_scope_argv(
                argv,
                run_id=run_id,
                memory_max=str(profile.limits.get("memory_max", "20G")),
                timeout_s=timeout,
                kill_after_s=kill_after,
            )
            proc = subprocess.Popen(
                scope_argv, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True
            )
            try:
                exit_code: int | None = proc.wait(timeout=timeout + kill_after + 30)
            except subprocess.TimeoutExpired:
                _killpg(proc.pid, signal.SIGKILL)
                exit_code = proc.wait()
                timed_out = True
            timed_out = timed_out or exit_code in TIMEOUT_EXIT_CODES
            stop_scope(run_id)
            leftover = list_scope_units(run_id)
            sweep_marked_processes(run_id)
            if leftover:
                raise AdapterError(f"scope가 남았습니다: {leftover}")
        else:
            proc = subprocess.Popen(
                argv, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True
            )
            try:
                exit_code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _killpg(proc.pid, signal.SIGTERM)
                try:
                    exit_code = proc.wait(timeout=kill_after)
                except subprocess.TimeoutExpired:
                    _killpg(proc.pid, signal.SIGKILL)
                    exit_code = proc.wait()
            # 부모가 끝난 뒤 남은 자식·손자는 run 표식으로만 찾는다(pgid 번호 재사용 위험 회피).
            sweep_marked_processes(run_id)
    wall_s = time.monotonic() - started

    response: dict[str, Any] = {}
    status: str
    error: str | None = None
    if response_path.is_file():
        try:
            loaded = json.loads(response_path.read_text(encoding="utf-8"))
            response = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError as exc:
            error = f"응답 JSON 파싱 실패: {exc}"
    if response:
        # 하드웨어 가드는 스키마 검사보다 먼저: 키 누락도 중단 사유다.
        check_hardware_guard(verb, response, workdir)
    if timed_out:
        status = "timeout"
        error = error or f"timeout {timeout:g}s 초과"
    elif response:
        try:
            validate_adapter_response(response, verb, ADAPTER_PROTOCOL)
            status = str(response["status"])
            error = response.get("error")
        except SchemaError as exc:
            status = "infra_error"
            error = f"응답 스키마 위반: {exc}"
    else:
        status = "infra_error"
        mapped = EXIT_CODES.get(exit_code) if exit_code is not None else None
        error = error or f"응답 파일 없음 (exit={exit_code}, {mapped or 'crash'})"

    return AdapterOutcome(
        verb=verb,
        task=task,
        run_id=run_id,
        status=status,
        response=response,
        exit_code=exit_code,
        timed_out=timed_out,
        wall_s=wall_s,
        workdir=workdir,
        log_path=log_path,
        isolation=mode,
        error=error,
    )
