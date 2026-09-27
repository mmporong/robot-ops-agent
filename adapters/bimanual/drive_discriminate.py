"""FAIL-20260921-1004 CPU 판별 (계획 §3.9, M0).

b38dbe8(수정 전)과 HEAD의 tools/mobile_service_control.py를 저장소 밖으로 꺼내
팀 테스트(test_final_alignment_converges_with_wheel_lag_and_asymmetry)와 같은
적분식으로 360초 지평까지 돌리고 final_alignment_bounded로 판정한다.
팀 저장소에는 쓰지 않는다. import하는 팀 파일은 mobile_service_control.py 하나다.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
TEAM_REPO = Path.home() / "bimanual-robot"
TEAM_FILE = "tools/mobile_service_control.py"
TEAM_TEST = "tools/test_mobile_service_control.py"
PRE_FIX = "b38dbe8"
SNAP_DIR = REPO_ROOT / ".local" / "m0" / "snap"
THRESHOLDS = REPO_ROOT / "evaluations" / "replay" / "f1004_thresholds.json"

# 팀 테스트 상수 (tools/test_mobile_service_control.py:114-133)
FAIL_POSE = (-1.62143, -2.35465, 1.87124)
GOAL = (-1.55, -2.25)
GOAL_YAW = math.pi
DT = 0.02
WHEEL_R = 0.0329
TRACK = 0.510
AXLE = 0.105

# 자세 섭동: 한 번에 한 축만 (7점)
POSE_OFFSETS = [
    ("base", (0.0, 0.0, 0.0)),
    ("x+0.05", (0.05, 0.0, 0.0)),
    ("x-0.05", (-0.05, 0.0, 0.0)),
    ("y+0.05", (0.0, 0.05, 0.0)),
    ("y-0.05", (0.0, -0.05, 0.0)),
    ("yaw+0.2", (0.0, 0.0, 0.2)),
    ("yaw-0.2", (0.0, 0.0, -0.2)),
]
LAGS = [0.0, 0.1, 0.2, 0.3, 0.5]
GAINS = [(1.0, 1.0), (0.9, 1.1), (0.85, 1.15), (1.1, 0.9), (1.15, 0.85)]


def _home(path: Path | str) -> str:
    s = str(path)
    home = str(Path.home())
    if s.startswith(home):
        s = "~" + s[len(home):]
    return s.replace(f"/data/{os.environ.get('USER', 'lim')}", "/data/$USER")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(rev: str, name: str) -> Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    src = subprocess.run(["git", "-C", str(TEAM_REPO), "show", f"{rev}:{TEAM_FILE}"],
                         check=True, capture_output=True).stdout
    out = SNAP_DIR / f"{name}_mobile_service_control.py"
    out.write_bytes(src)
    return out


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def simulate(module, pose0, lag_s, gains, horizon_s, window_s, deadband):
    """팀 테스트 적분식 그대로, 반복 횟수만 horizon_s/DT로 늘린다."""
    pose = np.array(pose0, dtype=float)
    goal = np.array(GOAL)
    gains = np.array(gains)
    actual = np.zeros(2)
    steps = int(round(horizon_s / DT))
    yaw_rates = np.zeros(steps)
    arrived = False
    n = 0
    for n in range(steps):
        cmd = module.follow_path(pose, [goal], GOAL_YAW)
        if cmd["arrived"]:
            arrived = True
            break
        target = np.array([cmd["left_rad_s"], cmd["right_rad_s"]]) * gains
        actual += min(1.0, DT / max(lag_s, DT)) * (target - actual)
        linear = WHEEL_R * actual.mean()
        angular = WHEEL_R * (actual[1] - actual[0]) / TRACK
        yaw = pose[2]
        pose[0] += DT * (linear * math.cos(yaw) + AXLE * angular * math.sin(yaw))
        pose[1] += DT * (linear * math.sin(yaw) - AXLE * angular * math.cos(yaw))
        pose[2] = math.atan2(math.sin(yaw + angular * DT), math.cos(yaw + angular * DT))
        yaw_rates[n] = angular
    else:
        n = steps
    elapsed = n * DT
    pos_err = float(np.linalg.norm(pose[:2] - goal))
    # 팀 테스트 도착 기준: arrived 플래그 + 위치 허용오차
    arrived_ok = arrived and pos_err <= module.FINAL_POSITION_TOLERANCE_M
    rates = yaw_rates[:n]
    win_steps = int(round(window_s / DT))
    tail = rates[-win_steps:] if n else rates
    total_rev = _reversals(rates, deadband)
    tail_rev = _reversals(tail, deadband)
    win_len = min(window_s, len(tail) * DT) if len(tail) else 0.0
    rate = tail_rev / win_len if win_len > 0 else 0.0
    return {
        "arrived": bool(arrived_ok),
        "elapsed_s": round(elapsed, 2),
        "final_pos_err_m": round(pos_err, 5),
        "reversals_total": int(total_rev),
        "reversals_last_window": int(tail_rev),
        "window_len_s": round(win_len, 2),
        "reversal_rate_per_s": round(rate, 4),
    }


def _reversals(rates: np.ndarray, deadband: float) -> int:
    live = rates[np.abs(rates) >= deadband]
    if live.size < 2:
        return 0
    s = np.sign(live)
    return int(np.count_nonzero(s[1:] != s[:-1]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / ".local/private/f1004_discriminate.json")
    args = ap.parse_args()
    th = json.loads(THRESHOLDS.read_text())
    horizon, window = float(th["horizon_s"]), float(th["reversal_window_s"])
    rate_max, deadband = float(th["reversal_rate_max_per_s"]), float(th["yaw_rate_deadband_rad_s"])

    head_sha = subprocess.run(["git", "-C", str(TEAM_REPO), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
    pre_sha = subprocess.run(["git", "-C", str(TEAM_REPO), "rev-parse", PRE_FIX],
                             check=True, capture_output=True, text=True).stdout.strip()
    versions = {"pre_fix": (PRE_FIX, pre_sha), "head": ("HEAD", head_sha)}
    modules, sources = {}, {}
    for key, (rev, full) in versions.items():
        path = _snapshot(rev, key)
        modules[key] = _load(path, f"msc_{key}")
        sources[key] = {"rev": full[:7], "commit": full, "file": TEAM_FILE,
                        "snapshot": _home(path.relative_to(REPO_ROOT)), "sha256": _sha256(path)}

    t0 = time.monotonic()
    cells = []
    for (pname, off), lag, g in itertools.product(POSE_OFFSETS, LAGS, GAINS):
        pose0 = (FAIL_POSE[0] + off[0], FAIL_POSE[1] + off[1], FAIL_POSE[2] + off[2])
        row = {"pose": pname, "lag_s": lag, "gains": list(g)}
        for key, mod in modules.items():
            r = simulate(mod, pose0, lag, g, horizon, window, deadband)
            r["pass"] = r["arrived"] and r["reversal_rate_per_s"] <= rate_max
            row[key] = r
        cells.append(row)
    wall = time.monotonic() - t0

    def count(key, field):
        return sum(1 for c in cells if c[key][field])

    n = len(cells)
    summary = {
        k: {"pass": count(k, "pass"), "arrived": count(k, "arrived"), "fail": n - count(k, "pass"),
            "fail_not_arrived": sum(1 for c in cells if not c[k]["arrived"]),
            "fail_reversal": sum(1 for c in cells if c[k]["reversal_rate_per_s"] > rate_max),
            "max_reversal_rate_per_s": max(c[k]["reversal_rate_per_s"] for c in cells)}
        for k in modules
    }
    head_worse = [c for c in cells if c["pre_fix"]["pass"] and not c["head"]["pass"]]
    head_better = [c for c in cells if not c["pre_fix"]["pass"] and c["head"]["pass"]]
    # 팀 테스트 3케이스 (자세 base) 재현 여부
    team_cases = [(0.0, (1.0, 1.0)), (0.2, (0.9, 1.1)), (0.5, (0.85, 1.15))]
    team_rows = [c for c in cells if c["pose"] == "base" and (c["lag_s"], tuple(c["gains"])) in team_cases]

    pre_reproduces = summary["pre_fix"]["fail"] > 0
    flag_a = pre_reproduces and summary["head"]["pass"] > summary["pre_fix"]["pass"]
    flag_b = not pre_reproduces
    flag_c = bool(head_worse)
    if flag_c:
        status, model_note = "after_fix_regression", ("not_reproducible_in_model" if flag_b else None)
    elif flag_b:
        status, model_note = "not_reproducible_in_model", None
    else:
        status, model_note = "active", None
    branch = "c" if flag_c else ("b" if flag_b else ("a" if flag_a else "none"))

    out = {
        "schema": "robot-ops-f1004-discriminate/1",
        "scenario": th["scenario"],
        "verifier": th["verifier"],
        "thresholds": {"file": "evaluations/replay/f1004_thresholds.json", "sha256": _sha256(THRESHOLDS),
                       "horizon_s": horizon, "reversal_window_s": window,
                       "reversal_rate_max_per_s": rate_max, "yaw_rate_deadband_rad_s": deadband},
        "sources": sources,
        "team_test": {"file": TEAM_TEST, "sha256": _sha256(TEAM_REPO / TEAM_TEST),
                      "function": "test_final_alignment_converges_with_wheel_lag_and_asymmetry",
                      "integration": "팀 테스트 적분식 그대로(dt=0.02, 1차 바퀴 지연, 게인 곱, 차축 오프셋 0.105), 반복만 T/dt"},
        "method": {
            "fail_pose": list(FAIL_POSE), "goal": list(GOAL), "goal_yaw": GOAL_YAW, "dt_s": DT,
            "pose_offsets": {name: list(o) for name, o in POSE_OFFSETS},
            "pose_offsets_note": "한 번에 한 축만 섭동(x·y ±0.05 m, yaw ±0.2 rad) + 원 자세 = 7점",
            "wheel_lag_s": LAGS, "gains_left_right": [list(g) for g in GAINS],
            "arrival": "follow_path arrived=True 이고 |pose_xy − goal| ≤ FINAL_POSITION_TOLERANCE_M (팀 테스트 assert 두 줄)",
            "yaw_rate": "적분에 쓰인 실제 차체 각속도(바퀴 지연·게인 반영)",
            "reversal": "|yaw rate| < deadband 샘플을 뺀 뒤 연속 샘플 부호 전환 횟수",
            "reversal_window": "종료 시점(도착 또는 T) 직전 60초. 실행이 60초보다 짧으면 실행 길이로 나눈다",
            "fail": "미도착 또는 반전율 > 임계",
        },
        "summary": {"cells": n, **summary, "wall_s": round(wall, 1)},
        "team_test_cases_base_pose": team_rows,
        "comparison": {
            "head_worse_cells": len(head_worse), "head_better_cells": len(head_better),
            "head_worse": [{"pose": c["pose"], "lag_s": c["lag_s"], "gains": c["gains"]} for c in head_worse],
            "head_better": [{"pose": c["pose"], "lag_s": c["lag_s"], "gains": c["gains"]} for c in head_better],
        },
        "branch": {
            "a_prefix_reproduces_and_head_better": flag_a,
            "b_prefix_not_reproduced": flag_b,
            "c_head_worse_cells_exist": flag_c,
            "decision": branch,
            "status": status,
            "model_note": model_note,
            "judgement_basis": "kinematic_drive",
            "note_ko": "차동 구동 운동학 모델 판별. Isaac 동역학 결과를 대체하지 않는다.",
        },
        "cells": cells,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"summary": out["summary"], "branch": out["branch"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
