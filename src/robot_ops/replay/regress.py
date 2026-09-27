"""`robot-ops regress`(격자 × 조건 A/B)와 `robot-ops replay`(에피소드 재생) 오케스트레이션.

regress 흐름
1. `lock_file` flock → 디스크 여유 확인 → 팀 저장소 상태 확인(dirty면 거부, 커밋 불일치는 경고).
2. 조건마다 격자 25점을 `run` → `judge`로 실행한다. 이어서 첫 조건에서 반복 점 × 반복 횟수를
   실행해 판정 일치율을 잰다.
3. run마다 `<root>/runs/<run_id>/manifest.json`을 쓰고 `delete_patterns` 파일을 지운다. 남은 크기
   (MP4 제외)가 `[limits] per_run_keep_mb`를 넘으면 결과 `warnings`에 남긴다(실패로 치지 않음).
4. 하드웨어 가드 위반(`HardwareGuardError`)은 잡지 않고 그대로 올려 regress 전체를 즉시 멈춘다.
   이때도 그 run의 manifest 기록·`delete_patterns` 삭제는 finally에서 먼저 한다.
5. `<root>/runs/<regress_id>/regress_result.json`을 쓴다.

GPU 확인: profile `[limits] gpu_tasks`에 든 과제는 run마다 실행 전 `nvidia-smi memory.used`를
기준으로 잡고, 실행·정리 뒤 `gpu_settle_s`(기본 20초) 동안 1초 간격으로 다시 읽어
기준 + `gpu_tolerance_mib`(기본 200) 이내로 돌아와야 다음 셀로 간다. 못 돌아오거나 값을 읽을
수 없으면 `GpuMemoryNotReleased`로 regress 전체를 멈춘다. 요약은 결과의 `gpu_check`에 남는다.

녹화 패스(`record_cells`): 지정한 조건·격자점만 `record=true`로 다시 실행한다. 반복(판정 일치율)은
돌리지 않으며 결과에 `mode: "record"`를 남긴다. 격자 판정은 본 패스(`mode: "grid"`) 결과를 쓴다.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .adapter import AdapterOutcome, call_adapter, gpu_memory_used_mib, run_lock
from .artifacts import (
    check_free_disk,
    delete_recorded_matches,
    sha256_file,
    write_json,
    write_manifest,
)
from .divergence import compute_divergence, read_trajectory
from .grid import (
    grid_points,
    select_repeat_points,
    summarize_condition,
    verdict_agreement,
)
from .profile import Profile, display_path
from .scenario_bank import apply_status
from .schema import REGRESS_SCHEMA, validate_regress_result

REPLAY_SCHEMA = "robot-ops-replay/1"
TIMESTAMP_KEYS = ("created_at", "finished_at", "wall_s")
GPU_POLL_S = 1.0


class RegressRefused(RuntimeError):
    pass


class GpuMemoryNotReleased(RegressRefused):
    pass


def display_tree(doc: Any) -> Any:
    """기록용: 문자열 값 중 홈·`/data/<user>` 아래 절대경로를 `~`·`/data/$USER`로 바꾼다."""
    if isinstance(doc, dict):
        return {k: display_tree(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [display_tree(v) for v in doc]
    if isinstance(doc, str) and doc.startswith("/"):
        return display_path(doc)
    return doc


def wait_gpu_release(
    before: int,
    reader: Callable[[], int | None],
    *,
    tolerance_mib: float,
    settle_s: float,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = GPU_POLL_S,
) -> tuple[int | None, float]:
    """실행 뒤 GPU 메모리가 기준 + 허용치 안으로 돌아올 때까지 최대 settle_s 동안 읽는다."""
    waited = 0.0
    after = reader()
    while (after is None or after > before + tolerance_mib) and waited < settle_s:
        sleep(poll_s)
        waited += poll_s
        after = reader()
    return after, waited


def parse_record_cells(text: str, conditions: Sequence[str]) -> dict[str, list[tuple[float, float]]]:
    """`x,y;x,y` 또는 조건 접두 `A:x,y;B:x,y` → 조건별 격자점 목록.

    접두가 없는 점은 선택된 모든 조건에 적용한다.
    """
    cells: dict[str, list[tuple[float, float]]] = {}
    for raw in text.split(";"):
        item = raw.strip()
        if not item:
            continue
        cond_part, sep, coords = item.rpartition(":")
        targets = [cond_part.strip()] if sep else list(conditions)
        for cond in targets:
            if cond not in conditions:
                raise ValueError(f"--record-cells 조건 {cond}이 선택된 조건 {list(conditions)}에 없습니다")
        parts = [p.strip() for p in coords.split(",")]
        if len(parts) != 2:
            raise ValueError(f"--record-cells 항목은 x,y 형식이어야 합니다: {item}")
        point = (float(parts[0]), float(parts[1]))
        for cond in targets:
            if point not in cells.setdefault(cond, []):
                cells[cond].append(point)
    if not cells:
        raise ValueError("--record-cells가 비어 있습니다")
    return cells


def new_run_id(prefix: str = "") -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return f"{prefix}{stamp}-{secrets.token_hex(3)}"


def _mm_token(value: float) -> str:
    return f"{value:+g}".replace("+", "p").replace("-", "m")


def _point_token(point: tuple[float, float]) -> str:
    return f"x{_mm_token(point[0])}_y{_mm_token(point[1])}"


def _git(repo: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def source_repo_state(profile: Profile) -> dict[str, Any]:
    """팀 저장소 HEAD·dirty 확인(읽기 전용, optional lock 없이)."""
    pinned = profile.project.get("pinned_commit")
    state: dict[str, Any] = {"pinned_commit": pinned, "commit": None, "warnings": []}
    repo = profile.source_repo
    if repo is None or not (repo / ".git").exists():
        state["warnings"].append("팀 저장소를 찾지 못해 커밋을 확인하지 못했습니다")
        return state
    head = _git(repo, "rev-parse", "HEAD")
    state["commit"] = head.strip() if head else None
    status = _git(repo, "status", "--porcelain")
    if status is None:
        state["warnings"].append("팀 저장소 status를 읽지 못했습니다")
    elif status.strip():
        raise RegressRefused(f"팀 저장소에 커밋되지 않은 변경이 있어 실행을 거부합니다: {display_path(repo)}")
    if pinned and state["commit"] and not state["commit"].startswith(str(pinned)):
        state["warnings"].append(f"팀 저장소 HEAD {state['commit'][:7]}가 pinned_commit {pinned}와 다릅니다")
    return state


def _collect_tool_sha(target: dict[str, Any], response: dict[str, Any]) -> None:
    value = response.get("tool_sha256")
    if isinstance(value, dict):
        target.update({str(k): v for k, v in value.items()})
    elif isinstance(value, str):
        target[str(response.get("task", "tool"))] = value


def _finish_run_dir(
    profile: Profile, run_dir: Path, run_id: str, grid_offset: dict[str, Any] | None
) -> str | None:
    """manifest 기록 → `delete_patterns` 삭제 → 남은 크기(MP4 제외)가 `per_run_keep_mb`를 넘으면 경고 문구."""
    write_manifest(run_dir, run_id=run_id, grid_offset=grid_offset)
    delete_recorded_matches(run_dir, root=profile.artifacts_root, patterns=profile.delete_patterns)
    keep_mb = profile.limits.get("per_run_keep_mb")
    if keep_mb is None:
        return None
    kept = sum(
        p.stat().st_size
        for p in run_dir.rglob("*")
        if p.is_file() and not p.is_symlink() and p.suffix.lower() != ".mp4"
    )
    if kept > float(keep_mb) * 1024 * 1024:
        return f"run {run_id} 보존 크기 {kept} B(MP4 제외)가 per_run_keep_mb {keep_mb} MiB를 넘습니다"
    return None


def _cell_from_outcomes(run: AdapterOutcome, judge: AdapterOutcome | None) -> dict[str, Any]:
    cell: dict[str, Any] = {"status": "ok", "verdict": None, "failure_code": None, "checks": []}
    if run.status == "timeout":
        cell.update(status="timeout", error=run.error)
    elif run.status != "ok":
        cell.update(status="infra", error=run.error or run.status)
    elif judge is None or judge.status != "ok":
        cell.update(status="infra", error=(judge.error if judge else None) or "judge 실패")
    else:
        response = judge.response
        cell.update(
            verdict=response["verdict"],
            failure_code=response.get("failure_code"),
            checks=response.get("checks", []),
        )
    return cell


def run_regress(
    profile: Profile,
    scenario: dict[str, Any],
    *,
    conditions: Sequence[str] = ("A", "B"),
    regress_id: str | None = None,
    isolation: str | None = None,
    check_source_repo: bool = True,
    record_cells: Mapping[str, Sequence[tuple[float, float]]] | None = None,
    gpu_reader: Callable[[], int | None] = gpu_memory_used_mib,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    task = scenario["task"]
    if task not in profile.tasks:
        raise RegressRefused(f"프로파일에 과제 {task}가 없습니다")
    for name in conditions:
        if name not in scenario["conditions"]:
            raise RegressRefused(f"시나리오에 조건 {name}이 없습니다")
    regress_id = regress_id or new_run_id("record-" if record_cells is not None else "regress-")
    root = profile.artifacts_root
    min_free = float(profile.limits.get("min_free_disk_gb", 0))
    timeout_s = scenario.get("limits", {}).get("timeout_s") or profile.limits.get("timeout_s", 600)
    grid = scenario["grid"]
    points = grid_points(grid["spawn_x_offset_mm"], grid["spawn_y_offset_mm"])
    if record_cells is not None:
        for cond_name, cells_wanted in record_cells.items():
            if cond_name not in conditions:
                raise RegressRefused(f"녹화 조건 {cond_name}이 실행 조건 {list(conditions)}에 없습니다")
            for point in cells_wanted:
                if tuple(point) not in points:
                    raise RegressRefused(f"녹화 격자점 {tuple(point)}이 시나리오 격자에 없습니다")
    gpu_on = task in tuple(profile.limits.get("gpu_tasks", ()))
    gpu_tol = float(profile.limits.get("gpu_tolerance_mib", 200))
    gpu_settle = float(profile.limits.get("gpu_settle_s", 20))
    gpu_log: list[dict[str, Any]] = []
    started = time.monotonic()

    with run_lock(profile.lock_file):
        check_free_disk(root, min_free)
        repo_state = (
            source_repo_state(profile)
            if check_source_repo
            else {"pinned_commit": profile.project.get("pinned_commit"), "commit": None, "warnings": []}
        )
        tool_sha: dict[str, Any] = {}
        sim_versions: set[str] = set()
        size_warnings: list[str] = []

        def execute(
            cond_name: str, point: tuple[float, float], run_id: str, repeat: int | None, record: bool = False
        ) -> dict[str, Any]:
            check_free_disk(root, min_free)
            x, y = point
            gpu_before = gpu_reader() if gpu_on else None
            if gpu_on and gpu_before is None:
                raise GpuMemoryNotReleased("GPU 과제인데 nvidia-smi memory.used를 읽지 못해 실행을 멈춥니다")
            run_dir = root / "runs" / run_id
            request = {
                "scenario_id": scenario["scenario_id"],
                "task": task,
                "condition": cond_name,
                "params": scenario["conditions"][cond_name]["params"],
                "grid_point": {"x_mm": x, "y_mm": y},
                "repeat": repeat,
                "record": record,
            }
            grid_offset = {"condition": cond_name, "x_mm": x, "y_mm": y, "repeat": repeat, "record": record}
            # HardwareGuardError 등으로 도중에 멈춰도 manifest 기록·delete_patterns 삭제는 하고 예외를 올린다.
            try:
                run = call_adapter(
                    profile, "run", task, request, run_dir,
                    run_id=run_id, timeout_s=timeout_s, isolation=isolation,
                )
                judge = None
                if run.status == "ok":
                    _collect_tool_sha(tool_sha, run.response)
                    if run.response.get("sim_version"):
                        sim_versions.add(str(run.response["sim_version"]))
                    result_path = Path(run.response["result_path"])
                    if not result_path.is_absolute():
                        result_path = run_dir / result_path
                    judge = call_adapter(
                        profile, "judge", task, {"result_path": str(result_path), "task": task}, run_dir,
                        run_id=run_id, timeout_s=timeout_s, isolation=isolation,
                    )
            finally:
                if run_dir.is_dir():
                    size_warning = _finish_run_dir(profile, run_dir, run_id, grid_offset)
                    if size_warning:
                        size_warnings.append(size_warning)
            cell = {"x_mm": x, "y_mm": y, **_cell_from_outcomes(run, judge), "run_id": run_id}
            if run.status == "ok":
                cell["sim_time_s"] = run.response.get("sim_time_s")
                cell["wall_s"] = round(run.wall_s, 3)
                if run.response.get("video_path"):
                    cell["video_path"] = display_path(run.response["video_path"])
            if gpu_on:
                assert gpu_before is not None
                gpu_after, waited = wait_gpu_release(
                    gpu_before, gpu_reader, tolerance_mib=gpu_tol, settle_s=gpu_settle, sleep=sleep
                )
                gpu_log.append({"run_id": run_id, "before_mib": gpu_before, "after_mib": gpu_after,
                                "waited_s": waited})
                if gpu_after is None or gpu_after > gpu_before + gpu_tol:
                    raise GpuMemoryNotReleased(
                        f"GPU 메모리가 {gpu_settle:g}초 안에 원복되지 않아 regress를 멈춥니다: "
                        f"run {run_id} 실행 전 {gpu_before} MiB → 후 {gpu_after} MiB (허용 +{gpu_tol:g} MiB)"
                    )
            return cell

        result_conditions: dict[str, Any] = {}
        recording = record_cells is not None
        for cond_name in conditions:
            if recording:
                wanted = [tuple(p) for p in (record_cells or {}).get(cond_name, [])]
                if not wanted:
                    continue
                cells = [
                    execute(cond_name, p, f"{regress_id}_{cond_name}_{_point_token(p)}_rec", None, True)
                    for p in points if p in wanted
                ]
            else:
                cells = [execute(cond_name, p, f"{regress_id}_{cond_name}_{_point_token(p)}", None) for p in points]
            result_conditions[cond_name] = {
                "label_ko": scenario["conditions"][cond_name].get("label_ko"),
                "judgement_basis": scenario["conditions"][cond_name]["judgement_basis"],
                "cells": cells,
                **summarize_condition(cells),
            }

        det_cond = conditions[0]
        repeat_points = [] if recording else select_repeat_points(points, int(grid.get("repeat_points", 0)))
        n_repeats = 0 if recording else int(grid.get("repeats", 0))
        repeats: dict[tuple[float, float], list[dict[str, Any]]] = {}
        for point in repeat_points:
            for r in range(n_repeats):
                run_id = f"{regress_id}_rep{r}_{det_cond}_{_point_token(point)}"
                repeats.setdefault(point, []).append(execute(det_cond, point, run_id, r))
        agreement = verdict_agreement(repeats)

    result = {
        "schema": REGRESS_SCHEMA,
        "regress_id": regress_id,
        "profile": profile.id,
        "scenario_id": scenario["scenario_id"],
        "task": task,
        "mode": "record" if recording else "grid",
        "conditions": result_conditions,
        "determinism": {
            "condition": det_cond,
            "points": len(repeat_points),
            "repeats": n_repeats,
            **agreement,
            "cells": [
                {"x_mm": p[0], "y_mm": p[1], "verdicts": [c.get("verdict") for c in cells],
                 "statuses": [c.get("status") for c in cells]}
                for p, cells in repeats.items()
            ],
        },
        "provenance": {
            "source_repo_commit": repo_state.get("commit") or repo_state.get("pinned_commit"),
            "pinned_commit": repo_state.get("pinned_commit"),
            "adapter_sha256": (
                sha256_file(profile.adapter_entry) if profile.adapter_entry.is_file() else None
            ),
            "tool_sha256": dict(sorted(tool_sha.items())),
            "sim_version": ",".join(sorted(sim_versions)) or None,
        },
        "warnings": [*repo_state.get("warnings", []), *size_warnings],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "wall_s": round(time.monotonic() - started, 3),
    }
    if gpu_on:
        result["gpu_check"] = {
            "tolerance_mib": gpu_tol,
            "settle_s": gpu_settle,
            "runs": len(gpu_log),
            "all_released": True,
            "max_excess_mib": max((g["after_mib"] - g["before_mib"] for g in gpu_log), default=None),
            "max_waited_s": max((g["waited_s"] for g in gpu_log), default=None),
            "baseline_range_mib": (
                [min(g["before_mib"] for g in gpu_log), max(g["before_mib"] for g in gpu_log)] if gpu_log else None
            ),
        }
    updated = apply_status(scenario, result)
    result["scenario_status"] = {"status": updated["status"], "model_note": updated["model_note"]}
    validate_regress_result(result)
    write_json(root / "runs" / regress_id / "regress_result.json", result)
    return result


def rejudge_regress(profile: Profile, scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """저장된 결과 파일로 `judge`만 다시 돌려 regress_result의 checks·failure_code를 갱신한다.

    시뮬은 다시 돌리지 않는다. 대상은 이 시나리오의 모든 regress_result(격자·녹화 패스).
    판정(verdict)이 바뀌면 파일을 쓰지 않고 `RegressRefused`로 멈춘다. 처음 갱신할 때 원본을
    `regress_result.before-rejudge.json`으로 한 번만 보존한다.
    """
    sid = scenario["scenario_id"]
    task = scenario["task"]
    summaries = []
    runs = profile.artifacts_root / "runs"
    for path in sorted(runs.glob("*/regress_result.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("scenario_id") != sid:
            continue
        changed = 0
        updates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for cond in (doc.get("conditions") or {}).values():
            for cell in cond.get("cells") or []:
                if cell.get("status") != "ok" or not cell.get("run_id"):
                    continue
                run_dir = runs / str(cell["run_id"])
                try:
                    response = json.loads((run_dir / "run.response.json").read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                result_path = Path(str(response.get("result_path") or ""))
                if not result_path.is_absolute():
                    result_path = run_dir / result_path
                if not result_path.is_file():
                    continue
                judge = call_adapter(
                    profile, "judge", task, {"result_path": str(result_path), "task": task}, run_dir,
                    run_id=str(cell["run_id"]),
                )
                if judge.status != "ok":
                    raise RegressRefused(f"다시 판정 실패({cell['run_id']}): {judge.error}")
                if judge.response["verdict"] != cell.get("verdict"):
                    raise RegressRefused(
                        f"다시 판정에서 verdict가 바뀌었습니다({cell['run_id']}: "
                        f"{cell.get('verdict')} → {judge.response['verdict']}). 파일을 쓰지 않습니다"
                    )
                updates.append((cell, judge.response))
        for cell, response in updates:
            new = {"failure_code": response.get("failure_code"), "checks": response.get("checks", [])}
            if any(cell.get(k) != v for k, v in new.items()):
                cell.update(new)
                changed += 1
        if changed:
            backup = path.with_name("regress_result.before-rejudge.json")
            if not backup.exists():
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            doc["rejudged_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            write_json(path, doc)
        summaries.append({"regress_id": doc.get("regress_id"), "mode": doc.get("mode", "grid"),
                          "cells_rejudged": len(updates), "cells_changed": changed})
    return summaries


def strip_timestamps(doc: Any) -> Any:
    """결정성 비교용: 시각 필드를 재귀적으로 뺀다."""
    if isinstance(doc, dict):
        return {k: strip_timestamps(v) for k, v in doc.items() if k not in TIMESTAMP_KEYS}
    if isinstance(doc, list):
        return [strip_timestamps(v) for v in doc]
    return doc


def run_replay(
    profile: Profile,
    source_path: Path,
    episode: int,
    *,
    ghost: bool = False,
    run_id: str | None = None,
    isolation: str | None = None,
) -> dict[str, Any]:
    """lerobot 에피소드 → convert → run(TCP·고스트) → 어긋남 계산."""
    task = "lerobot_episode"
    run_id = run_id or new_run_id("replay-")
    root = profile.artifacts_root
    check_free_disk(root, float(profile.limits.get("min_free_disk_gb", 0)))
    run_dir = root / "runs" / run_id
    warnings: list[str] = []

    convert = call_adapter(
        profile, "convert", task,
        {"source": {"kind": "lerobot_v3", "path": str(source_path), "episode": episode}},
        run_dir, run_id=run_id, isolation=isolation,
    )
    if convert.status != "ok":
        raise RegressRefused(f"convert 실패: {convert.status} {convert.error or ''}".strip())
    trajectory_path = Path(convert.response["trajectory_path"])
    video_path = None
    run = call_adapter(
        profile, "run", task,
        {
            "scenario_id": None,
            "task": task,
            "params": {"trajectory_path": str(trajectory_path), "ghost": ghost},
            "grid_point": None,
            "record": ghost,
        },
        run_dir, run_id=run_id, isolation=isolation,
    )
    if run.status == "ok":
        if not run.response.get("trajectory_path"):
            raise RegressRefused("궤적 과제의 run 응답에 trajectory_path가 없습니다")
        trajectory_path = Path(run.response["trajectory_path"])
        video_path = run.response.get("video_path")
    else:
        warnings.append(f"run {run.status}: {run.error}; convert 궤적(TCP 없음)으로 계산합니다")
    if not trajectory_path.is_absolute():
        trajectory_path = run_dir / trajectory_path

    header, rows = read_trajectory(trajectory_path)
    threshold = profile.replay.get("threshold", {})
    divergence = compute_divergence(
        header,
        rows,
        joint_threshold_deg=float(threshold.get("joint_deg", 3.0)),
        tcp_threshold_mm=float(threshold.get("tcp_mm", 20.0)),
    )
    result = {
        "schema": REPLAY_SCHEMA,
        "run_id": run_id,
        "profile": profile.id,
        "task": task,
        "origin": header.get("origin"),
        "source": {
            "path": display_path(source_path),
            "episode": episode,
            "sha256": header["source"].get("sha256"),
        },
        "trajectory_path": display_path(trajectory_path),
        "video_path": display_path(video_path) if video_path else None,
        "convert_video": convert.response.get("video"),
        "divergence": divergence,
        "warnings": warnings,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    result = display_tree(result)
    write_json(run_dir / "replay_result.json", result)
    _finish_run_dir(profile, run_dir, run_id, None)
    return result
