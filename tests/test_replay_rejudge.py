"""`rejudge_regress` / `robot-ops regress --rejudge`: 저장된 결과 파일로 judge만 다시 실행."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_support import make_scenario, save_scenario, write_profile  # noqa: E402

from robot_ops.cli import main  # noqa: E402
from robot_ops.replay.regress import RegressRefused, rejudge_regress, run_regress  # noqa: E402

SMALL_GRID = {"spawn_x_offset_mm": [0, 5], "spawn_y_offset_mm": [0], "repeats": 0}


class RejudgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = write_profile(self.root)
        self.log = self.root / "calls.log"
        self.scenario = make_scenario(
            grid=SMALL_GRID,
            params={"A": {"fail_points": [[5, 0]], "call_log": str(self.log)}},
        )
        run_regress(self.profile, self.scenario, conditions=("A",), regress_id="rg")
        self.runs = self.root / "artifacts" / "runs"
        self.result_path = self.runs / "rg" / "regress_result.json"
        self.backup_path = self.runs / "rg" / "regress_result.before-rejudge.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _load(self) -> dict:
        return json.loads(self.result_path.read_text(encoding="utf-8"))

    def _tamper_checks(self) -> str:
        """저장된 regress_result의 checks·failure_code를 낡은 값으로 바꾸고 그 본문을 돌려준다."""
        doc = self._load()
        for cell in doc["conditions"]["A"]["cells"]:
            cell["checks"] = [{"id": "old_check", "pass": True, "value": 0, "threshold": 0, "unit": "mm"}]
            cell["failure_code"] = "stale_code"
        text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        self.result_path.write_text(text, encoding="utf-8")
        return text

    def test_rewrites_checks_from_stored_results_without_rerun(self) -> None:
        runs_before = self.log.read_text(encoding="utf-8")
        stale = self._tamper_checks()
        summary = rejudge_regress(self.profile, self.scenario)
        self.assertEqual(
            summary, [{"regress_id": "rg", "mode": "grid", "cells_rejudged": 2, "cells_changed": 2}]
        )
        # run은 다시 호출되지 않는다(가짜 어댑터 run 호출 기록 불변)
        self.assertEqual(self.log.read_text(encoding="utf-8"), runs_before)
        doc = self._load()
        self.assertIn("rejudged_at", doc)
        cells = {(c["x_mm"], c["y_mm"]): c for c in doc["conditions"]["A"]["cells"]}
        self.assertEqual(cells[(0.0, 0.0)]["checks"][0]["id"], "lift_mm")
        self.assertIsNone(cells[(0.0, 0.0)]["failure_code"])
        self.assertEqual(cells[(5.0, 0.0)]["failure_code"], "premature_cup_contact")
        self.assertEqual(cells[(5.0, 0.0)]["verdict"], "fail")
        # 원본은 백업에 그대로 남는다
        self.assertEqual(self.backup_path.read_text(encoding="utf-8"), stale)

    def test_backup_is_written_only_once(self) -> None:
        first = self._tamper_checks()
        rejudge_regress(self.profile, self.scenario)
        self._tamper_checks()
        rejudge_regress(self.profile, self.scenario)
        self.assertEqual(self.backup_path.read_text(encoding="utf-8"), first)
        backups = sorted(p.name for p in (self.runs / "rg").glob("regress_result*.json"))
        self.assertEqual(backups, ["regress_result.before-rejudge.json", "regress_result.json"])

    def test_no_change_writes_nothing(self) -> None:
        before = self.result_path.read_bytes()
        summary = rejudge_regress(self.profile, self.scenario)
        self.assertEqual(summary[0]["cells_changed"], 0)
        self.assertEqual(summary[0]["cells_rejudged"], 2)
        self.assertEqual(self.result_path.read_bytes(), before)
        self.assertFalse(self.backup_path.exists())

    def test_verdict_change_refuses_to_write(self) -> None:
        stale = self._tamper_checks()
        stored = self.runs / "rg_A_xp0_yp0" / "result.json"
        result = json.loads(stored.read_text(encoding="utf-8"))
        result["verdict_hint"] = "fail"
        stored.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(RegressRefused) as caught:
            rejudge_regress(self.profile, self.scenario)
        self.assertIn("verdict", str(caught.exception))
        self.assertEqual(self.result_path.read_text(encoding="utf-8"), stale)
        self.assertFalse(self.backup_path.exists())

    def test_other_scenario_results_are_ignored(self) -> None:
        other = make_scenario(scenario_id="other-cup", grid=SMALL_GRID)
        self._tamper_checks()
        self.assertEqual(rejudge_regress(self.profile, other), [])
        self.assertFalse(self.backup_path.exists())

    def test_missing_result_file_is_skipped(self) -> None:
        self._tamper_checks()
        (self.runs / "rg_A_xp5_yp0" / "result.json").unlink()
        summary = rejudge_regress(self.profile, self.scenario)
        self.assertEqual(summary[0]["cells_rejudged"], 1)
        self.assertEqual(summary[0]["cells_changed"], 1)

    def test_cli_rejudge(self) -> None:
        save_scenario(self.root, self.scenario)
        self._tamper_checks()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main([
                "regress", "--profile", str(self.profile.path), "--scenario", "fake-cup", "--rejudge",
            ])
        out = json.loads(buffer.getvalue())
        self.assertEqual(code, 0, out)
        self.assertEqual(out["rejudged"][0]["cells_changed"], 2)
        self.assertTrue(self.backup_path.exists())

    def test_cli_rejudge_verdict_change_returns_error(self) -> None:
        save_scenario(self.root, self.scenario)
        stored = self.runs / "rg_A_xp5_yp0" / "result.json"
        result = json.loads(stored.read_text(encoding="utf-8"))
        result["verdict_hint"] = "pass"
        stored.write_text(json.dumps(result), encoding="utf-8")
        before = self.result_path.read_bytes()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main([
                "regress", "--profile", str(self.profile.path), "--scenario", "fake-cup", "--rejudge",
            ])
        out = json.loads(buffer.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(out["type"], "RegressRefused")
        self.assertEqual(self.result_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
