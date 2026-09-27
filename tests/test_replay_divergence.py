from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import replay_support  # noqa: E402,F401

from robot_ops.replay.divergence import (  # noqa: E402
    compute_divergence,
    detect_freezes,
    read_trajectory,
)
from robot_ops.replay.schema import SchemaError  # noqa: E402

HEADER = {
    "schema": "robot-ops-trajectory/1",
    "robot_id": "so101",
    "arm": "left",
    "joint_names": ["a", "b", "c"],
    "units": "rad",
    "fps": 10,
    "source": {"kind": "lerobot_v3", "path": "~/x", "episode": 0, "sha256": "0" * 64},
    "origin": "real_recording",
}


def _rows(n: int = 80, *, offset_deg: float = 0.0, freeze: tuple[int, int] | None = None, tcp: bool = False):
    rows = []
    for i in range(n):
        angle = 0.0 if i < 10 else (i - 10) * 1.0
        cmd = [math.radians(angle), math.radians(angle / 2), 0.0]
        obs = [q - math.radians(offset_deg) for q in cmd]
        if freeze and freeze[0] <= i < freeze[1]:
            obs = list(rows[freeze[0] - 1]["q_obs"])
        row = {"t": i / 10.0, "q_cmd": cmd, "q_obs": obs}
        if tcp:
            row["tcp_cmd_m"] = [cmd[0] * 0.3, 0.0, 0.1]
            row["tcp_obs_m"] = [obs[0] * 0.3, 0.0, 0.1]
        rows.append(row)
    return rows


class DivergenceTest(unittest.TestCase):
    def test_identical_trajectory_has_no_incident(self) -> None:
        result = compute_divergence(HEADER, _rows(tcp=True))
        self.assertEqual(result["incidents"], [])
        self.assertEqual(max(result["max_joint_err_deg"].values()), 0.0)
        self.assertEqual(result["tcp"]["max_mm"], 0.0)

    def test_constant_calibration_offset_removed(self) -> None:
        result = compute_divergence(HEADER, _rows(offset_deg=4.0))
        self.assertEqual(result["static_window"], [0, 10])
        self.assertAlmostEqual(result["calibration_offset_deg"][0], 4.0)
        self.assertLess(max(result["max_joint_err_deg"].values()), 1e-9)
        self.assertEqual(result["incidents"], [])
        # 교정 차를 빼지 않으면 4° > 임계 3°라 사고로 잡힌다
        raw = compute_divergence(HEADER, _rows(offset_deg=4.0), auto_static=False)
        self.assertEqual(raw["incidents"][0]["kind"], "joint_tracking")

    def test_freeze_start_within_one_frame(self) -> None:
        for start in (25, 40, 57):
            with self.subTest(start=start):
                freezes = detect_freezes(_rows(freeze=(start, start + 12)))
                self.assertEqual(len(freezes), 1)
                self.assertLessEqual(abs(freezes[0]["frame_start"] - start), 1)
                self.assertLessEqual(abs(freezes[0]["frame_end"] - (start + 11)), 1)

    def test_short_freeze_below_min_frames_ignored(self) -> None:
        self.assertEqual(detect_freezes(_rows(freeze=(30, 32)), min_frames=3), [])

    def test_gripper_counts_in_freeze_rule(self) -> None:
        still = [0.0, 0.0, 0.0]
        base = [{"t": i / 10.0, "q_cmd": list(still), "q_obs": list(still)} for i in range(20)]
        # 관절은 전부 정지, 그리퍼 명령만 움직이고 그리퍼 관측은 멈춤 → 동결
        cmd_only = [dict(r, gripper_cmd=float(i), gripper_obs=5.0) for i, r in enumerate(base)]
        freezes = detect_freezes(cmd_only)
        self.assertEqual(len(freezes), 1)
        self.assertEqual((freezes[0]["frame_start"], freezes[0]["frame_end"]), (1, 19))
        # 그리퍼 관측도 따라 움직이면 동결이 아니다(모든 관절 + 그리퍼 정지가 조건)
        both = [dict(r, gripper_cmd=float(i), gripper_obs=float(i)) for i, r in enumerate(base)]
        self.assertEqual(detect_freezes(both), [])
        # 관절 동결 구간에서 그리퍼 관측이 움직이면 동결에서 빠진다
        rows = _rows(freeze=(30, 45))
        for i, row in enumerate(rows):
            row["gripper_cmd"] = 0.0
            row["gripper_obs"] = float(i) if 30 <= i < 45 else 0.0
        self.assertEqual(detect_freezes(rows), [])
        # None 그리퍼는 관절만 본다(기존 동작 유지)
        rows = _rows(freeze=(30, 45))
        for row in rows:
            row["gripper_cmd"] = row["gripper_obs"] = None
        self.assertEqual(len(detect_freezes(rows)), 1)

    def test_freeze_and_threshold_incidents(self) -> None:
        result = compute_divergence(HEADER, _rows(freeze=(30, 45), tcp=True))
        kinds = [i["kind"] for i in result["incidents"]]
        self.assertIn("freeze", kinds)
        self.assertIn("joint_tracking", kinds)
        self.assertIn("tcp_distance", kinds)
        # 동결 뒤 명령이 4프레임째(4°)에 임계 3°를 넘는다
        self.assertAlmostEqual(result["first_exceed_joint_s"], 3.3)

    def test_calibration_suspect_only_on_tcp(self) -> None:
        result = compute_divergence(HEADER, _rows(offset_deg=8.0, tcp=True))
        self.assertTrue(result["tcp"]["calibration_suspect"])
        self.assertNotIn("calibration_suspect", json.dumps(result["max_joint_err_deg"]))
        no_tcp = compute_divergence(HEADER, _rows(offset_deg=8.0))
        self.assertIsNone(no_tcp["tcp"])
        for incident in no_tcp["incidents"]:
            self.assertNotIn("calibration_suspect", incident)

    def test_read_trajectory_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text("\n".join(json.dumps(x) for x in [HEADER, *_rows(5)]) + "\n")
            _, rows = read_trajectory(path)
            self.assertEqual(len(rows), 5)
            bad = dict(HEADER, units="deg")
            path.write_text(json.dumps(bad) + "\n")
            with self.assertRaises(SchemaError):
                read_trajectory(path)


if __name__ == "__main__":
    unittest.main()
