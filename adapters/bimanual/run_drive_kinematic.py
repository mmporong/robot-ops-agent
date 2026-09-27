"""drive_kinematic `run` (lerobot python): FAIL-20260921-1004 운동학 격자 셀 1개.

- 팀 코드는 `tools/mobile_service_control.py` 하나만, `git show <rev>:<path>`로 꺼낸 단일 파일
  사본(workdir/snapshot/)을 import한다. 팀 저장소에는 쓰지 않는다.
- 적분식·상수·반전 계산은 M0 `drive_discriminate.py`(팀 테스트 적분식)를 그대로 가져다 쓴다.
- 격자점 (x_mm, y_mm)은 사고 자세(FAIL_POSE)의 x·y 오프셋(mm)이다. 바퀴 지연·좌우 게인·yaw
  오프셋은 조건 params가 정한다.
- 결과: result.json(판정 입력 값) + trajectory.jsonl(`robot-ops-trajectory/1`, 10 Hz,
  관절 = 좌·우 바퀴 누적 각 [rad], q_cmd = 명령 바퀴 속도 적분, q_obs = 지연·게인 반영 실제 바퀴).
- 하드웨어·ROS·네트워크에 접근하지 않는다(`hardware_accessed: false`).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

import numpy as np  # noqa: E402

import drive_discriminate as dd  # noqa: E402 - 같은 폴더(M0) 적분 상수·반전 계산

TEAM_REPO = Path.home() / "bimanual-robot"
TEAM_FILE = "tools/mobile_service_control.py"
REPO_ROOT = Path(__file__).resolve().parents[2]
THRESHOLDS = REPO_ROOT / "evaluations" / "replay" / "f1004_thresholds.json"
TRAJ_HZ = 10


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(rev: str, workdir: Path) -> tuple[Path, str]:
    commit = subprocess.run(["git", "-C", str(TEAM_REPO), "rev-parse", "--verify", f"{rev}^{{commit}}"],
                            check=True, capture_output=True, text=True).stdout.strip()
    source = subprocess.run(["git", "-C", str(TEAM_REPO), "show", f"{commit}:{TEAM_FILE}"],
                            check=True, capture_output=True).stdout
    out = workdir / "snapshot" / f"{commit[:7]}_mobile_service_control.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(source)
    return out, commit


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def simulate(module, pose0, lag_s: float, gains, horizon_s: float, window_s: float, deadband: float):
    """dd.simulate와 같은 적분식. 10 Hz 궤적을 함께 기록한다."""
    pose = np.array(pose0, dtype=float)
    goal = np.array(dd.GOAL)
    gains = np.array(gains, dtype=float)
    actual = np.zeros(2)
    wheel_cmd = np.zeros(2)
    wheel_obs = np.zeros(2)
    steps = int(round(horizon_s / dd.DT))
    every = max(1, int(round(1.0 / (TRAJ_HZ * dd.DT))))
    yaw_rates = np.zeros(steps)
    rows = []
    arrived = False
    n = 0
    for n in range(steps):
        cmd = module.follow_path(pose, [goal], dd.GOAL_YAW)
        if n % every == 0:
            rows.append({"t": round(n * dd.DT, 4), "q_cmd": wheel_cmd.tolist(), "q_obs": wheel_obs.tolist(),
                         "pose": pose.tolist(), "cmd_rad_s": [cmd["left_rad_s"], cmd["right_rad_s"]]})
        if cmd["arrived"]:
            arrived = True
            break
        raw = np.array([cmd["left_rad_s"], cmd["right_rad_s"]])
        target = raw * gains
        actual += min(1.0, dd.DT / max(lag_s, dd.DT)) * (target - actual)
        wheel_cmd += raw * dd.DT
        wheel_obs += actual * dd.DT
        linear = dd.WHEEL_R * actual.mean()
        angular = dd.WHEEL_R * (actual[1] - actual[0]) / dd.TRACK
        yaw = pose[2]
        pose[0] += dd.DT * (linear * math.cos(yaw) + dd.AXLE * angular * math.sin(yaw))
        pose[1] += dd.DT * (linear * math.sin(yaw) - dd.AXLE * angular * math.cos(yaw))
        pose[2] = math.atan2(math.sin(yaw + angular * dd.DT), math.cos(yaw + angular * dd.DT))
        yaw_rates[n] = angular
    else:
        n = steps
    elapsed = n * dd.DT
    pos_err = float(np.linalg.norm(pose[:2] - goal))
    arrived_ok = arrived and pos_err <= module.FINAL_POSITION_TOLERANCE_M
    rates = yaw_rates[:n]
    win_steps = int(round(window_s / dd.DT))
    tail = rates[-win_steps:] if n else rates
    tail_rev = dd._reversals(tail, deadband)
    win_len = min(window_s, len(tail) * dd.DT) if len(tail) else 0.0
    summary = {
        "arrived": bool(arrived_ok),
        "arrived_flag": bool(arrived),
        "elapsed_s": round(elapsed, 2),
        "final_pos_err_m": round(pos_err, 5),
        "final_pose": [float(v) for v in pose],
        "reversals_total": int(dd._reversals(rates, deadband)),
        "reversals_last_window": int(tail_rev),
        "window_len_s": round(win_len, 2),
        "reversal_rate_per_s": round(tail_rev / win_len, 4) if win_len > 0 else 0.0,
    }
    return summary, rows


def run(request: dict, workdir: Path) -> dict:
    started = time.monotonic()
    params = request.get("params") or {}
    point = request.get("grid_point") or {"x_mm": 0.0, "y_mm": 0.0}
    th = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    horizon = float(th["horizon_s"])
    window = float(th["reversal_window_s"])
    deadband = float(th["yaw_rate_deadband_rad_s"])
    rev = str(params["rev"])
    lag = float(params.get("wheel_lag_s", 0.0))
    gains = [float(g) for g in params.get("gains_left_right", [1.0, 1.0])]
    yaw_off = float(params.get("yaw_offset_rad", 0.0))
    pose0 = (dd.FAIL_POSE[0] + float(point["x_mm"]) / 1000.0,
             dd.FAIL_POSE[1] + float(point["y_mm"]) / 1000.0,
             dd.FAIL_POSE[2] + yaw_off)

    snap, commit = snapshot(rev, workdir)
    module = load_module(snap, f"msc_{commit[:7]}_{workdir.name}".replace("-", "_").replace(".", "_"))
    summary, rows = simulate(module, pose0, lag, gains, horizon, window, deadband)

    header = {
        "schema": "robot-ops-trajectory/1",
        "robot_id": "bimanual_mobile_base",
        "arm": "base",
        "joint_names": ["left_wheel", "right_wheel"],
        "units": "rad",
        "fps": TRAJ_HZ,
        "source": {"kind": "kinematic_drive", "path": f"~/bimanual-robot@{commit[:7]}:{TEAM_FILE}",
                   "episode": None, "sha256": _sha256(snap)},
        "origin": "sim_run",
        "note_ko": "차동 구동 운동학 모델(팀 테스트 적분식). q=바퀴 누적 각, pose=[x,y,yaw]",
    }
    traj = workdir / "trajectory.jsonl"
    with traj.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        for r in rows:
            handle.write(json.dumps(r) + "\n")

    result = {
        "schema": "robot-ops-drive-kinematic/1",
        "task": "drive_kinematic",
        "scenario_id": request.get("scenario_id"),
        "condition": request.get("condition"),
        "rev": rev,
        "commit": commit,
        "team_file": TEAM_FILE,
        "snapshot_sha256": _sha256(snap),
        "grid_point": point,
        "pose0": list(pose0),
        "wheel_lag_s": lag,
        "gains_left_right": gains,
        "horizon_s": horizon,
        "thresholds_sha256": _sha256(THRESHOLDS),
        "judgement_basis": "kinematic_drive",
        **summary,
        "hardware_accessed": False,
    }
    result_path = workdir / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "run_dir": str(workdir),
        "result_path": str(result_path),
        "trajectory_path": str(traj),
        "video_path": None,
        "sim_time_s": summary["elapsed_s"],
        "wall_s": round(time.monotonic() - started, 3),
        "tool_sha256": {f"mobile_service_control.py@{commit[:7]}": _sha256(snap)},
        "hardware_accessed": False,
    }


def handle(verb: str, task: str, request: dict, workdir: Path) -> dict:
    return {"status": "ok", **run(request, workdir)}
