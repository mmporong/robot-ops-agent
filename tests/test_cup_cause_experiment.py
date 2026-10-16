from __future__ import annotations

import copy
import math
import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from robot_ops import cup_cause_experiment  # noqa: E402


DT = 1.0 / 120.0


def _config() -> dict[str, object]:
    return {
        "cup_center_m": [0.39, 0.17, 0.78],
        "cup_spawn_offset_m": [0.0, -0.005, 0.0],
        "minimum_contact_force_n": 0.02,
        "friction": 0.8,
        "physics_dt_s": DT,
        "minimum_lift_m": 0.04,
        "maximum_tracking_error_rad": 0.12,
        "maximum_midbody_height_error_m": 0.015,
        "maximum_contact_center_error_m": 0.015,
        "maximum_cup_lateral_drift_m": 0.02,
        "maximum_cup_tilt_deg": 15.0,
        "recovery": {
            "observation_duration_s": 0.5,
            "maximum_observation_motion_m": 0.001,
        },
    }


def _samples(position: list[float]) -> list[dict[str, object]]:
    rows = []
    for index in range(61):
        rows.append(
            {
                "time_s": index * DT,
                "physics_time_s": 0.525 + index * DT,
                "phase": "RESET",
                "attempt": 0,
                "cup_position_m": list(position),
                "contact_force_n": [0.0, 0.0],
                "left_joint_actual_rad": [0.0, -0.95, 1.22, -0.26, 1.62],
                "gripper_positions_rad": [1.0],
            }
        )
    return rows


def _result(position: list[float] | None = None) -> dict[str, object]:
    return {"samples": _samples(position or [0.39, 0.165, 0.78])}


def _plan() -> dict[str, object]:
    return {
        "config": _config(),
        "tool_sha256": {
            "simulation/simulate_cup_contact.py": "a" * 64,
            "config/simulation/cup_contact_experiment.json": "b" * 64,
        },
    }


def _treatment_fixture(
    *,
    correction: list[float] | None = None,
    position: list[float] | None = None,
    unmodified: bool = False,
) -> tuple[dict, dict, dict, dict, dict]:
    correction = None if unmodified else ([0.0, -5.0] if correction is None else correction)
    position = [0.0, -5.0] if position is None else position
    source = _config()
    source["cup_spawn_offset_m"] = [0.0, 0.0, 0.0]
    center = list(source["cup_center_m"])
    change = correction or [0.0, 0.0]
    corrected_center = [
        center[0] + change[0] / 1000.0,
        center[1] + change[1] / 1000.0,
        center[2],
    ]
    effective_grid = [position[index] - change[index] for index in range(2)]
    offset = [
        effective_grid[0] / 1000.0,
        effective_grid[1] / 1000.0,
        source["cup_spawn_offset_m"][2],
    ]
    entry = {"position_mm": position, "correction_mm": correction}
    plan = {"config": {**copy.deepcopy(source), "cup_center_m": corrected_center, "cup_spawn_offset_m": offset}}
    params = {"recover": False}
    if correction is not None:
        params["plan_correction_mm"] = correction
    request = {"grid_point": {"x_mm": position[0], "y_mm": position[1]}, "params": params}
    response = {}
    if correction is not None:
        response["plan_intervention"] = {
            "correction_mm": correction,
            "original_grid_point_mm": position,
            "original_planned_center_m": center,
            "corrected_planned_center_m": corrected_center,
            "effective_grid_point_mm": effective_grid,
            "actual_spawn_center_m": [
                center[0] + position[0] / 1000.0,
                center[1] + position[1] / 1000.0,
                center[2] + source["cup_spawn_offset_m"][2],
            ],
        }
    return entry, plan, source, request, response


def _passing_experiment() -> dict[str, object]:
    runs = []
    core = {}
    expected = {"original": False, "sham": False, "corrected": True, "opposite": False}
    for condition, passed in expected.items():
        names = [f"core_{condition}_{index}" for index in range(3)]
        core[condition] = names
        runs.extend({"name": name, "common_evaluation": {"passed": passed}} for name in names)

    regression = []
    for x in (-5, 0, 5):
        for y in (-5, 0, 5):
            baseline_name = f"grid_{x}_{y}_baseline"
            corrected_name = f"grid_{x}_{y}_corrected"
            baseline_pass = x == 0 and y == 0
            runs.extend(
                (
                    {"name": baseline_name, "common_evaluation": {"passed": baseline_pass}},
                    {"name": corrected_name, "common_evaluation": {"passed": True}},
                )
            )
            regression.append(
                {
                    "x_mm": x,
                    "y_mm": y,
                    "baseline": baseline_name,
                    "corrected": corrected_name,
                    "baseline_pass": baseline_pass,
                    "corrected_pass": True,
                }
            )
    return {"runs": runs, "core": core, "regression": regression}


class DiagnoseBoundaryTest(unittest.TestCase):
    def test_only_allowlisted_reset_window_is_forwarded(self) -> None:
        config = _config()
        result = _result()
        for row in result["samples"]:
            row.update(
                {
                    "spawn_offset_m": [99.0, 99.0, 99.0],
                    "grid_point": {"x_mm": 999.0},
                    "runtime_overrides": {"cup_center_m": [99.0, 99.0, 99.0]},
                    "failure": "injected_failure",
                    "task_pass": True,
                }
            )
        result["samples"].append(
            {
                **copy.deepcopy(result["samples"][-1]),
                "time_s": 61 * DT,
                "physics_time_s": 0.525 + 61 * DT,
                "phase": "APPROACH",
                "cup_position_m": [88.0, 88.0, 88.0],
            }
        )
        result["samples"].append(
            {
                **copy.deepcopy(result["samples"][-1]),
                "time_s": 62 * DT,
                "physics_time_s": 0.525 + 62 * DT,
                "phase": "RESET",
            }
        )

        sentinel = {"status": "captured"}
        with mock.patch.object(
            cup_cause_experiment,
            "derive_plan_correction",
            return_value=sentinel,
        ) as derive:
            actual = cup_cause_experiment.diagnose(result, config)

        self.assertIs(actual, sentinel)
        derive.assert_called_once()
        args, kwargs = derive.call_args
        self.assertEqual(args[0], config["cup_center_m"])
        forwarded = args[1]
        self.assertEqual(len(forwarded), 61)
        self.assertTrue(all(row["phase"] == "RESET" for row in forwarded))
        self.assertEqual(
            set(forwarded[0]),
            {
                "time_s",
                "physics_time_s",
                "phase",
                "attempt",
                "cup_position_m",
                "contact_force_n",
            },
        )
        self.assertEqual(
            kwargs,
            {
                "window_s": 0.5,
                "max_motion_m": 0.001,
                "contact_threshold_n": 0.02,
            },
        )

    def test_diagnosis_is_unchanged_by_result_injection_metadata(self) -> None:
        clean = _result()
        poisoned = copy.deepcopy(clean)
        poisoned.update(
            {
                "evaluation_center_m": [20.0, 20.0, 20.0],
                "recovery_enabled": True,
                "retry_count": 900,
                "failure": "pretend_failure",
                "task_pass": True,
                "runtime_overrides": {"cup_center_m": [-20.0, -20.0, -20.0]},
            }
        )
        self.assertEqual(
            cup_cause_experiment.diagnose(clean, _config()),
            cup_cause_experiment.diagnose(poisoned, _config()),
        )


class VerifyPairTest(unittest.TestCase):
    def setUp(self) -> None:
        self.baseline = _result()
        self.baseline_plan = _plan()

    def _verify(self, result: dict | None = None, plan: dict | None = None) -> dict:
        return cup_cause_experiment.verify_pair(
            self.baseline,
            self.baseline_plan,
            result or copy.deepcopy(self.baseline),
            plan or copy.deepcopy(self.baseline_plan),
        )

    def test_accepts_xy_plan_change_when_actual_spawn_is_preserved(self) -> None:
        plan = copy.deepcopy(self.baseline_plan)
        plan["config"]["cup_center_m"][1] -= 0.005
        plan["config"]["cup_spawn_offset_m"][1] += 0.005

        controls = self._verify(plan=plan)

        self.assertTrue(controls["physical_spawn_equal"])
        self.assertTrue(controls["physics_and_thresholds_equal"])
        self.assertEqual(controls["initial_position_delta_mm"], 0.0)
        self.assertEqual(controls["reset_median_position_delta_mm"], 0.0)

    def test_rejects_changed_actual_spawn(self) -> None:
        plan = copy.deepcopy(self.baseline_plan)
        plan["config"]["cup_spawn_offset_m"][1] += 0.001
        with self.assertRaisesRegex(ValueError, "실제 컵 배치"):
            self._verify(plan=plan)

    def test_rejects_reset_median_shift_even_when_first_position_matches(self) -> None:
        result = copy.deepcopy(self.baseline)
        for row in result["samples"][1:]:
            row["cup_position_m"][1] += 0.0002
        with self.assertRaisesRegex(ValueError, "안정 RESET 위치"):
            self._verify(result=result)

    def test_rejects_changed_nonplanning_configuration(self) -> None:
        plan = copy.deepcopy(self.baseline_plan)
        plan["config"]["friction"] = 0.9
        with self.assertRaisesRegex(ValueError, "물리 설정 또는 판정 임계값"):
            self._verify(plan=plan)

    def test_rejects_changed_source_hash(self) -> None:
        plan = copy.deepcopy(self.baseline_plan)
        plan["tool_sha256"]["simulation/simulate_cup_contact.py"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "소스 해시"):
            self._verify(plan=plan)

    def test_rejects_initial_position_change(self) -> None:
        result = copy.deepcopy(self.baseline)
        result["samples"][0]["cup_position_m"][0] += 0.0002
        with self.assertRaisesRegex(ValueError, "초기 컵 위치"):
            self._verify(result=result)

    def test_rejects_initial_arm_or_gripper_change(self) -> None:
        for key, index in (("left_joint_actual_rad", 2), ("gripper_positions_rad", 0)):
            with self.subTest(key=key):
                result = copy.deepcopy(self.baseline)
                result["samples"][0][key][index] += 0.0002
                with self.assertRaisesRegex(ValueError, "초기 관절 상태"):
                    self._verify(result=result)

    def test_rejects_z_plan_change_even_when_actual_spawn_is_preserved(self) -> None:
        plan = copy.deepcopy(self.baseline_plan)
        plan["config"]["cup_center_m"][2] += 0.001
        plan["config"]["cup_spawn_offset_m"][2] -= 0.001
        with self.assertRaisesRegex(ValueError, "높이 설정"):
            self._verify(plan=plan)

    def test_rejects_invalid_center_and_spawn_vectors(self) -> None:
        invalid_vectors = (
            [math.nan, 0.17, 0.78],
            [0.39, math.inf, 0.78],
            [0.39, False, 0.78],
            [0.39, 0.17],
            [0.39, 0.17, 0.78, 0.0],
        )
        for key in ("cup_center_m", "cup_spawn_offset_m"):
            for value in invalid_vectors:
                with self.subTest(key=key, value=value):
                    plan = copy.deepcopy(self.baseline_plan)
                    plan["config"][key] = value
                    with self.assertRaisesRegex(ValueError, "유한한 숫자 배열"):
                        self._verify(plan=plan)

    def test_rejects_invalid_initial_cup_position_vectors(self) -> None:
        invalid_vectors = (
            [math.nan, 0.165, 0.78],
            [0.39, math.inf, 0.78],
            [0.39, True, 0.78],
            [0.39, 0.165],
            [0.39, 0.165, 0.78, 1.0],
        )
        for value in invalid_vectors:
            with self.subTest(value=value):
                result = copy.deepcopy(self.baseline)
                result["samples"][0]["cup_position_m"] = value
                with self.assertRaisesRegex(ValueError, "유한한 숫자 배열"):
                    self._verify(result=result)

    def test_rejects_nonfinite_or_boolean_initial_joint_values(self) -> None:
        for key in ("left_joint_actual_rad", "gripper_positions_rad"):
            for value in (math.nan, math.inf, True):
                with self.subTest(key=key, value=value):
                    result = copy.deepcopy(self.baseline)
                    result["samples"][0][key][0] = value
                    with self.assertRaisesRegex(ValueError, "유한한 숫자 배열"):
                        self._verify(result=result)

    def test_rejects_missing_or_different_length_initial_joint_vectors(self) -> None:
        for key in ("left_joint_actual_rad", "gripper_positions_rad"):
            with self.subTest(key=key, case="empty"):
                result = copy.deepcopy(self.baseline)
                result["samples"][0][key] = []
                with self.assertRaisesRegex(ValueError, "유한한 숫자 배열"):
                    self._verify(result=result)
            with self.subTest(key=key, case="different_length"):
                result = copy.deepcopy(self.baseline)
                result["samples"][0][key] = result["samples"][0][key] + [0.0]
                with self.assertRaisesRegex(ValueError, "대조할 수 없습니다"):
                    self._verify(result=result)


class TreatmentVerificationTest(unittest.TestCase):
    def test_accepts_exact_treatment_and_small_serialization_roundoff(self) -> None:
        entry, plan, source, request, response = _treatment_fixture()
        cup_cause_experiment.verify_treatment(entry, plan, source, request, response)

        rounded_plan = copy.deepcopy(plan)
        rounded_response = copy.deepcopy(response)
        rounded_plan["config"]["cup_center_m"][0] += 5e-10
        rounded_response["plan_intervention"]["effective_grid_point_mm"][1] += 5e-10
        cup_cause_experiment.verify_treatment(
            entry, rounded_plan, source, request, rounded_response
        )

    def test_rejects_wrong_correction_sign_or_changed_physical_spawn(self) -> None:
        entry, plan, source, request, response = _treatment_fixture()
        wrong_sign = copy.deepcopy(plan)
        wrong_sign["config"]["cup_center_m"][1] = source["cup_center_m"][1] + 0.005
        wrong_sign["config"]["cup_spawn_offset_m"][1] = -0.010
        with self.assertRaisesRegex(ValueError, r"보정 \+C/-C"):
            cup_cause_experiment.verify_treatment(
                entry, wrong_sign, source, request, response
            )

        changed_spawn = copy.deepcopy(plan)
        changed_spawn["config"]["cup_spawn_offset_m"][1] += 0.001
        with self.assertRaisesRegex(ValueError, r"보정 \+C/-C"):
            cup_cause_experiment.verify_treatment(
                entry, changed_spawn, source, request, response
            )

    def test_rejects_request_recovery_grid_or_correction_mismatch(self) -> None:
        entry, plan, source, request, response = _treatment_fixture()
        mutations = (
            ("recover", lambda value: value["params"].__setitem__("recover", True)),
            ("grid", lambda value: value.__setitem__("grid_point", {"x_mm": 1.0, "y_mm": -5.0})),
            ("correction", lambda value: value["params"].__setitem__("plan_correction_mm", [0.0, 5.0])),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                changed = copy.deepcopy(request)
                mutate(changed)
                with self.assertRaisesRegex(ValueError, "실행 요청과 실험 조건"):
                    cup_cause_experiment.verify_treatment(
                        entry, plan, source, changed, response
                    )

    def test_rejects_forged_adapter_intervention_metadata(self) -> None:
        entry, plan, source, request, response = _treatment_fixture()
        mutations = (
            ("correction", lambda value: value.__setitem__("correction_mm", [0.0, 5.0])),
            ("original_grid", lambda value: value.__setitem__("original_grid_point_mm", [0.0, 0.0])),
            ("original_center", lambda value: value.__setitem__("original_planned_center_m", [0.39, 0.18, 0.78])),
            ("corrected_center", lambda value: value["corrected_planned_center_m"].__setitem__(1, 0.16)),
            ("effective_grid", lambda value: value["effective_grid_point_mm"].__setitem__(1, -5.0)),
            ("actual_spawn", lambda value: value["actual_spawn_center_m"].__setitem__(1, 0.17)),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                changed = copy.deepcopy(response)
                mutate(changed["plan_intervention"])
                with self.assertRaisesRegex(ValueError, "개입 기록|보정 좌표"):
                    cup_cause_experiment.verify_treatment(
                        entry, plan, source, request, changed
                    )

    def test_unmodified_condition_rejects_intervention_metadata(self) -> None:
        entry, plan, source, request, response = _treatment_fixture(
            position=[0.0, 0.0], unmodified=True
        )
        cup_cause_experiment.verify_treatment(entry, plan, source, request, response)
        response["plan_intervention"] = {"correction_mm": [0.0, 0.0]}
        with self.assertRaisesRegex(ValueError, "무보정 실행"):
            cup_cause_experiment.verify_treatment(entry, plan, source, request, response)


class ExperimentPassedTest(unittest.TestCase):
    def test_requires_three_repeats_for_four_conditions_and_nine_grid_cells(self) -> None:
        experiment = _passing_experiment()
        self.assertTrue(cup_cause_experiment.experiment_passed(experiment))

        missing_repeat = copy.deepcopy(experiment)
        missing_repeat["core"]["opposite"].pop()
        with self.assertRaisesRegex(ValueError, "3회 반복"):
            cup_cause_experiment.experiment_passed(missing_repeat)

        missing_cell = copy.deepcopy(experiment)
        missing_cell["regression"].pop()
        with self.assertRaisesRegex(ValueError, "3×3"):
            cup_cause_experiment.experiment_passed(missing_cell)

    def test_rejects_forged_stored_grid_pass(self) -> None:
        experiment = _passing_experiment()
        experiment["regression"][0]["corrected_pass"] = False
        with self.assertRaisesRegex(ValueError, "격자 표시와 실행 판정"):
            cup_cause_experiment.experiment_passed(experiment)

    def test_returns_false_when_a_required_condition_fails(self) -> None:
        experiment = _passing_experiment()
        corrected_name = experiment["core"]["corrected"][1]
        entry = next(item for item in experiment["runs"] if item["name"] == corrected_name)
        entry["common_evaluation"]["passed"] = False
        self.assertFalse(cup_cause_experiment.experiment_passed(experiment))

        experiment = _passing_experiment()
        normal = next(
            cell
            for cell in experiment["regression"]
            if cell["x_mm"] == 0 and cell["y_mm"] == 0
        )
        normal["baseline_pass"] = False
        normal_entry = next(
            item for item in experiment["runs"] if item["name"] == normal["baseline"]
        )
        normal_entry["common_evaluation"]["passed"] = False
        self.assertFalse(cup_cause_experiment.experiment_passed(experiment))


if __name__ == "__main__":
    unittest.main()
