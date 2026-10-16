from __future__ import annotations

import copy
import math
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from robot_ops.cup_diagnosis import (  # noqa: E402
    derive_plan_correction,
    evaluate_common_reference,
)


DT = 1.0 / 120.0
REFERENCE = [0.39, 0.17, 0.78]
CONFIG = {
    "physics_dt_s": DT,
    "minimum_lift_m": 0.04,
    "minimum_contact_force_n": 0.02,
    "maximum_tracking_error_rad": 0.12,
    "maximum_midbody_height_error_m": 0.015,
    "maximum_contact_center_error_m": 0.015,
    "maximum_cup_lateral_drift_m": 0.02,
    "maximum_cup_tilt_deg": 15.0,
    "cup_center_m": [99.0, 99.0, 99.0],
}


def _sample(
    index: int,
    phase: str,
    position: list[float],
    *,
    force: tuple[float, float] = (0.0, 0.0),
    attempt: int = 0,
) -> dict[str, object]:
    return {
        "time_s": index * DT,
        "physics_time_s": 0.525 + index * DT,
        "phase": phase,
        "attempt": attempt,
        "cup_position_m": position,
        "contact_force_n": list(force),
        "arm_error_rad": 0.01,
        "midbody_height_error_m": 0.001,
        "contact_center_error_m": 0.002,
        "cup_tilt_deg": 1.0,
    }


def _reset_window(position: list[float]) -> list[dict[str, object]]:
    return [_sample(index, "RESET", list(position)) for index in range(61)]


def _passing_result() -> dict[str, object]:
    samples = [_sample(index, "RESET", list(REFERENCE)) for index in range(10)]
    start = len(samples)
    for index in range(120):
        samples.append(
            _sample(
                start + index,
                "LIFT_HOLD",
                [REFERENCE[0] + 0.001, REFERENCE[1] - 0.001, REFERENCE[2] + 0.05],
                force=(0.5, 0.6),
            )
        )
    return {
        "completed": True,
        "stop_reason": "sequence_finished",
        "recovery_enabled": False,
        "retry_count": 0,
        "task_pass": False,
        "rigid_proxy_lift_pass": False,
        "evaluation_center_m": [100.0, 100.0, 100.0],
        "samples": samples,
    }


class PlanCorrectionTest(unittest.TestCase):
    def test_median_observation_produces_xy_candidate(self) -> None:
        samples = _reset_window([0.395, 0.167, 0.78])
        samples[3]["cup_position_m"] = [0.3955, 0.1665, 0.78]
        result = derive_plan_correction(
            REFERENCE,
            samples,
            window_s=0.5,
            max_motion_m=0.001,
            contact_threshold_n=0.02,
        )
        self.assertEqual(result["status"], "candidate_available")
        self.assertAlmostEqual(result["correction_mm"][0], 5.0)
        self.assertAlmostEqual(result["correction_mm"][1], -3.0)
        self.assertEqual(result["observed_center_m"], [0.395, 0.167, 0.78])
        self.assertEqual(result["interpretation"]["cause_status"], "undetermined")

    def test_small_mismatch_has_zero_correction(self) -> None:
        result = derive_plan_correction(
            REFERENCE,
            _reset_window([0.3905, 0.17, 0.78]),
            window_s=0.5,
            max_motion_m=0.001,
            contact_threshold_n=0.02,
        )
        self.assertEqual(result["status"], "no_material_pose_mismatch")
        self.assertEqual(result["correction_mm"], [0.0, 0.0])

    def test_unstable_reset_window_is_rejected(self) -> None:
        samples = _reset_window([0.395, 0.167, 0.78])
        samples[-1]["cup_position_m"] = [0.397, 0.167, 0.78]
        with self.assertRaisesRegex(ValueError, "많이 움직였습니다"):
            derive_plan_correction(
                REFERENCE, samples, window_s=0.5, max_motion_m=0.001, contact_threshold_n=0.02
            )

    def test_contact_during_reset_window_is_rejected(self) -> None:
        samples = _reset_window([0.395, 0.167, 0.78])
        samples[20]["contact_force_n"] = [0.0, 0.02]
        with self.assertRaisesRegex(ValueError, "접촉력이 임계값 이상"):
            derive_plan_correction(
                REFERENCE, samples, window_s=0.5, max_motion_m=0.001, contact_threshold_n=0.02
            )

    def test_reset_window_requires_two_sided_contact_force(self) -> None:
        for forces in ([0.0], [0.0, 0.0, 0.0]):
            with self.subTest(forces=forces):
                samples = _reset_window([0.395, 0.167, 0.78])
                samples[20]["contact_force_n"] = forces
                with self.assertRaisesRegex(ValueError, "숫자 2개"):
                    derive_plan_correction(
                        REFERENCE,
                        samples,
                        window_s=0.5,
                        max_motion_m=0.001,
                        contact_threshold_n=0.02,
                    )

    def test_non_reset_or_short_window_is_rejected(self) -> None:
        samples = _reset_window([0.395, 0.167, 0.78])
        samples[30]["phase"] = "APPROACH"
        with self.assertRaisesRegex(ValueError, "RESET 샘플만"):
            derive_plan_correction(
                REFERENCE, samples, window_s=0.5, max_motion_m=0.001, contact_threshold_n=0.02
            )
        with self.assertRaisesRegex(ValueError, "충분하지 않습니다"):
            derive_plan_correction(
                REFERENCE, samples[:20], window_s=0.5, max_motion_m=0.001, contact_threshold_n=0.02
            )

    def test_untrusted_metadata_cannot_poison_correction(self) -> None:
        samples = _reset_window([0.395, 0.167, 0.78])
        for sample in samples:
            sample.update(
                {
                    "spawn_offset_m": [99, 99, 99],
                    "grid_point": {"x_mm": -999},
                    "runtime_overrides": {"cup_center_m": [99, 99, 99]},
                    "failure": "pretend_failure",
                    "task_pass": True,
                }
            )
        result = derive_plan_correction(
            REFERENCE,
            samples,
            window_s=0.5,
            max_motion_m=0.001,
            contact_threshold_n=0.02,
        )
        self.assertEqual([round(value, 6) for value in result["correction_mm"]], [5.0, -3.0])

    def test_timestamp_and_numeric_validation(self) -> None:
        samples = _reset_window([0.395, 0.167, 0.78])
        samples[5]["time_s"] = samples[4]["time_s"]
        with self.assertRaisesRegex(ValueError, "엄격히 증가"):
            derive_plan_correction(
                REFERENCE, samples, window_s=0.5, max_motion_m=0.001, contact_threshold_n=0.02
            )
        samples = _reset_window([0.395, 0.167, 0.78])
        samples[1]["cup_position_m"][0] = math.nan
        with self.assertRaisesRegex(ValueError, "유한한 숫자"):
            derive_plan_correction(
                REFERENCE, samples, window_s=0.5, max_motion_m=0.001, contact_threshold_n=0.02
            )


class CommonReferenceEvaluationTest(unittest.TestCase):
    def test_pass_ignores_stored_failure_and_planned_center(self) -> None:
        result = _passing_result()
        evaluated = evaluate_common_reference(result, CONFIG, REFERENCE)
        self.assertEqual(evaluated["verdict"], "pass")
        self.assertTrue(evaluated["stored_outcome_ignored"])
        self.assertTrue(all(check["pass"] for check in evaluated["checks"]))

    def test_config_center_and_result_center_cannot_change_verdict(self) -> None:
        result = _passing_result()
        first = evaluate_common_reference(result, CONFIG, REFERENCE)
        changed_config = {**CONFIG, "cup_center_m": [-500.0, 200.0, 0.0]}
        result["evaluation_center_m"] = [-800.0, -800.0, -800.0]
        second = evaluate_common_reference(result, changed_config, REFERENCE)
        self.assertEqual(first, second)

    def test_not_lifted_fails_common_height(self) -> None:
        result = _passing_result()
        for sample in result["samples"]:
            if sample["phase"] == "LIFT_HOLD":
                sample["cup_position_m"][2] = REFERENCE[2] + 0.01
        evaluated = evaluate_common_reference(result, CONFIG, REFERENCE)
        checks = {check["id"]: check for check in evaluated["checks"]}
        self.assertEqual(evaluated["verdict"], "fail")
        self.assertFalse(checks["proxy_lift_height"]["pass"])

    def test_preclose_contact_fails_even_if_lift_succeeds(self) -> None:
        result = _passing_result()
        result["samples"][2]["contact_force_n"] = [0.02, 0.0]
        evaluated = evaluate_common_reference(result, CONFIG, REFERENCE)
        checks = {check["id"]: check for check in evaluated["checks"]}
        self.assertFalse(checks["no_preclose_contact"]["pass"])
        self.assertEqual(evaluated["verdict"], "fail")

    def test_common_evaluation_requires_two_sided_contact_force(self) -> None:
        for forces in ([0.5], [0.5, 0.6, 0.7]):
            with self.subTest(forces=forces):
                result = _passing_result()
                result["samples"][-1]["contact_force_n"] = forces
                with self.assertRaisesRegex(ValueError, "숫자 2개"):
                    evaluate_common_reference(result, CONFIG, REFERENCE)

    def test_recovery_result_is_excluded_from_prevention_evidence(self) -> None:
        result = _passing_result()
        result["recovery_enabled"] = True
        result["retry_count"] = 1
        for sample in result["samples"]:
            sample["attempt"] = 1
        evaluated = evaluate_common_reference(result, CONFIG, REFERENCE)
        checks = {check["id"]: check for check in evaluated["checks"]}
        self.assertFalse(checks["recovery_disabled"]["pass"])
        self.assertFalse(checks["retry_count_zero"]["pass"])
        self.assertFalse(checks["single_attempt"]["pass"])
        self.assertEqual(evaluated["verdict"], "fail")

    def test_unstable_terminal_measurements_fail(self) -> None:
        result = _passing_result()
        result["samples"][-1]["cup_tilt_deg"] = CONFIG["maximum_cup_tilt_deg"] + 0.1
        evaluated = evaluate_common_reference(result, CONFIG, REFERENCE)
        checks = {check["id"]: check for check in evaluated["checks"]}
        self.assertFalse(checks["cup_tilt_max"]["pass"])

    def test_short_hold_window_fails(self) -> None:
        result = _passing_result()
        result["samples"] = result["samples"][:10] + result["samples"][-20:]
        # Keep the raw physical clock contiguous after slicing.
        for index, sample in enumerate(result["samples"]):
            sample["time_s"] = index * DT
            sample["physics_time_s"] = 0.525 + index * DT
        evaluated = evaluate_common_reference(result, CONFIG, REFERENCE)
        checks = {check["id"]: check for check in evaluated["checks"]}
        self.assertFalse(checks["hold_window_samples"]["pass"])

    def test_bad_physics_time_is_rejected(self) -> None:
        result = _passing_result()
        result["samples"][25]["physics_time_s"] += DT / 2
        with self.assertRaisesRegex(ValueError, "시간 간격"):
            evaluate_common_reference(result, CONFIG, REFERENCE)

    def test_nonfinite_terminal_field_is_rejected(self) -> None:
        result = _passing_result()
        result["samples"][-1]["midbody_height_error_m"] = math.inf
        with self.assertRaisesRegex(ValueError, "유한한 숫자"):
            evaluate_common_reference(result, CONFIG, REFERENCE)

    def test_changed_reference_changes_verdict_while_metadata_does_not(self) -> None:
        result = _passing_result()
        poisoned = copy.deepcopy(result)
        poisoned.update({"task_pass": True, "evaluation_center_m": [0.39, 0.17, 0.83]})
        self.assertEqual(
            evaluate_common_reference(result, CONFIG, REFERENCE)["verdict"],
            evaluate_common_reference(poisoned, CONFIG, REFERENCE)["verdict"],
        )
        shifted_reference = [REFERENCE[0], REFERENCE[1], REFERENCE[2] + 0.02]
        self.assertEqual(
            evaluate_common_reference(result, CONFIG, shifted_reference)["verdict"],
            "fail",
        )


if __name__ == "__main__":
    unittest.main()
