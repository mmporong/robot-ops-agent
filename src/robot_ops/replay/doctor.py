"""재생·회귀 실행 전제 조건을 부수효과 없이 진단한다."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from robot_ops.secrets import redact_text

from . import VERBS
from .profile import Profile, display_path

SCHEMA = "robot-ops-doctor/1"
PROBE_TIMEOUT_S = 5
_DETAIL_LIMIT = 500


def _check(check_id: str, status: str, message_ko: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"id": check_id, "status": status, "message_ko": message_ko}
    if details:
        result["details"] = details
    return result


def _safe_output(value: str | None) -> str:
    return redact_text(value or "").strip()[:_DETAIL_LIMIT]


def _probe(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
            env=env,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _nearest_existing_ancestor(path: Path) -> Path | None:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return candidate


def _check_adapter(profile: Profile) -> dict[str, Any]:
    entry = profile.adapter_entry
    if entry.is_file():
        return _check(
            "adapter_entry", "ok", "어댑터 진입 파일을 찾았습니다.", path=display_path(entry)
        )
    return _check(
        "adapter_entry", "blocked", "어댑터 진입 파일이 없습니다.", path=display_path(entry)
    )


def _check_interpreters(profile: Profile, tasks: list[str]) -> dict[str, Any]:
    missing: list[dict[str, str]] = []
    checked: dict[str, str] = {}
    for task in tasks:
        for verb in VERBS:
            if verb not in profile.tasks[task]:
                continue
            executable = Path(profile.interpreter_for(task, verb)[0])
            key = f"{task}.{verb}"
            checked[key] = display_path(executable)
            if not executable.is_file() or not os.access(executable, os.X_OK):
                missing.append({"mapping": key, "path": display_path(executable)})
    if missing:
        return _check(
            "interpreters",
            "blocked",
            "선택한 과제에 필요한 인터프리터가 없거나 실행할 수 없습니다.",
            missing=missing,
        )
    return _check(
        "interpreters",
        "ok",
        "선택한 과제의 실행 가능한 인터프리터 파일을 찾았습니다.",
        mappings=checked,
    )


def _check_disk(profile: Profile) -> dict[str, Any]:
    ancestor = _nearest_existing_ancestor(profile.artifacts_root)
    if ancestor is None:
        return _check(
            "artifact_disk",
            "blocked",
            "산출물 경로의 디스크 여유 공간을 확인할 기존 상위 경로가 없습니다.",
            artifacts_root=display_path(profile.artifacts_root),
        )
    if not ancestor.is_dir():
        return _check(
            "artifact_disk",
            "blocked",
            "산출물 경로의 가장 가까운 기존 상위 경로가 디렉터리가 아닙니다.",
            artifacts_root=display_path(profile.artifacts_root),
            checked_path=display_path(ancestor),
        )
    try:
        free_bytes = shutil.disk_usage(ancestor).free
        minimum_gb = float(profile.limits.get("min_free_disk_gb", 0))
    except (OSError, TypeError, ValueError) as error:
        return _check(
            "artifact_disk",
            "blocked",
            "산출물 디스크 여유 공간을 확인하지 못했습니다.",
            path=display_path(ancestor),
            error=_safe_output(str(error)),
        )
    free_gb = free_bytes / (1024**3)
    details = {
        "artifacts_root": display_path(profile.artifacts_root),
        "checked_path": display_path(ancestor),
        "free_gb": round(free_gb, 3),
        "required_gb": minimum_gb,
    }
    if free_gb < minimum_gb:
        return _check(
            "artifact_disk",
            "blocked",
            "산출물 디스크 여유 공간이 최소 기준보다 적습니다.",
            **details,
        )
    return _check(
        "artifact_disk",
        "ok",
        "산출물 디스크 여유 공간이 최소 기준을 충족합니다.",
        **details,
    )


def _git(profile: Profile, *args: str) -> subprocess.CompletedProcess[str] | None:
    assert profile.source_repo is not None
    return _probe(["git", "--no-optional-locks", "-C", str(profile.source_repo), *args])


def _check_source_repo(profile: Profile) -> dict[str, Any]:
    repo = profile.source_repo
    if repo is None:
        return _check(
            "source_repo", "ok", "프로파일에 원천 저장소 확인이 설정되지 않았습니다."
        )
    if not repo.is_dir():
        return _check(
            "source_repo",
            "blocked",
            "설정된 원천 저장소를 찾을 수 없습니다.",
            path=display_path(repo),
        )
    head = _git(profile, "rev-parse", "HEAD")
    status = _git(profile, "status", "--porcelain")
    if head is None or status is None or head.returncode != 0 or status.returncode != 0:
        stderr = "\n".join(
            _safe_output(result.stderr)
            for result in (head, status)
            if result is not None and result.stderr
        )
        return _check(
            "source_repo",
            "blocked",
            "원천 저장소의 HEAD 또는 작업 트리 상태를 읽지 못했습니다.",
            path=display_path(repo),
            error=stderr,
        )
    commit = head.stdout.strip()
    if status.stdout.strip():
        return _check(
            "source_repo",
            "blocked",
            "원천 저장소에 커밋되지 않은 변경이 있습니다.",
            path=display_path(repo),
            commit=commit,
        )
    pinned = profile.project.get("pinned_commit")
    if pinned and not commit.startswith(str(pinned)):
        return _check(
            "source_repo",
            "warning",
            "원천 저장소 HEAD가 pinned_commit과 다릅니다.",
            path=display_path(repo),
            commit=commit,
            pinned_commit=str(pinned),
        )
    if not pinned:
        return _check(
            "source_repo",
            "ok",
            "원천 저장소가 깨끗합니다. pinned_commit은 설정되지 않았습니다.",
            path=display_path(repo),
            commit=commit,
            pinned_commit=None,
        )
    return _check(
        "source_repo",
        "ok",
        "원천 저장소가 깨끗하고 기준 커밋과 일치합니다.",
        path=display_path(repo),
        commit=commit,
        pinned_commit=str(pinned) if pinned else None,
    )


def _systemd_state() -> tuple[bool, dict[str, Any]]:
    missing = [
        name
        for name in ("systemd-run", "systemctl", "timeout")
        if shutil.which(name) is None
    ]
    if missing:
        return False, {"missing_commands": missing}
    result = _probe(["systemctl", "--user", "is-system-running"])
    if result is None:
        return False, {"error": "systemctl 사용자 상태 조회 실패"}
    state = result.stdout.strip()
    if state not in ("running", "degraded"):
        return False, {"state": state or None, "error": _safe_output(result.stderr)}
    return True, {"state": state}


def _check_gpu_probe() -> dict[str, Any]:
    if shutil.which("nvidia-smi") is None:
        return _check(
            "gpu_probe", "blocked", "GPU 과제에 필요한 nvidia-smi를 찾을 수 없습니다."
        )
    result = _probe(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
    if result is None or result.returncode != 0:
        error = (
            _safe_output("\n".join(part for part in (result.stdout, result.stderr) if part))
            if result is not None
            else "probe failed"
        )
        return _check(
            "gpu_probe",
            "blocked",
            "nvidia-smi로 GPU 메모리 사용량을 읽지 못했습니다.",
            error=error,
        )
    try:
        values = [int(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    except ValueError:
        values = []
    if not values or any(value < 0 for value in values):
        return _check(
            "gpu_probe",
            "blocked",
            "nvidia-smi가 유효한 GPU 메모리 사용량을 반환하지 않았습니다.",
            output=_safe_output(result.stdout),
        )
    return _check(
        "gpu_probe",
        "ok",
        "GPU 메모리 사용량 프로브가 정상 데이터를 반환했습니다.",
        gpu_count=len(values),
    )


def _check_isolation(profile: Profile, *, gpu_required: bool) -> dict[str, Any]:
    mode = profile.isolation
    if mode == "pgid":
        if gpu_required:
            return _check(
                "isolation",
                "blocked",
                "GPU 과제는 PGID 격리에서 MemoryMax를 보장할 수 없습니다.",
                configured=mode,
            )
        return _check(
            "isolation", "ok", "CPU 과제를 PGID 격리로 실행할 수 있습니다.", configured=mode
        )

    available, details = _systemd_state()
    if available:
        return _check(
            "isolation",
            "ok",
            "systemd 사용자 scope와 MemoryMax 적용에 필요한 도구가 준비되었습니다.",
            configured=mode,
            **details,
        )
    if mode == "auto" and not gpu_required:
        return _check(
            "isolation",
            "warning",
            "systemd 사용자 scope를 쓸 수 없어 CPU 실행은 PGID 격리로 전환됩니다.",
            configured=mode,
            fallback="pgid",
            **details,
        )
    reason = (
        "GPU 과제는 systemd 사용자 scope 없이 MemoryMax를 보장할 수 없습니다."
        if gpu_required
        else "isolation=systemd이지만 systemd 사용자 scope를 쓸 수 없습니다."
    )
    return _check("isolation", "blocked", reason, configured=mode, **details)


def check_profile(profile: Profile, *, task: str | None = None) -> dict[str, Any]:
    """프로파일의 재생·회귀 런타임 전제 조건을 읽기 전용으로 확인한다."""
    if task is not None and task not in profile.tasks:
        raise ValueError(f"프로파일에 없는 과제입니다: {task}")
    tasks = [task] if task is not None else list(profile.tasks)
    gpu_tasks = {str(value) for value in profile.limits.get("gpu_tasks", ())}
    gpu_required = any(name in gpu_tasks for name in tasks)

    checks = [
        _check_adapter(profile),
        _check_interpreters(profile, tasks),
        _check_disk(profile),
        _check_source_repo(profile),
        _check_isolation(profile, gpu_required=gpu_required),
    ]
    if gpu_required:
        checks.append(_check_gpu_probe())

    summary = {
        name: sum(item["status"] == name for item in checks)
        for name in ("ok", "warning", "blocked")
    }
    ready = summary["blocked"] == 0
    return {
        "schema": SCHEMA,
        "status": "ready" if ready else "blocked",
        "scope": "replay_runtime_prerequisites",
        "profile": profile.id,
        "tasks": tasks,
        "checks": checks,
        "summary": summary,
        "message_ko": (
            "재생·회귀 실행 전제 조건이 준비되었습니다."
            if ready
            else "재생·회귀 실행 전에 해결해야 할 차단 항목이 있습니다."
        ),
        "boundaries": {
            "verified": (
                "프로파일에 선언된 파일·실행기·저장소·디스크·격리·GPU 프로브 전제 조건"
            ),
            "not_verified": "패키지 import, 시뮬레이션 실행 성공, 로봇 동작, 성능 수치",
        },
    }
