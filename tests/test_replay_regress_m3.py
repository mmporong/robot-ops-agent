"""M3 코어 추가분: GPU 메모리 원복 확인, 녹화 패스, replay 결과 경로 정규화."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_support import make_scenario, write_profile  # noqa: E402

from robot_ops.replay.adapter import HardwareGuardError  # noqa: E402
from robot_ops.replay.profile import current_user  # noqa: E402
from robot_ops.replay.regress import (  # noqa: E402
    GpuMemoryNotReleased,
    display_tree,
    parse_record_cells,
    run_regress,
    wait_gpu_release,
)

SMALL_GRID = {"spawn_x_offset_mm": [0, 5], "spawn_y_offset_mm": [0], "repeats": 0}


class FakeSmi:
    """nvidia-smi memory.used 대역: 값 목록을 차례로 돌려주고 마지막 값을 유지한다."""

    def __init__(self, values: list[int | None]) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> int | None:
        self.calls += 1
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class GpuCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = write_profile(self.root)
        self.profile.limits.update(gpu_tasks=["cup_contact"], gpu_tolerance_mib=200, gpu_settle_s=5)
        self.sleeps: list[float] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, smi: FakeSmi, **kwargs):
        return run_regress(
            self.profile, make_scenario(grid=SMALL_GRID), conditions=("A",), regress_id="gpu",
            gpu_reader=smi, sleep=self.sleeps.append, **kwargs,
        )

    def test_wait_returns_once_back_within_tolerance(self) -> None:
        smi = FakeSmi([3000, 1500, 250])
        after, waited = wait_gpu_release(100, smi, tolerance_mib=200, settle_s=20, sleep=self.sleeps.append)
        self.assertEqual((after, waited), (250, 2.0))
        self.assertEqual(self.sleeps, [1.0, 1.0])

    def test_released_memory_recorded(self) -> None:
        # 셀마다: 실행 전 기준, 실행 뒤 한 번 높았다가 원복
        smi = FakeSmi([100, 2600, 150, 110, 2000, 120])
        result = self._run(smi)
        check = result["gpu_check"]
        self.assertEqual(check["runs"], 2)
        self.assertTrue(check["all_released"])
        self.assertEqual(check["max_excess_mib"], 50)
        self.assertEqual(check["max_waited_s"], 1.0)
        self.assertEqual(check["baseline_range_mib"], [100, 110])
        saved = json.loads((self.root / "artifacts" / "runs" / "gpu" / "regress_result.json").read_text())
        self.assertEqual(saved["gpu_check"]["runs"], 2)

    def test_unreleased_memory_stops_regress(self) -> None:
        smi = FakeSmi([100, 900])
        with self.assertRaises(GpuMemoryNotReleased) as caught:
            self._run(smi)
        self.assertIn("100 MiB", str(caught.exception))
        self.assertIn("900 MiB", str(caught.exception))
        self.assertEqual(len(self.sleeps), 5)  # settle_s 5초 동안 1초 간격
        self.assertFalse((self.root / "artifacts" / "runs" / "gpu" / "regress_result.json").exists())
        # 두 번째 셀은 실행되지 않았다
        self.assertFalse((self.root / "artifacts" / "runs" / "gpu_A_xp5_yp0").exists())

    def test_unreadable_smi_stops_before_run(self) -> None:
        with self.assertRaises(GpuMemoryNotReleased):
            self._run(FakeSmi([None]))
        self.assertFalse((self.root / "artifacts" / "runs" / "gpu_A_xp0_yp0").exists())

    def test_non_gpu_task_skips_check(self) -> None:
        self.profile.limits["gpu_tasks"] = ["other_task"]
        smi = FakeSmi([None])
        result = self._run(smi)
        self.assertNotIn("gpu_check", result)
        self.assertEqual(smi.calls, 0)


class RunDirCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = write_profile(self.root)
        self.runs = self.root / "artifacts" / "runs"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_hardware_guard_still_writes_manifest_and_deletes_patterns(self) -> None:
        scenario = make_scenario(grid=SMALL_GRID, params={"A": {"hardware_accessed": "true"}})
        with self.assertRaises(HardwareGuardError):
            run_regress(self.profile, scenario, conditions=("A",), regress_id="hw")
        run_dir = self.runs / "hw_A_xp0_yp0"
        self.assertTrue((run_dir / "manifest.json").is_file())
        self.assertFalse((run_dir / "scene.usda").exists())
        self.assertFalse((run_dir / "frames" / "0000.png").exists())
        self.assertTrue((run_dir / "result.json").exists())
        manifest = json.loads((run_dir / "manifest.json").read_text())
        deleted = {f["path"] for f in manifest["files"] if f["deleted"]}
        self.assertEqual(deleted, {"scene.usda", "frames/0000.png"})
        self.assertFalse((self.runs / "hw_A_xp5_yp0").exists())

    def test_kept_size_over_budget_warns_without_failing(self) -> None:
        self.profile.limits["per_run_keep_mb"] = 0.0001  # 약 105 B: 가짜 run의 남는 파일이 넘는다
        result = run_regress(self.profile, make_scenario(grid=SMALL_GRID), conditions=("A",), regress_id="sz")
        size_warnings = [w for w in result["warnings"] if "per_run_keep_mb" in w]
        self.assertEqual(len(size_warnings), 2)
        self.assertIn("sz_A_xp0_yp0", size_warnings[0])
        self.assertEqual(result["conditions"]["A"]["pass"], 2)

    def test_kept_size_within_budget_has_no_warning(self) -> None:
        self.profile.limits["per_run_keep_mb"] = 5
        result = run_regress(self.profile, make_scenario(grid=SMALL_GRID), conditions=("A",), regress_id="ok")
        self.assertFalse([w for w in result["warnings"] if "per_run_keep_mb" in w])


class RecordPassTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parse_record_cells(self) -> None:
        self.assertEqual(
            parse_record_cells("0,-5; A:2.5,5;B:0,-5", ("A", "B")),
            {"A": [(0.0, -5.0), (2.5, 5.0)], "B": [(0.0, -5.0)]},
        )
        with self.assertRaises(ValueError):
            parse_record_cells("C:0,0", ("A", "B"))
        with self.assertRaises(ValueError):
            parse_record_cells("0", ("A",))

    def test_record_pass_runs_only_selected_cells_with_record(self) -> None:
        log = self.root / "calls.log"
        scenario = make_scenario(params={"A": {"call_log": str(log)}, "B": {"call_log": str(log)}})
        result = run_regress(
            write_profile(self.root), scenario, regress_id="rec",
            record_cells={"A": [(0, -5), (5, 5)], "B": [(0, -5)]},
        )
        self.assertEqual(result["mode"], "record")
        self.assertEqual(len(result["conditions"]["A"]["cells"]), 2)
        self.assertEqual(len(result["conditions"]["B"]["cells"]), 1)
        self.assertEqual(result["determinism"]["repeats"], 0)
        runs = [line for line in log.read_text().splitlines() if line.startswith("run")]
        self.assertEqual(len(runs), 3)
        run_dir = self.root / "artifacts" / "runs" / "rec_A_xp0_ym5_rec"
        request = json.loads((run_dir / "run.request.json").read_text())
        self.assertTrue(request["record"])
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertTrue(manifest["grid_offset"]["record"])

    def test_record_cell_outside_grid_refused(self) -> None:
        from robot_ops.replay.regress import RegressRefused

        with self.assertRaises(RegressRefused):
            run_regress(write_profile(self.root), make_scenario(), regress_id="rec2",
                        record_cells={"A": [(1, 1)]})

    def test_grid_mode_requests_no_record(self) -> None:
        result = run_regress(write_profile(self.root), make_scenario(grid=SMALL_GRID),
                             conditions=("A",), regress_id="g")
        self.assertEqual(result["mode"], "grid")
        request = json.loads((self.root / "artifacts" / "runs" / "g_A_xp0_yp0" / "run.request.json").read_text())
        self.assertFalse(request["record"])


class DisplayTreeTest(unittest.TestCase):
    def test_nested_absolute_paths_normalized(self) -> None:
        home = str(Path.home())
        data = f"/data/{current_user()}"
        doc = {
            "convert_video": {"path": f"{home}/x/media/camera_wrist.mp4", "camera": "wrist",
                              "source": {"path": f"{data}/ds/v.mp4"}},
            "videos": [f"{home}/a.mp4", "/opt/other/b.mp4", "relative/c.mp4"],
            "n": 3,
        }
        out = display_tree(doc)
        self.assertEqual(out["convert_video"]["path"], "~/x/media/camera_wrist.mp4")
        self.assertEqual(out["convert_video"]["source"]["path"], "/data/$USER/ds/v.mp4")
        self.assertEqual(out["videos"], ["~/a.mp4", "/opt/other/b.mp4", "relative/c.mp4"])
        self.assertEqual(out["n"], 3)

    def test_replay_result_file_has_no_user_paths(self) -> None:
        from robot_ops.replay.regress import run_replay

        with tempfile.TemporaryDirectory(dir=Path.home()) as tmp:
            root = Path(tmp)
            profile = write_profile(root)
            result = run_replay(profile, root / "dataset", 1)
            text = (root / "artifacts" / "runs" / result["run_id"] / "replay_result.json").read_text()
        self.assertNotIn(str(Path.home()) + "/", text)
        self.assertNotIn(f"/data/{current_user()}/", text)


if __name__ == "__main__":
    unittest.main()
