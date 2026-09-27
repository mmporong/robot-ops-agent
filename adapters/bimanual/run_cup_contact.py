"""cup_contact `run` (leisaac python): Isaac Sim 5.1 컵 접촉 격자 셀 1개.

- 팀 스크립트는 allowlist의 `tools/simulate_cup_contact.py` 하나만 subprocess로 실행한다.
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
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

TEAM_REPO = Path.home() / "bimanual-robot"
TEAM_SCRIPT = "tools/simulate_cup_contact.py"  # allowlist 포함 여부는 tests/test_replay_profile.py가 고정한다
FPS = 10
OK_EXIT = (0, 2)
INFRA_STOP_PREFIXES = ("exception:", "initializing", "window_closed")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_argv(python: str, script: Path, out_dir: Path, request: dict) -> list[str]:
    params = request.get("params") or {}
    point = request.get("grid_point") or {}
    argv = [python, str(script), "--headless", "--output-dir", str(out_dir)]
    if point.get("x_mm") is not None:
        argv += ["--spawn-x-offset-mm", f"{float(point['x_mm']):g}"]
    if point.get("y_mm") is not None:
        argv += ["--spawn-y-offset-mm", f"{float(point['y_mm']):g}"]
    if params.get("recover") is True:
        argv.append("--recover")
    if request.get("record") is True:
        argv.append("--record")
    return argv


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
    script = TEAM_REPO / TEAM_SCRIPT
    if not script.is_file():
        raise RuntimeError(f"팀 스크립트가 없습니다: {script}")
    out_dir = workdir / "isaac"
    if out_dir.exists():
        raise RuntimeError(f"출력 폴더가 이미 있습니다(재사용 금지): {out_dir}")

    argv = build_argv(sys.executable, script, out_dir, request)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    started = time.monotonic()
    with (workdir / "isaac.log").open("wb") as log:
        proc = subprocess.run(argv, cwd=str(workdir), env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
    wall_s = round(time.monotonic() - started, 3)

    result_path = out_dir / "result.json"
    plan_path = out_dir / "plan.json"
    if not result_path.is_file() or not plan_path.is_file():
        return {"status": "infra_error", "error": f"결과 파일 없음 (exit={proc.returncode})",
                "hardware_accessed": False}
    result = json.loads(result_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    stop = str(result.get("stop_reason") or "")
    if proc.returncode not in OK_EXIT or stop.startswith(INFRA_STOP_PREFIXES):
        return {"status": "infra_error",
                "error": f"팀 스크립트 비정상 종료 (exit={proc.returncode}, stop_reason={stop or None})",
                "hardware_accessed": result.get("hardware_accessed", False)}

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
    return {
        "run_dir": str(workdir),
        "result_path": str(result_path),
        "video_path": video_path,
        "video": video,
        "sim_time_s": sim_time_s,
        "wall_s": wall_s,
        "tool_sha256": plan.get("tool_sha256") or {},
        "sim_version": sim_version,
        "team_exit_code": proc.returncode,
        "team_args": ["<workdir>/isaac" if a == str(out_dir) else a for a in argv[2:]],
        "hardware_accessed": result.get("hardware_accessed"),
    }


def handle(verb: str, task: str, request: dict, workdir: Path) -> dict:
    return {"status": "ok", **run(request, workdir)}
