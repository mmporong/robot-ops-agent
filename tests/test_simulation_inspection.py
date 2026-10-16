from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from robot_ops.simulation_inspection import inspect_simulation_result  # noqa: E402


def _plan() -> dict[str, object]:
    return {
        "hardware_accessed": False,
        "observation_source": "simulator_ground_truth_not_rgb",
        "config": {
            "physics_dt_s": 0.1,
            "minimum_contact_force_n": 0.02,
        },
    }


def _sample(
    time_s: float,
    phase: str,
    force_n: float,
    *,
    attempt: int = 0,
) -> dict[str, object]:
    return {
        "time_s": time_s,
        "physics_time_s": time_s + 0.525,
        "phase": phase,
        "attempt": attempt,
        "contact_force_n": [force_n, 0.0],
    }


def _result(samples: list[dict[str, object]]) -> dict[str, object]:
    return {
        "hardware_accessed": False,
        "observation_source": "simulator_ground_truth_not_rgb",
        "task_pass": True,
        "completed": True,
        "stop_reason": "sequence_finished",
        "recovery_enabled": False,
        "retry_count": 0,
        "events": [],
        "samples": samples,
    }


class SimulationInspectionTest(unittest.TestCase):
    def test_normal_run_has_no_premature_contact(self) -> None:
        result = _result(
            [
                _sample(0.0, "RESET", 0.0),
                _sample(0.1, "APPROACH", 0.019),
                _sample(0.2, "CLOSE", 0.04),
                _sample(0.3, "LIFT", 1.0),
            ]
        )

        summary = inspect_simulation_result(
            result, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
        )

        self.assertEqual(summary["status"], "no_issue_in_checked_scope")
        check = summary["premature_contact_check"]
        self.assertEqual(check["issue_count"], 0)
        self.assertEqual(check["raw_matching_sample_count"], 0)
        self.assertTrue(summary["consistency"]["stored_event_agrees_with_recheck"])
        self.assertNotIn("samples", summary)

    def test_consecutive_premature_contact_is_one_bounded_incident(self) -> None:
        result = _result(
            [
                _sample(0.0, "RESET", 0.0),
                _sample(0.1, "APPROACH", 0.021),
                _sample(0.2, "APPROACH", 0.08),
            ]
        )
        result.update(
            {
                "task_pass": False,
                "completed": False,
                "stop_reason": "premature_cup_contact",
                "events": [
                    {
                        "failure_code": "premature_cup_contact",
                        "time_s": 0.1,
                        "attempt": 0,
                    }
                ],
            }
        )

        summary = inspect_simulation_result(
            result, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
        )

        self.assertEqual(summary["status"], "review_required")
        check = summary["premature_contact_check"]
        self.assertEqual(check["raw_matching_sample_count"], 2)
        self.assertEqual(check["issue_count"], 1)
        issue = check["issues"][0]
        self.assertEqual(issue["sample_index"], 1)
        self.assertEqual(issue["phase"], "APPROACH")
        self.assertEqual(issue["maximum_force_n"], 0.08)
        self.assertEqual(issue["threshold_n"], 0.02)
        self.assertTrue(summary["consistency"]["stored_event_agrees_with_recheck"])
        self.assertTrue(summary["consistency"]["stop_reason_agrees_with_recheck"])

    def test_recovered_contact_does_not_replace_final_task_pass(self) -> None:
        result = _result(
            [
                _sample(0.0, "APPROACH", 0.03, attempt=0),
                _sample(0.1, "RESET", 0.0, attempt=1),
                _sample(0.2, "CLOSE", 0.1, attempt=1),
                _sample(0.3, "LIFT", 1.0, attempt=1),
            ]
        )
        result.update(
            {
                "recovery_enabled": True,
                "retry_count": 1,
                "recovered": True,
                "events": [
                    {
                        "failure_code": "premature_cup_contact",
                        "time_s": 0.0,
                        "attempt": 0,
                    }
                ],
            }
        )

        summary = inspect_simulation_result(
            result, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
        )

        self.assertTrue(summary["stored_outcome"]["task_pass"])
        self.assertTrue(summary["runtime"]["recovered"])
        self.assertEqual(summary["premature_contact_check"]["issue_count"], 1)
        self.assertEqual(summary["consistency"]["expected_stop_reason"], "sequence_finished")
        self.assertTrue(summary["consistency"]["stop_reason_agrees_with_recheck"])

    def test_hardware_access_is_rejected(self) -> None:
        result = _result([_sample(0.0, "RESET", 0.0), _sample(0.1, "CLOSE", 0.0)])
        result["hardware_accessed"] = True

        with self.assertRaisesRegex(ValueError, "hardware_accessed"):
            inspect_simulation_result(
                result, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
            )

    def test_nonfinite_force_is_rejected(self) -> None:
        result = _result(
            [_sample(0.0, "RESET", math.nan), _sample(0.1, "CLOSE", 0.0)]
        )

        with self.assertRaisesRegex(ValueError, "유한한 숫자"):
            inspect_simulation_result(
                result, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
            )

    def test_issue_details_are_capped_without_losing_total(self) -> None:
        samples: list[dict[str, object]] = []
        for index in range(10):
            samples.append(_sample(index * 0.2, "APPROACH", 0.03))
            samples.append(_sample(index * 0.2 + 0.1, "CLOSE", 0.0))
        result = _result(samples)
        result.update(
            {
                "task_pass": False,
                "completed": False,
                "stop_reason": "premature_cup_contact",
                "events": [
                    {
                        "failure_code": "premature_cup_contact",
                        "time_s": 0.0,
                        "attempt": 0,
                    }
                ],
            }
        )

        summary = inspect_simulation_result(
            result, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
        )

        check = summary["premature_contact_check"]
        self.assertEqual(check["issue_count"], 10)
        self.assertEqual(len(check["issues"]), 8)
        self.assertTrue(check["issues_truncated"])

    def test_wrong_event_timestamp_is_a_mismatch(self) -> None:
        result = _result([_sample(0.0, "RESET", 0.0), _sample(0.1, "APPROACH", 0.03)])
        result.update(
            {
                "task_pass": False,
                "completed": False,
                "stop_reason": "premature_cup_contact",
                "events": [
                    {
                        "failure_code": "premature_cup_contact",
                        "time_s": 0.2,
                        "attempt": 0,
                    }
                ],
            }
        )

        summary = inspect_simulation_result(
            result, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
        )

        consistency = summary["consistency"]
        self.assertFalse(consistency["stored_event_agrees_with_recheck"])
        self.assertEqual(consistency["event_match_status"], "mismatch")
        self.assertEqual(
            consistency["stored_event_matches"][0]["mismatch_reason"],
            "no_raw_hit_with_same_attempt_and_time",
        )

    def test_missing_event_time_is_explicitly_not_assessable(self) -> None:
        result = _result(
            [_sample(0.0, "APPROACH", 0.03), _sample(0.1, "CLOSE", 0.0)]
        )
        result.update(
            {
                "task_pass": False,
                "completed": False,
                "stop_reason": "premature_cup_contact",
                "events": [
                    {"failure_code": "premature_cup_contact", "attempt": 0}
                ],
            }
        )

        summary = inspect_simulation_result(
            result, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
        )

        consistency = summary["consistency"]
        self.assertIsNone(consistency["stored_event_agrees_with_recheck"])
        self.assertEqual(consistency["event_match_status"], "not_assessable")
        self.assertEqual(
            consistency["stored_event_matches"][0]["not_assessable_fields"],
            ["time_s"],
        )

    def test_mismatched_event_attempt_is_a_mismatch(self) -> None:
        result = _result(
            [
                _sample(0.0, "APPROACH", 0.03, attempt=1),
                _sample(0.1, "CLOSE", 0.0, attempt=1),
            ]
        )
        result.update(
            {
                "task_pass": False,
                "completed": False,
                "stop_reason": "premature_cup_contact",
                "events": [
                    {
                        "failure_code": "premature_cup_contact",
                        "time_s": 0.0,
                        "attempt": 0,
                    }
                ],
            }
        )

        summary = inspect_simulation_result(
            result, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
        )

        consistency = summary["consistency"]
        self.assertFalse(consistency["stored_event_agrees_with_recheck"])
        self.assertEqual(consistency["event_match_status"], "mismatch")

    def test_missing_event_attempt_is_explicitly_not_assessable(self) -> None:
        result = _result(
            [_sample(0.0, "APPROACH", 0.03), _sample(0.1, "CLOSE", 0.0)]
        )
        result.update(
            {
                "task_pass": False,
                "completed": False,
                "stop_reason": "premature_cup_contact",
                "events": [
                    {"failure_code": "premature_cup_contact", "time_s": 0.0}
                ],
            }
        )

        summary = inspect_simulation_result(
            result, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
        )

        consistency = summary["consistency"]
        self.assertIsNone(consistency["stored_event_agrees_with_recheck"])
        self.assertEqual(
            consistency["stored_event_matches"][0]["not_assessable_fields"],
            ["attempt"],
        )

    def test_event_sample_index_must_identify_the_matching_hit(self) -> None:
        result = _result(
            [_sample(0.0, "RESET", 0.0), _sample(0.1, "APPROACH", 0.03)]
        )
        result.update(
            {
                "task_pass": False,
                "completed": False,
                "stop_reason": "premature_cup_contact",
                "events": [
                    {
                        "failure_code": "premature_cup_contact",
                        "time_s": 0.1,
                        "attempt": 0,
                        "sample_index": 0,
                    }
                ],
            }
        )

        summary = inspect_simulation_result(
            result, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
        )

        match = summary["consistency"]["stored_event_matches"][0]
        self.assertEqual(match["match_status"], "mismatch")
        self.assertFalse(summary["consistency"]["stored_event_agrees_with_recheck"])

    def test_time_axes_require_two_strict_dt_spaced_samples(self) -> None:
        one_sample = _result([_sample(0.0, "RESET", 0.0)])
        duplicate_time = _result(
            [_sample(0.0, "RESET", 0.0), _sample(0.0, "CLOSE", 0.0)]
        )
        wrong_dt = _result(
            [_sample(0.0, "RESET", 0.0), _sample(0.2, "CLOSE", 0.0)]
        )

        with self.assertRaisesRegex(ValueError, "두 개 이상"):
            inspect_simulation_result(
                one_sample, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
            )
        with self.assertRaisesRegex(ValueError, "엄격히 증가"):
            inspect_simulation_result(
                duplicate_time,
                _plan(),
                result_sha256="a" * 64,
                plan_sha256="b" * 64,
            )
        with self.assertRaisesRegex(ValueError, "physics_dt_s"):
            inspect_simulation_result(
                wrong_dt, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
            )

    def test_physics_time_is_required_and_keeps_a_constant_offset(self) -> None:
        missing = _result(
            [_sample(0.0, "RESET", 0.0), _sample(0.1, "CLOSE", 0.0)]
        )
        del missing["samples"][1]["physics_time_s"]
        shifted = _result(
            [_sample(0.0, "RESET", 0.0), _sample(0.1, "CLOSE", 0.0)]
        )
        shifted["samples"][1]["physics_time_s"] = 0.725

        with self.assertRaisesRegex(ValueError, "physics_time_s"):
            inspect_simulation_result(
                missing, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
            )
        with self.assertRaisesRegex(ValueError, "physics_dt_s|오프셋"):
            inspect_simulation_result(
                shifted, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
            )

    def test_final_attempt_contact_prevents_recovered_classification(self) -> None:
        result = _result(
            [
                _sample(0.0, "APPROACH", 0.03, attempt=0),
                _sample(0.1, "RESET", 0.0, attempt=1),
                _sample(0.2, "APPROACH", 0.04, attempt=1),
                _sample(0.3, "CLOSE", 0.0, attempt=1),
            ]
        )
        result.update(
            {
                "recovery_enabled": True,
                "retry_count": 1,
                "recovered": True,
                "events": [
                    {
                        "failure_code": "premature_cup_contact",
                        "time_s": 0.0,
                        "attempt": 0,
                    },
                    {
                        "failure_code": "premature_cup_contact",
                        "time_s": 0.2,
                        "attempt": 1,
                    },
                ],
            }
        )

        summary = inspect_simulation_result(
            result, _plan(), result_sha256="a" * 64, plan_sha256="b" * 64
        )

        self.assertFalse(summary["runtime"]["recovered"])
        self.assertEqual(summary["runtime"]["final_attempt"], 1)
        self.assertEqual(
            summary["runtime"]["final_attempt_premature_contact_count"], 1
        )
        self.assertFalse(
            summary["consistency"]["recovery_status_agrees_with_samples"]
        )


if __name__ == "__main__":
    unittest.main()
