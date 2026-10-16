"""cup_contact `run` (leisaac python): Isaac Sim 5.1 컵 접촉 격자 셀 1개.

- 팀 스크립트는 allowlist의 고정 후보 중 현재 worktree에 있는 파일 하나만 subprocess로 실행한다.
  팀 저장소에는 쓰지 않는다(출력은 `<workdir>/isaac/`, cwd = workdir, `PYTHONDONTWRITEBYTECODE=1`).
- 인자: `--headless --output-dir <workdir>/isaac --spawn-x-offset-mm X --spawn-y-offset-mm Y`
  + 조건 params `recover=true`면 `--recover` + 요청 `record=true`면 `--record`.
- 팀 스크립트 종료 코드: 0 = task_pass, 2 = 판정 실패(정상 종료), 그 밖 = 예외.
  결과 파일이 없거나 `stop_reason`이 예외·초기화·창 닫힘이면 infra_error로 올린다.
- 녹화: `isaac/frames/frame_%04d.png`(물리 12스텝 = 0.1초마다 1장) → 10 fps H.264 MP4
  (`libx264 -preset slow -crf 23 -pix_fmt yuv420p -g 10 -movflags +faststart`, 짝수 크기).
  PNG와 `isaac/scene.usda`는 코어가 manifest 기록 뒤 `delete_patterns`로 지운다.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from team_source import resolve_team_file

sys.dont_write_bytecode = True

TEAM_REPO = Path.home() / "bimanual-robot"
TEAM_SCRIPTS = ("simulation/simulate_cup_contact.py", "tools/simulate_cup_contact.py")
TEAM_CONFIG = ("config/simulation/cup_contact_experiment.json",)
FPS = 10
OK_EXIT = (0, 2)
INFRA_STOP_PREFIXES = ("exception:", "initializing", "window_closed")


def _source_repo(request: dict) -> Path:
    value = request.get("source_repo")
    if value is None:
        return TEAM_REPO
    if not isinstance(value, (str, os.PathLike)) or not str(value):
        raise ValueError("source_repo는 비어 있지 않은 절대 경로여야 합니다")
    repo = Path(value)
    if not repo.is_absolute():
        raise ValueError("source_repo는 절대 경로여야 합니다")
    if repo.is_symlink():
        raise ValueError("source_repo 루트의 심볼릭 링크를 거부합니다")
    if not repo.is_dir():
        raise ValueError("source_repo는 일반 디렉터리여야 합니다")
    return repo


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number_arg(value: object) -> str:
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    compact = repr(numeric)
    if "e" in compact.lower():
        return format(Decimal(str(numeric)), "f")
    return compact


def build_argv(
    python: str,
    script: Path,
    out_dir: Path,
    request: dict,
    config_path: Path | None = None,
) -> list[str]:
    params = request.get("params") or {}
    point = request.get("grid_point") or {}
    argv = [python, str(script), "--headless", "--output-dir", str(out_dir)]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    if point.get("x_mm") is not None:
        argv += ["--spawn-x-offset-mm", _number_arg(point["x_mm"])]
    if point.get("y_mm") is not None:
        argv += ["--spawn-y-offset-mm", _number_arg(point["y_mm"])]
    if params.get("recover") is True:
        argv.append("--recover")
    if request.get("record") is True:
        argv.append("--record")
    return argv


def _finite_pair(value: object, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name}는 유한한 숫자 2개여야 합니다")
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ValueError(f"{name}는 유한한 숫자 2개여야 합니다")
        result.append(float(item))
    return result[0], result[1]


def _finite_vector(value: object, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{name}는 유한한 숫자 3개여야 합니다")
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ValueError(f"{name}는 유한한 숫자 3개여야 합니다")
        result.append(float(item))
    return result


def _prepare_plan_intervention(
    request: dict,
    source_repo: Path,
    workdir: Path,
) -> tuple[dict, Path, dict] | None:
    params = request.get("params") or {}
    if "plan_correction_mm" not in params:
        return None
    if params.get("recover") is True:
        raise ValueError("plan_correction_mm와 recover=true는 함께 사용할 수 없습니다")

    correction_x_mm, correction_y_mm = _finite_pair(
        params["plan_correction_mm"], "params.plan_correction_mm"
    )
    source_path, _ = resolve_team_file(source_repo, TEAM_CONFIG)
    source_bytes = source_path.read_bytes()
    source_config = json.loads(source_bytes)
    if not isinstance(source_config, dict):
        raise ValueError("컵 접촉 설정은 JSON 객체여야 합니다")
    original_center = _finite_vector(source_config.get("cup_center_m"), "cup_center_m")
    original_spawn = _finite_vector(source_config.get("cup_spawn_offset_m"), "cup_spawn_offset_m")

    point = request.get("grid_point") or {}
    original_x_mm = _finite_pair(
        [
            original_spawn[0] * 1000 if point.get("x_mm") is None else point["x_mm"],
            original_spawn[1] * 1000 if point.get("y_mm") is None else point["y_mm"],
        ],
        "grid_point",
    )
    effective_x_mm = original_x_mm[0] - correction_x_mm
    effective_y_mm = original_x_mm[1] - correction_y_mm

    generated_config = deepcopy(source_config)
    corrected_center = list(original_center)
    corrected_center[0] += correction_x_mm / 1000
    corrected_center[1] += correction_y_mm / 1000
    generated_config["cup_center_m"] = corrected_center
    generated_path = workdir / "intervention_config.json"
    generated_path.write_text(
        json.dumps(generated_config, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    original_actual = [
        original_center[0] + original_x_mm[0] / 1000,
        original_center[1] + original_x_mm[1] / 1000,
        original_center[2] + original_spawn[2],
    ]
    actual_spawn = [
        corrected_center[0] + effective_x_mm / 1000,
        corrected_center[1] + effective_y_mm / 1000,
        corrected_center[2] + original_spawn[2],
    ]
    if not all(math.isclose(before, after, rel_tol=0.0, abs_tol=1e-12)
               for before, after in zip(original_actual, actual_spawn)):
        raise RuntimeError("계획 보정이 실제 컵 위치를 보존하지 못했습니다")

    adjusted_request = deepcopy(request)
    adjusted_request["grid_point"] = {"x_mm": effective_x_mm, "y_mm": effective_y_mm}
    metadata = {
        "source_config_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "generated_config_sha256": _sha256(generated_path),
        "correction_mm": [correction_x_mm, correction_y_mm],
        "original_grid_point_mm": [original_x_mm[0], original_x_mm[1]],
        "effective_grid_point_mm": [effective_x_mm, effective_y_mm],
        "original_planned_center_m": original_center,
        "corrected_planned_center_m": corrected_center,
        "actual_spawn_center_m": actual_spawn,
        "changed_config_keys": ["cup_center_m[0]", "cup_center_m[1]"],
    }
    return adjusted_request, generated_path, metadata


def encode_frames(frames_dir: Path, out: Path) -> dict:
    frames = sorted(frames_dir.glob("frame_*.png"))
    if not frames:
        raise RuntimeError(f"녹화 요청이지만 프레임이 없습니다: {frames_dir}")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg/ffprobe를 찾지 못했습니다")
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-framerate", str(FPS), "-i", str(frames_dir / "frame_%04d.png"),
         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
         "-c:v", "libx264", "-preset", "slow", "-crf", "23", "-pix_fmt", "yuv420p",
         "-g", str(FPS), "-movflags", "+faststart", str(out)],
        check=True,
    )
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=codec_name,width,height,nb_read_frames", "-of", "json", str(out)],
        check=True, capture_output=True, text=True,
    )
    info = json.loads(probe.stdout)["streams"][0]
    return {**info, "fps": FPS, "n_png": len(frames), "size": out.stat().st_size, "sha256": _sha256(out)}


def run(request: dict, workdir: Path) -> dict:
    source_repo = _source_repo(request)
    script, team_script = resolve_team_file(source_repo, TEAM_SCRIPTS)
    out_dir = workdir / "isaac"
    if out_dir.exists():
        raise RuntimeError(f"출력 폴더가 이미 있습니다(재사용 금지): {out_dir}")

    intervention = _prepare_plan_intervention(request, source_repo, workdir)
    effective_request, config_path, plan_intervention = (
        intervention if intervention is not None else (request, None, None)
    )
    argv = build_argv(sys.executable, script, out_dir, effective_request, config_path)
    pythonpath = str(source_repo.resolve())
    if os.environ.get("PYTHONPATH"):
        pythonpath += os.pathsep + os.environ["PYTHONPATH"]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=pythonpath)
    started = time.monotonic()
    with (workdir / "isaac.log").open("wb") as log:
        proc = subprocess.run(argv, cwd=str(workdir), env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
    wall_s = round(time.monotonic() - started, 3)

    result_path = out_dir / "result.json"
    plan_path = out_dir / "plan.json"
    if not result_path.is_file() or not plan_path.is_file():
        response = {"status": "infra_error", "error": f"결과 파일 없음 (exit={proc.returncode})",
                    "hardware_accessed": False}
        if plan_intervention is not None:
            response["plan_intervention"] = plan_intervention
        return response
    result = json.loads(result_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    stop = str(result.get("stop_reason") or "")
    if proc.returncode not in OK_EXIT or stop.startswith(INFRA_STOP_PREFIXES):
        response = {
            "status": "infra_error",
            "error": f"팀 스크립트 비정상 종료 (exit={proc.returncode}, stop_reason={stop or None})",
            "hardware_accessed": result.get("hardware_accessed", False),
        }
        if plan_intervention is not None:
            response["plan_intervention"] = plan_intervention
        return response

    samples = result.get("samples") or []
    sim_time_s = float(samples[-1].get("time_s", 0.0)) if samples else 0.0
    video_path = None
    video = None
    if request.get("record") is True:
        video_file = workdir / "cup_contact.mp4"
        video = encode_frames(out_dir / "frames", video_file)
        video_path = str(video_file)

    try:
        sim_version = importlib.metadata.version("isaacsim")
    except importlib.metadata.PackageNotFoundError:
        sim_version = None
    response = {
        "run_dir": str(workdir),
        "result_path": str(result_path),
        "video_path": video_path,
        "video": video,
        "sim_time_s": sim_time_s,
        "wall_s": wall_s,
        "tool_sha256": plan.get("tool_sha256") or {},
        "sim_version": sim_version,
        "team_exit_code": proc.returncode,
        "team_script": team_script,
        "team_args": ["<workdir>/isaac" if a == str(out_dir) else a for a in argv[2:]],
        "hardware_accessed": result.get("hardware_accessed"),
    }
    if plan_intervention is not None:
        response["plan_intervention"] = plan_intervention
    return response


def handle(verb: str, task: str, request: dict, workdir: Path) -> dict:
    return {"status": "ok", **run(request, workdir)}
