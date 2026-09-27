from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_support import GRID5, make_scenario, write_profile  # noqa: E402

from robot_ops.replay.adapter import HardwareGuardError  # noqa: E402
from robot_ops.replay.artifacts import load_manifest  # noqa: E402
from robot_ops.replay.grid import (  # noqa: E402
    grid_points,
    select_repeat_points,
    summarize_condition,
    verdict_agreement,
)
from robot_ops.replay.regress import run_regress, strip_timestamps  # noqa: E402


class GridUnitTest(unittest.TestCase):
    def test_grid_order_and_repeat_points(self) -> None:
        points = grid_points(GRID5, GRID5)
        self.assertEqual(len(points), 25)
        self.assertEqual(points[:2], [(-5, -5), (-2.5, -5)])
        self.assertEqual(points[-1], (5, 5))
        self.assertEqual(
            select_repeat_points(points, 5),
            [(-5, -5), (-2.5, -2.5), (0, 0), (2.5, 2.5), (5, 5)],
        )

    def test_invalid_threshold(self) -> None:
        cells = [{"status": "ok", "verdict": "pass"}] * 22 + [{"status": "infra"}] * 3
        self.assertFalse(summarize_condition(cells)["invalid"])
        cells = [{"status": "ok", "verdict": "pass"}] * 21 + [{"status": "infra"}] * 3 + [{"status": "timeout"}]
        summary = summarize_condition(cells)
        self.assertTrue(summary["invalid"])
        self.assertEqual((summary["valid"], summary["pass"]), (21, 21))

    def test_agreement_excludes_infra(self) -> None:
        result = verdict_agreement(
            {
                "p1": [{"status": "ok", "verdict": "pass"}, {"status": "ok", "verdict": "pass"}, {"status": "infra"}],
                "p2": [{"status": "infra"}],
            }
        )
        self.assertEqual(result["verdict_agreement"], 1.0)
        self.assertEqual(result["measured_points"], 1)


class RegressTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_full_grid_is_deterministic_and_counts(self) -> None:
        fails = [[-5, -5], [0, 2.5], [5, 5]]
        scenario = make_scenario(params={"A": {"fail_points": fails}, "B": {"fail_points": [[5, 5]]}})
        results = []
        for name in ("one", "two"):
            profile = write_profile(self.root / name)
            results.append(run_regress(profile, scenario, regress_id="rg1"))
        first, second = results
        self.assertEqual(strip_timestamps(first), strip_timestamps(second))
        self.assertEqual((first["conditions"]["A"]["pass"], first["conditions"]["A"]["valid"]), (22, 25))
        self.assertEqual(first["conditions"]["B"]["pass"], 24)
        self.assertFalse(first["conditions"]["A"]["invalid"])
        self.assertEqual(first["determinism"]["verdict_agreement"], 1.0)
        self.assertEqual(first["determinism"]["points"], 5)
        saved = json.loads(
            (self.root / "one" / "artifacts" / "runs" / "rg1" / "regress_result.json").read_text()
        )
        self.assertEqual(strip_timestamps(saved), strip_timestamps(first))
        self.assertEqual(saved["scenario_status"], {"status": "active", "model_note": None})

        run_dir = self.root / "one" / "artifacts" / "runs" / "rg1_A_xm5_ym5"
        self.assertFalse((run_dir / "scene.usda").exists())
        self.assertFalse((run_dir / "frames" / "0000.png").exists())
        manifest = load_manifest(run_dir)
        by_path = {f["path"]: f for f in manifest["files"]}
        self.assertTrue(by_path["scene.usda"]["deleted"])
        self.assertFalse(by_path["result.json"]["deleted"])
        self.assertEqual(manifest["grid_offset"], {"condition": "A", "x_mm": -5, "y_mm": -5, "repeat": None, "record": False})

    def test_four_infra_cells_mark_invalid(self) -> None:
        infra = [[-5, -5], [0, 0], [2.5, 5], [5, -5]]
        scenario = make_scenario(params={"A": {"infra_points": infra}}, grid={"repeats": 0})
        result = run_regress(write_profile(self.root), scenario, conditions=("A",), regress_id="rg2")
        cond = result["conditions"]["A"]
        self.assertEqual((cond["valid"], cond["infra"], cond["pass"]), (21, 4, 21))
        self.assertTrue(cond["invalid"])

    def test_timeout_cell_excluded(self) -> None:
        scenario = make_scenario(
            params={"A": {"sleep_s": 30, "timeout_points": [[0, 0]]}},
            grid={"spawn_x_offset_mm": [0, 5], "spawn_y_offset_mm": [0], "repeats": 0},
            limits={"timeout_s": 1},
        )
        result = run_regress(write_profile(self.root), scenario, conditions=("A",), regress_id="rg3")
        statuses = [c["status"] for c in result["conditions"]["A"]["cells"]]
        self.assertEqual(statuses, ["timeout", "ok"])
        self.assertEqual(result["conditions"]["A"]["valid"], 1)

    def test_flip_every_third_repeat_agreement(self) -> None:
        scenario = make_scenario(
            params={"A": {"flip_every_3rd": True, "metric_jitter": 0.5}},
            grid={"spawn_x_offset_mm": GRID5, "spawn_y_offset_mm": GRID5, "repeat_points": 5, "repeats": 3},
        )
        result = run_regress(write_profile(self.root), scenario, conditions=("A",), regress_id="rg4")
        det = result["determinism"]
        self.assertAlmostEqual(det["verdict_agreement"], 2 / 3)
        self.assertEqual(det["unanimous_points"], 0)
        self.assertEqual(det["repeats"], 3)
        self.assertEqual(det["cells"][0]["verdicts"], ["pass", "pass", "fail"])
        self.assertAlmostEqual(det["max_metric_spread"]["lift_mm"], 30.0 + 0.5 - 3.0)

    def test_hardware_guard_aborts_immediately(self) -> None:
        log = self.root / "calls.log"
        for params in ({"hardware_accessed": "true"}, {"hardware_accessed": "missing"}):
            log.write_text("")
            scenario = make_scenario(params={"A": {**params, "call_log": str(log)}})
            with self.subTest(params=params):
                with self.assertRaises(HardwareGuardError):
                    run_regress(write_profile(self.root), scenario, regress_id="rg5")
                self.assertEqual(log.read_text().splitlines(), ["run -5 -5 None"])
                self.assertFalse((self.root / "artifacts" / "runs" / "rg5" / "regress_result.json").exists())


if __name__ == "__main__":
    unittest.main()
