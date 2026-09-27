from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_support import make_scenario, save_scenario, write_profile  # noqa: E402

from robot_ops.replay.report import build  # noqa: E402


class ReportBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = write_profile(self.root)
        save_scenario(self.root, make_scenario(scenario_id="cup-a"))
        save_scenario(
            self.root,
            make_scenario(
                scenario_id="drive-f1004",
                task="drive_kinematic",
                status="after_fix_regression",
                model_note="not_reproducible_in_model",
                sources=[{"path": str(Path.home() / "bimanual-robot/logs/x.json"), "sha256": "0" * 64, "role": "incident_run"}],
            ),
        )
        for sid in ("cup-a", "drive-f1004"):
            out = self.root / "artifacts" / "runs" / f"rg-{sid}"
            out.mkdir(parents=True)
            (out / "regress_result.json").write_text(
                json.dumps({"scenario_id": sid, "created_at": "2026-09-27T00:00:00", "conditions": {}})
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _data(self) -> tuple[list, str]:
        data = self.root / "report" / "data"
        cards = json.loads((data / "scenarios.json").read_text())
        blob = "".join(p.read_text() for p in sorted(data.rglob("*.json")))
        return cards, blob

    def test_public_build_excludes_after_fix_regression_entirely(self) -> None:
        summary = build(self.profile)
        cards, blob = self._data()
        self.assertEqual([c["scenario_id"] for c in cards], ["cup-a"])
        self.assertEqual(summary["build"], "public")
        self.assertNotIn("drive-f1004", blob)
        self.assertNotIn("after_fix_regression", blob)
        self.assertFalse((self.root / "report" / "data" / "regress" / "drive-f1004.json").exists())

    def test_private_includes_all_and_preserves_model_note(self) -> None:
        build(self.profile, private=True)
        cards, _ = self._data()
        by_id = {c["scenario_id"]: c for c in cards}
        self.assertEqual(set(by_id), {"cup-a", "drive-f1004"})
        self.assertEqual(by_id["drive-f1004"]["status"], "after_fix_regression")
        self.assertEqual(by_id["drive-f1004"]["model_note"], "not_reproducible_in_model")
        self.assertTrue(by_id["drive-f1004"]["sources"][0]["path"].startswith("~/"))

    def test_public_rebuild_removes_private_leftovers(self) -> None:
        build(self.profile, private=True)
        build(self.profile)
        _, blob = self._data()
        self.assertNotIn("drive-f1004", blob)

    def test_regress_derived_status_excludes_drive_scenario(self) -> None:
        save_scenario(self.root, make_scenario(scenario_id="drive-f1004", task="drive_kinematic", status="active"))
        out = self.root / "artifacts" / "runs" / "rg-drive-f1004" / "regress_result.json"
        out.write_text(json.dumps({
            "scenario_id": "drive-f1004", "created_at": "2026-09-27T01:00:00", "conditions": {},
            "scenario_status": {"status": "after_fix_regression", "model_note": None},
        }))
        build(self.profile)
        _, blob = self._data()
        self.assertNotIn("drive-f1004", blob)


if __name__ == "__main__":
    unittest.main()
