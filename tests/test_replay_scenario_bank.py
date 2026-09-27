from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_support import make_scenario, save_scenario  # noqa: E402

from robot_ops.replay.scenario_bank import (  # noqa: E402
    apply_status,
    enforce_replayable,
    load_scenario,
    resolve_status,
    status_candidates,
    verify_sources,
)
from robot_ops.replay.schema import SchemaError, validate_scenario  # noqa: E402


def _regress(a_fail: list, b_fail: list, points=((0, 0), (5, 0))) -> dict:
    def cells(fails):
        return [
            {"x_mm": x, "y_mm": y, "status": "ok", "verdict": "fail" if [x, y] in fails else "pass"}
            for x, y in points
        ]
    return {"conditions": {"A": {"cells": cells(a_fail)}, "B": {"cells": cells(b_fail)}}}


class SchemaTest(unittest.TestCase):
    def test_example_validates(self) -> None:
        validate_scenario(make_scenario())

    def test_repository_scenarios_validate(self) -> None:
        import json

        from replay_support import PROJECT_ROOT

        docs = {}
        for path in sorted((PROJECT_ROOT / "scenarios").glob("*/scenario.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            validate_scenario(doc)
            docs[doc["scenario_id"]] = doc
        self.assertEqual(docs["so101-observation-freeze"]["assembly"], "single_arm_so101")
        with self.assertRaises(SchemaError):
            validate_scenario(make_scenario(assembly="single_arm"))

    def test_missing_key_and_bad_enum(self) -> None:
        doc = make_scenario()
        del doc["grid"]
        with self.assertRaisesRegex(SchemaError, "grid"):
            validate_scenario(doc)
        for key, value in (
            ("origin", "simulation"),
            ("status", "fixed"),
            ("judgement_basis", "real_grasp"),
            ("task", "pour"),
            ("model_note", "something"),
        ):
            with self.subTest(key=key), self.assertRaises(SchemaError):
                validate_scenario(make_scenario(**{key: value}))
        bad = make_scenario()
        bad["conditions"]["B"]["judgement_basis"] = "oracle"
        with self.assertRaises(SchemaError):
            validate_scenario(bad)


class ReplayableTest(unittest.TestCase):
    def test_hardware_layer_code_and_no_trajectory(self) -> None:
        doc = make_scenario()
        self.assertTrue(enforce_replayable(copy.deepcopy(doc))["replayable"]["value"])
        hw = make_scenario(replayable={"value": True, "layer": "hardware", "reason": "x"})
        self.assertFalse(enforce_replayable(hw)["replayable"]["value"])
        code = make_scenario()
        code["incident"]["code"] = "SERVO_THERMAL_LATCH"
        self.assertFalse(enforce_replayable(code)["replayable"]["value"])
        no_traj = make_scenario(sources=[{"path": "~/f.json", "sha256": "0" * 64, "role": "failure_record"}])
        result = enforce_replayable(no_traj)["replayable"]
        self.assertFalse(result["value"])
        self.assertIn("궤적 없음", result["reason"])


class StatusTest(unittest.TestCase):
    def test_priority_and_model_note_preserved(self) -> None:
        self.assertEqual(
            resolve_status({"after_fix_regression", "not_reproducible_in_model"}),
            ("after_fix_regression", "not_reproducible_in_model"),
        )
        self.assertEqual(resolve_status({"stale", "not_reproducible_in_model"}), ("not_reproducible_in_model", None))
        self.assertEqual(resolve_status({"stale", "active"}), ("stale", None))
        self.assertEqual(resolve_status(set()), ("active", None))

    def test_drive_rules(self) -> None:
        # 수정 전 전부 통과 → 모델 미재현
        self.assertEqual(status_candidates("drive_kinematic", _regress([], [])), {"not_reproducible_in_model"})
        # 수정 전 통과 셀이 HEAD에서 실패 → after_fix_regression (+ 미재현 동시 성립)
        doc = apply_status(make_scenario(task="drive_kinematic"), _regress([], [[5, 0]]))
        self.assertEqual((doc["status"], doc["model_note"]), ("after_fix_regression", "not_reproducible_in_model"))
        # 수정 전 재현 + HEAD 전부 통과 → stale
        self.assertEqual(status_candidates("drive_kinematic", _regress([[0, 0]], [])), {"stale"})
        # 수정 전 재현 + HEAD도 같은 셀 실패 → active
        self.assertEqual(status_candidates("drive_kinematic", _regress([[0, 0]], [[0, 0]])), {"active"})

    def test_cup_task_status_untouched(self) -> None:
        doc = make_scenario(task="cup_contact", status="active")
        self.assertEqual(apply_status(doc, _regress([], [[5, 0]]))["status"], "active")
        self.assertEqual(status_candidates("cup_contact", _regress([], [[5, 0]])), set())


class LoadAndVerifyTest(unittest.TestCase):
    def test_load_and_sha_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            src = base / "rec.json"
            src.write_text("{}")
            digest = hashlib.sha256(b"{}").hexdigest()
            doc = make_scenario(sources=[
                {"path": str(src), "sha256": digest, "role": "recording"},
                {"path": str(base / "missing.json"), "sha256": digest, "role": "incident_run"},
                {"path": str(src), "sha256": "1" * 64, "role": "incident_run"},
            ])
            path = save_scenario(base, doc)
            loaded = load_scenario(path)
            results = verify_sources(loaded)
            self.assertEqual([r["ok"] for r in results], [True, False, False])
            self.assertEqual(results[1]["reason"], "파일 없음")
            self.assertEqual(results[2]["reason"], "SHA-256 불일치")
            path.write_text(json.dumps(dict(doc, status="bogus")))
            with self.assertRaises(SchemaError):
                load_scenario(path)


if __name__ == "__main__":
    unittest.main()
