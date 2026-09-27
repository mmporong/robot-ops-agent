"""테스트용 가짜 어댑터 (robot-ops-adapter/1, stdlib만).

동작은 request `params`로 조절한다.
- fail_points / infra_points / timeout_points: [[x_mm, y_mm], ...] 격자점별 판정·infra·timeout
- flip_every_3rd: repeat 번호가 3의 배수 − 1(2, 5, …)이면 판정을 뒤집는다
- hardware_accessed: "true" | "missing" → run 응답 값, result_hardware: 결과 파일 값
- sleep_s + spawn_setsid_child + marker: setsid 손자(sleep)를 띄우고 자신도 잠든다
- call_log: 호출마다 "<verb> x y repeat" 한 줄을 덧붙일 파일
- dump_env: workdir/env.json에 환경변수 일부를 기록
- metric_jitter: repeat마다 lift_mm 값을 이만큼씩 더한다
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

PROTOCOL = "robot-ops-adapter/1"


def _write(path: str, doc: dict) -> None:
    Path(path).write_text(json.dumps(doc), encoding="utf-8")


def _point_in(point: dict | None, points: list) -> bool:
    if not point:
        return False
    return [point.get("x_mm"), point.get("y_mm")] in [list(p) for p in points]


def _log(params: dict, verb: str, request: dict) -> None:
    log = params.get("call_log")
    if log:
        point = request.get("grid_point") or {}
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"{verb} {point.get('x_mm')} {point.get('y_mm')} {request.get('repeat')}\n")


def do_run(request: dict, workdir: Path, response: str) -> int:
    params = request.get("params") or {}
    point = request.get("grid_point")
    _log(params, "run", request)
    if params.get("dump_env"):
        keys = ("PYTHONDONTWRITEBYTECODE", "ROBOT_OPS_RUN_ID")
        _write(str(workdir / "env.json"), {k: os.environ.get(k) for k in keys})
    if params.get("spawn_setsid_child"):
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)", params.get("marker", "x")],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if params.get("sleep_s") and (not params.get("timeout_points") or _point_in(point, params["timeout_points"])):
        time.sleep(float(params["sleep_s"]))
    if _point_in(point, params.get("infra_points", [])):
        _write(response, {"protocol": PROTOCOL, "status": "infra_error", "error": "fake infra"})
        return 3

    if request.get("task") == "lerobot_episode":
        return run_episode(request, workdir, response)

    verdict = "fail" if _point_in(point, params.get("fail_points", [])) else "pass"
    repeat = request.get("repeat")
    if params.get("flip_every_3rd") and repeat is not None and repeat % 3 == 2:
        verdict = "pass" if verdict == "fail" else "fail"
    lift = 30.0 if verdict == "pass" else 2.0
    lift += float(params.get("metric_jitter", 0.0)) * (repeat or 0)

    result_hw: object = False
    if params.get("result_hardware") == "true":
        result_hw = True
    result = {"verdict_hint": verdict, "lift_mm": lift}
    if params.get("result_hardware") != "missing":
        result["hardware_accessed"] = result_hw
    (workdir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (workdir / "scene.usda").write_text("#usda 1.0\n" * 100, encoding="utf-8")
    (workdir / "frames").mkdir(exist_ok=True)
    (workdir / "frames" / "0000.png").write_bytes(b"\x89PNG fake")

    doc = {
        "protocol": PROTOCOL,
        "status": "ok",
        "run_dir": str(workdir),
        "result_path": "result.json",
        "sim_time_s": 1.0,
        "wall_s": 0.01,
        "tool_sha256": {"fake_tool": "0" * 64},
        "hardware_accessed": False,
    }
    if params.get("hardware_accessed") == "true":
        doc["hardware_accessed"] = True
    elif params.get("hardware_accessed") == "missing":
        del doc["hardware_accessed"]
    _write(response, doc)
    return 0


def do_judge(request: dict, workdir: Path, response: str) -> int:
    result = json.loads(Path(request["result_path"]).read_text(encoding="utf-8"))
    verdict = result["verdict_hint"]
    lift = result["lift_mm"]
    _write(
        response,
        {
            "protocol": PROTOCOL,
            "status": "ok",
            "verdict": verdict,
            "judgement_basis": "rigid_proxy_lift",
            "assembly": "left_arm_proxy",
            "checks": [
                {"id": "lift_mm", "pass": verdict == "pass", "value": lift, "threshold": 10.0, "unit": "mm"}
            ],
            "failure_code": None if verdict == "pass" else "premature_cup_contact",
        },
    )
    return 0


def _trajectory_rows(n: int = 60, freeze_from: int = 30, freeze_len: int = 10) -> list[dict]:
    rows = []
    obs_hold = None
    for i in range(n):
        q = [math.radians(0.0 if i < 10 else (i - 10) * 1.0), 0.0]
        obs = list(q)
        if freeze_from <= i < freeze_from + freeze_len:
            obs_hold = obs_hold or rows[freeze_from - 1]["q_obs"]
            obs = list(obs_hold)
        rows.append({"t": i / 10.0, "q_cmd": q, "q_obs": obs})
    return rows


def do_convert(request: dict, workdir: Path, response: str) -> int:
    source = request["source"]
    header = {
        "schema": "robot-ops-trajectory/1",
        "robot_id": "so101",
        "arm": "left",
        "joint_names": ["shoulder_pan", "shoulder_lift"],
        "units": "rad",
        "fps": 10,
        "source": {"kind": source["kind"], "path": source["path"], "episode": source["episode"], "sha256": "f" * 64},
        "origin": "real_recording",
    }
    path = workdir / "trajectory.jsonl"
    rows = _trajectory_rows()
    path.write_text("\n".join(json.dumps(r) for r in [header, *rows]) + "\n", encoding="utf-8")
    _write(
        response,
        {
            "protocol": PROTOCOL,
            "status": "ok",
            "trajectory_path": str(path),
            "sha256": "0" * 64,
            "fps": 10,
            "n_frames": len(rows),
            "video": None,
        },
    )
    return 0


def run_episode(request: dict, workdir: Path, response: str) -> int:
    src = Path(request["params"]["trajectory_path"])
    lines = src.read_text(encoding="utf-8").splitlines()
    out_lines = [lines[0]]
    for line in lines[1:]:
        row = json.loads(line)
        row["tcp_cmd_m"] = [0.1 * row["q_cmd"][0], 0.0, 0.2]
        row["tcp_obs_m"] = [0.1 * row["q_obs"][0], 0.0, 0.2]
        out_lines.append(json.dumps(row))
    out = workdir / "trajectory_tcp.jsonl"
    out.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    result = workdir / "episode_result.json"
    result.write_text(json.dumps({"hardware_accessed": False}), encoding="utf-8")
    _write(
        response,
        {
            "protocol": PROTOCOL,
            "status": "ok",
            "run_dir": str(workdir),
            "result_path": str(result),
            "trajectory_path": str(out),
            "video_path": None,
            "sim_time_s": 0.0,
            "wall_s": 0.0,
            "tool_sha256": {},
            "hardware_accessed": False,
        },
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("verb")
    parser.add_argument("--task", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    workdir = Path(args.workdir)
    if args.verb == "run":
        return do_run(request, workdir, args.response)
    if args.verb == "judge":
        return do_judge(request, workdir, args.response)
    if args.verb == "convert":
        return do_convert(request, workdir, args.response)
    _write(args.response, {"protocol": PROTOCOL, "status": "unsupported", "error": args.verb})
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
