"""SO-101 고스트 재생 충실도 (rlwalk MuJoCo 미러가 있는 기기에서만).

녹화(teleop_bench·teleop_v2, 2026-08-24)는 책상 클램프 배치였고 3인칭 카메라는 팔 뒤·오른쪽 위에
있었다. `run_mujoco_episode`가
- 차량 받침대를 치우고 책상 상판을 팔 장착면(팬 축 아래 78 mm)에 두는지,
- 현재 차량 팔의 mapping.json이 아니라 녹화 당시 변환(LeRobot 도 = URDF q)으로 TCP를 내는지,
- 두 렌더의 카메라가 같고 물리 스텝이 0인지
를 합성 궤적 두 자세(t=0 접힘, t=18 s 파지 직전 — teleop_bench ep0 관측값)로 확인한다.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRY = REPO_ROOT / "adapters" / "bimanual" / "entry.py"
RLWALK = Path.home() / "miniforge3" / "envs" / "rlwalk" / "bin" / "python"
MIRROR = Path.home() / "so101_tools" / "sim" / "sim_core.py"
JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
PARK_DEG = [-3.9, -112.0, 97.6, 74.0, 3.9]      # teleop_bench ep0 관측, t=0
GRASP_DEG = [7.8, 8.7, 20.4, 72.4, 29.5]        # teleop_bench ep0 관측, t≥17 s
# 위 두 자세의 graspframe(팔 기준 좌표, m) — 녹화 당시 변환(부호 +1·오프셋 0)의 기대값.
PARK_TCP_M = (0.0950, 0.0040, -0.0087)
GRASP_TCP_M = (0.1750, -0.0262, 0.0030)


@unittest.skipUnless(
    RLWALK.is_file() and MIRROR.is_file() and shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "rlwalk 인터프리터·SO-101 미러·ffmpeg가 이 기기에 없습니다",
)
class MujocoGhostFidelityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        header = {"schema": "robot-ops-trajectory/1", "joint_names": JOINTS, "fps": 10, "source": {"kind": "test"}}
        rows = [
            {"t": 0.0, "q_cmd": [math.radians(v) for v in PARK_DEG], "q_obs": [math.radians(v) for v in PARK_DEG],
             "gripper_cmd": 4.7, "gripper_obs": 4.7},
            {"t": 0.1, "q_cmd": [math.radians(v) for v in GRASP_DEG], "q_obs": [math.radians(v) for v in PARK_DEG],
             "gripper_cmd": 41.3, "gripper_obs": 4.7},
        ]
        traj = root / "trajectory.jsonl"
        traj.write_text("\n".join(json.dumps(r) for r in [header, *rows]) + "\n", encoding="utf-8")
        request = root / "request.json"
        request.write_text(json.dumps({"params": {"trajectory_path": str(traj), "ghost": True}}), encoding="utf-8")
        response = root / "response.json"
        work = root / "work"
        proc = subprocess.run(
            [str(RLWALK), str(ENTRY), "run", "--task", "lerobot_episode", "--request", str(request),
             "--response", str(response), "--workdir", str(work)],
            capture_output=True, text=True, timeout=300,
        )
        cls.proc = proc
        cls.response = json.loads(response.read_text(encoding="utf-8")) if response.exists() else {}
        result = work / "mujoco_result.json"
        cls.result = json.loads(result.read_text(encoding="utf-8")) if result.exists() else {}
        out_traj = work / "trajectory_mujoco.jsonl"
        lines = out_traj.read_text(encoding="utf-8").splitlines() if out_traj.exists() else []
        cls.header = json.loads(lines[0]) if lines else {}
        cls.rows = [json.loads(line) for line in lines[1:]]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_run_ok_without_physics(self) -> None:
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr[-2000:])
        self.assertEqual(self.response.get("status"), "ok")
        self.assertEqual(self.response.get("physics_steps"), 0)
        self.assertIs(self.response.get("camera_matrices_equal"), True)

    def test_desk_clamp_scene(self) -> None:
        scene = self.result["scene"]
        self.assertEqual(scene["layout"], "desk_clamp")
        self.assertEqual(scene["removed_bodies"], ["mobile_platform"])
        self.assertAlmostEqual(scene["desk_top_panel_m"], -0.078, places=6)

    def test_recording_conversion_not_current_mapping(self) -> None:
        self.assertNotIn("mapping.json", self.result["tool_sha256"])
        self.assertEqual(self.header["joint_conversion"], self.result["joint_conversion"])
        park, grasp = self.rows
        for got, want in ((park["tcp_obs_m"], PARK_TCP_M), (grasp["tcp_cmd_m"], GRASP_TCP_M)):
            self.assertLess(math.dist(got, want), 0.001, got)

    def test_camera_behind_right_above_like_real_view(self) -> None:
        ghost = self.result["ghost"]
        cam = ghost["camera_command"]
        ext = cam["extrinsic_world_to_cam"]
        rot = [row[:3] for row in ext[:3]]
        trans = [row[3] for row in ext[:3]]
        pos_world = [-sum(rot[r][c] * trans[r] for r in range(3)) for c in range(3)]
        offset = [w - p for w, p in zip(cam["lookat"], ghost["camera_setup_panel"]["lookat_panel_m"])]
        x, y, z = (w - o for w, o in zip(pos_world, offset))
        self.assertLess(x, 0.0)   # 팬 축보다 뒤
        self.assertLess(y, 0.0)   # 팔 오른쪽
        self.assertGreater(z, 0.3)  # 팬 축보다 30 cm 이상 위


if __name__ == "__main__":
    unittest.main()
